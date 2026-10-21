#!/usr/bin/env python3
"""Scenario eval runner for the Goaly SOP harness. Standard library only.

Drives POST /api/chat with the scripts in scenarios.py, checks each response against the
scenario's expectations plus the always-on invariants below, and writes one transcript per
scenario and a summary keyed to checklist.md IDs.

Usage:
  python3 evals/run_evals.py --list                     # coverage matrix, no server needed
  python3 evals/run_evals.py --mode fake                # against a FakeLLM server, skips live_only
  python3 evals/run_evals.py --only margaret_full_path_email_yes,V1
  python3 evals/run_evals.py --send-key                 # pass $ANTHROPIC_API_KEY as X-API-Key

Exit code: 0 when no scenario FAILs or ERRORs, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import checks
from scenarios import MANUAL_CHECKS, SCENARIOS

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = HERE.parent / "sop-agent" / "app" / "data" / "insurance_db.json"
CHECKLIST = HERE.parent / "checklist.md"

# Invariants run on every turn; failures are attributed to these checklist IDs.
INVARIANT_IDS = {
    "tools": ["R5"],
    "disclosure": ["V1"],
    "edges": ["W1"],
    "masking": ["P3"],
    "grounding": ["R3"],
}


class Client:
    def __init__(self, base_url: str, api_key: str | None, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("X-API-Key", self.api_key)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read() or b"null")

    def chat(self, session_id: str, message: str) -> dict:
        return self._request("POST", "/api/chat", {"session_id": session_id, "message": message})

    def reset(self, session_id: str) -> None:
        self._request("POST", "/api/reset", {"session_id": session_id})

    def outbox(self, session_id: str) -> list[dict]:
        query = urllib.parse.urlencode({"session_id": session_id})
        return self._request("GET", f"/api/outbox?{query}") or []


class Result:
    def __init__(self):
        self.items: list[tuple[str, list[str], str]] = []  # (FAIL|WARN|PASS, check ids, message)

    def add(self, level: str, ids: list[str], msg: str) -> None:
        self.items.append((level, ids, msg))

    @property
    def status(self) -> str:
        levels = {lvl for lvl, _, _ in self.items}
        for lvl in ("ERROR", "FAIL", "WARN"):
            if lvl in levels:
                return lvl
        return "PASS"


def _text_has(text: str, needle: str) -> bool:
    return needle.lower() in text.lower()


def check_expect(exp: dict, resp: dict, events: list[dict], ids: list[str], res: Result, level: str, tag: str):
    reply = resp.get("reply") or ""
    memory = resp.get("memory") or {}
    counters = resp.get("counters") or {}

    def fail(msg: str) -> None:
        res.add(level, ids, f"{tag}: {msg}")

    if "phase" in exp:
        want = exp["phase"] if isinstance(exp["phase"], list) else [exp["phase"]]
        if resp.get("phase") not in want:
            fail(f"phase {resp.get('phase')!r}, want {want}")
    for m in exp.get("events", []):
        if checks.find_event(events, m) < 0:
            fail(f"missing event {m}")
    for m in exp.get("no_events", []):
        if checks.find_event(events, m) >= 0:
            fail(f"unexpected event {m}")
    for a, b in exp.get("before", []):
        ib = checks.find_event(events, b)
        if ib >= 0 and not (0 <= checks.find_event(events, a) < ib):
            fail(f"{b} happened without {a} before it")
    for slot, want in exp.get("memory", {}).items():
        if slot not in memory:
            fail(f"memory slot {slot!r} missing")
        elif want is not True:
            value = str((memory[slot] or {}).get("value", ""))
            options = want if isinstance(want, list) else [want]
            if not any(_text_has(value, o) for o in options):
                fail(f"memory {slot}={value!r}, want one of {options}")
    for slot in exp.get("memory_absent", []):
        if slot in memory:
            fail(f"memory slot {slot!r} should be absent")
    for name, n in exp.get("counters_min", {}).items():
        if (counters.get(name) or 0) < n:
            fail(f"counter {name}={counters.get(name)}, want >= {n}")
    if "ended" in exp and bool(resp.get("ended")) != exp["ended"]:
        fail(f"ended={resp.get('ended')}, want {exp['ended']}")
    if exp.get("reply_any") and not any(_text_has(reply, s) for s in exp["reply_any"]):
        fail(f"reply has none of {exp['reply_any']}")
    for s in exp.get("reply_none", []):
        if _text_has(reply, s):
            fail(f"reply contains {s!r}")
    if "soft" in exp:
        check_expect(exp["soft"], resp, events, ids, res, "WARN", tag + " (soft)")


def check_invariants(state: dict, resp: dict, events: list[dict], ctx: dict, res: Result, tag: str):
    reply = resp.get("reply") or ""

    for e in events:
        if e.get("type") == "tool_call" and e.get("name") not in checks.ALLOWED_TOOLS:
            res.add("FAIL", INVARIANT_IDS["tools"], f"{tag}: tool {e.get('name')!r} is not in the allowed list")

    gate = checks.find_event(events, {"type": "gate", "name": "identity_verified", "passed": True})
    if not state["verified"]:
        for i, e in enumerate(events):
            if e.get("type") == "tool_call" and e.get("name") in checks.CLAIM_READ_TOOLS and (gate < 0 or i < gate):
                res.add("FAIL", INVARIANT_IDS["disclosure"], f"{tag}: {e.get('name')} called before verification")
        if gate < 0:
            leaks = checks.find_leaks(reply, ctx["leak_tokens"], state["user_text"])
            if leaks:
                res.add("FAIL", INVARIANT_IDS["disclosure"], f"{tag}: claim facts before verification: {leaks}")
    if gate >= 0:
        state["verified"] = True

    for e in events:
        if e.get("type") != "transition":
            continue
        pair = (e.get("from"), e.get("to"))
        if pair[1] == "HANDOFF" or pair in checks.ALLOWED_EDGES:
            pass
        elif pair in checks.QUESTIONABLE_EDGES:
            res.add("WARN", INVARIANT_IDS["edges"], f"{tag}: questionable edge {pair[0]} -> {pair[1]}")
        else:
            res.add("FAIL", INVARIANT_IDS["edges"], f"{tag}: illegal edge {pair[0]} -> {pair[1]}")
        if pair[0] != state["phase"]:
            res.add("FAIL", INVARIANT_IDS["edges"], f"{tag}: transition from {pair[0]} but phase was {state['phase']}")
        state["phase"] = pair[1]
    if resp.get("phase") != state["phase"]:
        res.add("FAIL", INVARIANT_IDS["edges"],
                f"{tag}: phase {resp.get('phase')} without a transition event (last known {state['phase']})")
        state["phase"] = resp.get("phase")
    if state["verified"] and state["phase"] == "VERIFY_ID":
        res.add("FAIL", INVARIANT_IDS["edges"], f"{tag}: back in VERIFY_ID after verification")

    # Memory, the masked extraction and the handoff packet are shown in the UI or passed on,
    # so no raw SSN, DOB or phone may appear anywhere in them (values or quotes).
    for field in ("memory", "extraction", "handoff_packet"):
        dump = json.dumps(resp.get(field) or {})
        exposed = sorted(raw for raw in ctx["raw_ids"] if raw in dump)
        if exposed:
            res.add("FAIL", INVARIANT_IDS["masking"], f"{tag}: raw identity values in {field}: {exposed}")

    if state["verified"]:
        bad = checks.ungrounded(reply, ctx["grounding"], state["user_text"])
        if bad:
            res.add("FAIL", INVARIANT_IDS["grounding"], f"{tag}: facts not in the caller's data: {bad}")


def check_outbox(spec: dict, new_mail: list[dict], ids: list[str], res: Result, raw_ssns: set[str]):
    if "count" in spec and len(new_mail) != spec["count"]:
        res.add("FAIL", ids, f"outbox: {len(new_mail)} new emails, want {spec['count']}")
    for mail in new_mail:
        body = f"{mail.get('subject', '')}\n{mail.get('body', '')}"
        if "to" in spec and mail.get("to") != spec["to"]:
            res.add("FAIL", ids, f"outbox: sent to {mail.get('to')!r}, want {spec['to']!r}")
        for group in spec.get("body_all_groups", []):
            if not any(_text_has(body, s) for s in group):
                res.add("FAIL", ids, f"outbox: body has none of {group}")
        for s in set(spec.get("body_none", [])) | raw_ssns:
            if _text_has(body, s):
                res.add("FAIL", ids + ["P3"], f"outbox: body contains {s!r}")


def run_scenario(sc: dict, client: Client, db: dict, out_dir: Path) -> Result:
    res = Result()
    ctx = {
        "leak_tokens": checks.claim_fact_tokens(checks.claims_for(db)),
        "grounding": checks.grounding_set(db, sc["customer"]),
        "raw_ids": checks.raw_identity_values(db),
    }
    state = {"phase": "VERIFY_ID", "verified": False, "user_text": ""}
    session_id = f"eval-{sc['name']}-{uuid.uuid4().hex[:8]}"
    lines = [f"# {sc['name']}", "", f"Covers: {', '.join(sc['covers'])}", f"Session: `{session_id}`", ""]

    try:
        client.reset(session_id)
        mail_before = len(client.outbox(session_id))
        for n, turn in enumerate(sc["turns"], 1):
            state["user_text"] += "\n" + turn["say"]
            started = time.time()
            resp = client.chat(session_id, turn["say"])
            elapsed = time.time() - started
            events = resp.get("events") or []
            tag = f"turn {n}"
            mark = len(res.items)
            check_expect(turn.get("expect", {}), resp, events, sc["covers"], res, "FAIL", tag)
            check_invariants(state, resp, events, ctx, res, tag)
            lines += [
                f"## Turn {n} ({elapsed:.1f}s)",
                "",
                f"**Caller:** {turn['say']}",
                "",
                f"**Agent** [{resp.get('phase')}]: {resp.get('reply')}",
                "",
                "Events: " + "; ".join(json.dumps(e, separators=(",", ":")) for e in events),
                "",
                "Memory: " + json.dumps(resp.get("memory") or {}, separators=(",", ":")),
                "",
                "Counters: " + json.dumps(resp.get("counters") or {}),
                "",
            ]
            lines += [f"- {lvl} {msg}" for lvl, _, msg in res.items[mark:]] or ["- all checks passed"]
            lines.append("")
        if "outbox" in sc:
            new_mail = client.outbox(session_id)[mail_before:]
            raw_ssns = {c["ssn_last4"] for c in db.get("customers", []) if c.get("ssn_last4")}
            check_outbox(sc["outbox"], new_mail, sc["covers"], res, raw_ssns)
            lines += ["## Outbox", "", "```json", json.dumps(new_mail, indent=2), "```", ""]
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError, OSError) as exc:
        res.add("ERROR", sc["covers"], f"request failed: {exc}")

    lines += [f"**Result: {res.status}**", ""]
    lines += [f"- {lvl} [{', '.join(ids)}] {msg}" for lvl, ids, msg in res.items if lvl != "PASS"]
    (out_dir / f"{sc['name']}.md").write_text("\n".join(lines) + "\n")
    return res


def checklist_ids() -> list[str]:
    if not CHECKLIST.exists():
        return []
    return re.findall(r"^\| ([A-Z]\d+) \|", CHECKLIST.read_text(), re.MULTILINE)


def checklist_tags() -> dict[str, str]:
    if not CHECKLIST.exists():
        return {}
    rows = re.findall(r"^\| ([A-Z]\d+) \| (MUST|IMPLIED|BONUS|DELIVERY) \|", CHECKLIST.read_text(), re.MULTILINE)
    return dict(rows)


RANK = {"PASS": 0, "WARN": 1, "FAIL": 2, "ERROR": 3}


def status_by_check(runs: list[tuple[dict, Result]]) -> dict[str, str]:
    """Worst level seen per checklist ID. A scenario-level failure counts against every ID the scenario covers."""
    out: dict[str, str] = {}

    def bump(cid: str, level: str) -> None:
        if RANK[level] > RANK.get(out.get(cid, "PASS"), 0) or cid not in out:
            out[cid] = level

    for sc, res in runs:
        for cid in sc["covers"]:
            bump(cid, "PASS")
        for level, ids, _ in res.items:
            for cid in ids:
                bump(cid, level)
    if runs:
        for ids in INVARIANT_IDS.values():
            for cid in ids:
                bump(cid, "PASS")
    return out


def tag_table(runs: list[tuple[dict, Result]]) -> list[str]:
    tags = checklist_tags()
    status = status_by_check(runs)
    rows = ["| Tag | Checks | Pass | Warn | Fail | Manual or not run |", "|---|---|---|---|---|---|"]
    for tag in ("MUST", "IMPLIED", "BONUS", "DELIVERY"):
        ids = [cid for cid, t in tags.items() if t == tag]
        counts = {lvl: sum(1 for cid in ids if status.get(cid) == lvl) for lvl in ("PASS", "WARN")}
        failed = sum(1 for cid in ids if status.get(cid) in ("FAIL", "ERROR"))
        rest = [cid for cid in ids if cid not in status]
        rows.append(f"| {tag} | {len(ids)} | {counts['PASS']} | {counts['WARN']} | {failed} | "
                    f"{len(rest)}{' (' + ', '.join(rest) + ')' if rest else ''} |")
    return rows


def coverage(selected: list[dict]) -> dict[str, list[str]]:
    cov: dict[str, list[str]] = {}
    for sc in selected:
        for cid in sc["covers"]:
            cov.setdefault(cid, []).append(sc["name"])
    for ids in INVARIANT_IDS.values():
        for cid in ids:
            cov.setdefault(cid, []).append("(invariant, every turn)")
    return cov


def print_coverage(selected: list[dict]) -> list[str]:
    cov = coverage(selected)
    rows = ["| Check | Automated by | Manual |", "|---|---|---|"]
    for cid in checklist_ids() or sorted(cov):
        auto = ", ".join(sorted(set(cov.get(cid, [])))) or "-"
        rows.append(f"| {cid} | {auto} | {MANUAL_CHECKS.get(cid, '')} |")
    uncovered = [cid for cid in checklist_ids() if cid not in cov and cid not in MANUAL_CHECKS]
    if uncovered:
        rows += ["", f"Not covered at all: {', '.join(uncovered)}"]
    return rows


def select(only: str | None, mode: str) -> list[dict]:
    chosen = SCENARIOS
    if only:
        keys = {k.strip() for k in only.split(",") if k.strip()}
        chosen = [s for s in SCENARIOS if s["name"] in keys or keys & set(s["covers"])]
    if mode == "fake":
        chosen = [s for s in chosen if not s.get("live_only")]
    return chosen


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.environ.get("SOP_BASE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--data", default=os.environ.get("SOP_DATA_PATH", str(DEFAULT_DATA)))
    ap.add_argument("--mode", choices=["live", "fake"], default="live")
    ap.add_argument("--only", help="comma-separated scenario names and/or checklist IDs")
    ap.add_argument("--out", default=str(HERE / "runs"))
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--send-key", action="store_true", help="send $ANTHROPIC_API_KEY as X-API-Key")
    ap.add_argument("--list", action="store_true", help="print the coverage matrix and exit")
    args = ap.parse_args()

    selected = select(args.only, args.mode)
    if args.list:
        print("\n".join(print_coverage(selected)))
        return 0
    if not selected:
        print("no scenarios selected", file=sys.stderr)
        return 1

    db = checks.load_fixture(args.data)
    api_key = os.environ.get("ANTHROPIC_API_KEY") if args.send_key else None
    client = Client(args.base_url, api_key, args.timeout)
    out_dir = Path(args.out) / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = ["# Eval run", "", f"Server: {args.base_url} · mode: {args.mode} · data: {args.data}", "",
               "| Scenario | Result | Covers | Problems |", "|---|---|---|---|"]
    worst = 0
    runs: list[tuple[dict, Result]] = []
    for sc in selected:
        res = run_scenario(sc, client, db, out_dir)
        runs.append((sc, res))
        problems = sum(1 for lvl, _, _ in res.items if lvl in ("FAIL", "ERROR"))
        warns = sum(1 for lvl, _, _ in res.items if lvl == "WARN")
        print(f"{res.status:5} {sc['name']}  ({problems} fail, {warns} warn)")
        summary.append(f"| [{sc['name']}]({sc['name']}.md) | {res.status} | {', '.join(sc['covers'])} | "
                       f"{problems} fail, {warns} warn |")
        if res.status in ("FAIL", "ERROR"):
            worst = 1
    by_tag = tag_table(runs)
    print("\n" + "\n".join(by_tag))
    summary += ["", "## Checklist results by tag", ""] + by_tag
    summary += ["", "## Coverage", ""] + print_coverage(selected)
    (out_dir / "summary.md").write_text("\n".join(summary) + "\n")
    print(f"\nTranscripts and summary: {out_dir}")
    return worst


if __name__ == "__main__":
    sys.exit(main())
