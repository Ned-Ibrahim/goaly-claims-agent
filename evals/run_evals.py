#!/usr/bin/env python3
"""Scenario eval runner for the Goaly claims agent. Standard library only (the simulator also needs the anthropic SDK).

Drives POST /api/chat with the scripts in scenarios.py, checks each response against the
scenario's expectations plus the always-on invariants below, and writes one transcript per
scenario and a summary keyed to checklist.md IDs. Grading reads state and events (slots, gates,
tool calls, phase), not reply wording; wording checks only WARN.

Usage:
  python3 evals/run_evals.py --list                     # coverage matrix, no server needed
  python3 evals/run_evals.py --mode fake                # against a FakeLLM server, skips live_only
  python3 evals/run_evals.py --only margaret_full_path_email_yes,V1
  python3 evals/run_evals.py --consent-scenario timeout --only consent_timeout   # server started with CLAIMS_CONSENT_SCENARIO=timeout
  python3 evals/run_evals.py --simulate --k 5           # model-played caller, pass^k (needs ANTHROPIC_API_KEY)
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
from scenarios import MANUAL_CHECKS, SCENARIOS, SIM_SCENARIOS

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = HERE.parent / "app" / "data" / "starter"
CHECKLIST = HERE.parent / "checklist.md"

# Invariants run on every turn; failures are attributed to these checklist IDs.
INVARIANT_IDS = {
    "tools": ["R5"],
    "disclosure": ["V1"],
    "edges": ["W1"],
    "masking": ["P3"],
    "grounding": ["R3"],
    "consent": ["C1"],
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

    def chat(self, session_id: str, message: str, channel: str = "text") -> dict:
        body = {"session_id": session_id, "message": message}
        if channel != "text":
            body["channel"] = channel
        return self._request("POST", "/api/chat", body)

    def state(self, session_id: str) -> dict:
        query = urllib.parse.urlencode({"session_id": session_id})
        return self._request("GET", f"/api/state?{query}") or {}

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


def consent_state(resp: dict, events: list[dict]) -> str | None:
    """The consent status as far as the response shows it: a state field first, then a consent gate event."""
    value = resp.get("consent_status")
    if isinstance(resp.get("consent"), dict):
        value = value or resp["consent"].get("status")
    if value:
        return str(value)
    for e in events:
        if e.get("type") == "gate" and "consent" in str(e.get("name", "")) and "email" not in str(e.get("name", "")):
            return "approved" if e.get("passed") else "pending"
    return None


def check_expect(exp: dict, resp: dict, events: list[dict], ids: list[str], res: Result, level: str, tag: str,
                 ctx: dict | None = None, user_text: str = ""):
    reply = resp.get("reply") or ""
    memory = resp.get("memory") or {}
    counters = resp.get("counters") or {}

    def fail(msg: str) -> None:
        res.add(level, ids, f"{tag}: {msg}")

    if "phase" in exp:
        want = exp["phase"] if isinstance(exp["phase"], list) else [exp["phase"]]
        if resp.get("phase") not in want:
            fail(f"phase {resp.get('phase')!r}, want {want}")
    if "verified" in exp and bool(resp.get("verified")) != exp["verified"]:
        fail(f"verified={resp.get('verified')}, want {exp['verified']}")
    for key, field in (("proposed_claim", "proposed_claim_id"), ("selected_claim", "selected_claim_id")):
        if key in exp:
            want = exp[key] if isinstance(exp[key], list) else [exp[key]]
            if resp.get(field) not in want:
                fail(f"{field}={resp.get(field)!r}, want {want}")
    for key, want in exp.get("state", {}).items():
        got = resp.get(key)
        options = want if isinstance(want, list) else [want]
        if got not in options:
            fail(f"state {key}={got!r}, want one of {options}")
    if "consent" in exp:
        want = exp["consent"] if isinstance(exp["consent"], list) else [exp["consent"]]
        if consent_state(resp, events) not in want:
            fail(f"consent {consent_state(resp, events)!r}, want {want}")
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
        # "a|b" accepts either counter name (the refusal count is named by the escalation code).
        if max((counters.get(part) or 0) for part in name.split("|")) < n:
            fail(f"counter {name}={counters.get(name)}, want >= {n}")
    for name, n in exp.get("counters_max", {}).items():
        if (counters.get(name) or 0) > n:
            fail(f"counter {name}={counters.get(name)}, want <= {n}")
    if "ended" in exp and bool(resp.get("ended")) != exp["ended"]:
        fail(f"ended={resp.get('ended')}, want {exp['ended']}")
    if exp.get("no_leaks") and ctx:
        leaks = checks.find_leaks(reply, ctx["leak_tokens"], user_text)
        if leaks:
            fail(f"claim facts in reply: {leaks}")
    if exp.get("reply_any") and not any(_text_has(reply, s) for s in exp["reply_any"]):
        fail(f"reply has none of {exp['reply_any']}")
    for s in exp.get("reply_none", []):
        if _text_has(reply, s):
            fail(f"reply contains {s!r}")
    if "soft" in exp:
        check_expect(exp["soft"], resp, events, ids, res, "WARN", tag + " (soft)", ctx, user_text)


def check_invariants(state: dict, resp: dict, events: list[dict], ctx: dict, res: Result, tag: str,
                     switch: bool = False):
    reply = resp.get("reply") or ""
    rep = ctx["rep"]

    for e in events:
        if e.get("type") == "tool_call" and not checks.tool_allowed(e.get("name")):
            res.add("FAIL", INVARIANT_IDS["tools"], f"{tag}: tool {e.get('name')!r} is not in the allowed list")

    was_verified = state["verified"]
    gate = checks.find_event(events, {"type": "gate", "name": "identity_verified", "passed": True})
    consent_ok = state["consent_ok"] or consent_state(resp, events) == "approved"
    reads = [(i, e) for i, e in enumerate(events) if e.get("type") == "tool_call" and e.get("name") in checks.CLAIM_READ_TOOLS]
    for i, e in reads:
        if not was_verified and (gate < 0 or i < gate):
            res.add("FAIL", INVARIANT_IDS["disclosure"], f"{tag}: {e.get('name')} called before verification")
        elif rep and not consent_ok:
            res.add("FAIL", INVARIANT_IDS["consent"], f"{tag}: {e.get('name')} called before the policyholder's consent")
    if (not was_verified and gate < 0) or (rep and not consent_ok):
        leaks = checks.find_leaks(reply, ctx["leak_tokens"], state["user_text"])
        if leaks:
            ids = INVARIANT_IDS["consent"] if was_verified or gate >= 0 else INVARIANT_IDS["disclosure"]
            res.add("FAIL", ids, f"{tag}: claim facts before the caller may hear them: {leaks}")
    if gate >= 0:
        state["verified"] = True
    state["consent_ok"] = consent_ok

    for e in events:
        if e.get("type") != "transition":
            continue
        pair = (e.get("from"), e.get("to"))
        if pair[1] == "HANDOFF" or pair in checks.ALLOWED_EDGES or (switch and pair in checks.SWITCH_EDGES):
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
    if switch and state["phase"] == "VERIFY_ID":
        state["verified"] = False  # a switch of person wipes the old caller (6c-switch)
        state["consent_ok"] = False
    if state["verified"] and state["phase"] == "VERIFY_ID":
        res.add("FAIL", INVARIANT_IDS["edges"], f"{tag}: back in VERIFY_ID after verification")

    # Memory, the masked extraction, the transcript and the handoff packet are shown in the UI or passed on,
    # so no raw ID digits, DOB, phone or email may appear anywhere in them (values or quotes).
    for field in ("memory", "extraction", "handoff_packet", "transcript"):
        dump = json.dumps(resp.get(field) or {})
        exposed = sorted(raw for raw in ctx["raw_ids"] if raw in dump)
        if exposed:
            res.add("FAIL", INVARIANT_IDS["masking"], f"{tag}: raw identity values in {field}: {len(exposed)} found")

    if state["verified"] and (not rep or consent_ok):
        bad = checks.ungrounded(reply, ctx["grounding"](state["customer"]), state["user_text"])
        if bad:
            res.add("FAIL", INVARIANT_IDS["grounding"], f"{tag}: facts not in the caller's data: {bad}")


def check_outbox(spec: dict, new_mail: list[dict], ids: list[str], res: Result, raw_ids: set[str]):
    if "count" in spec and len(new_mail) != spec["count"]:
        res.add("FAIL", ids, f"outbox: {len(new_mail)} new emails, want {spec['count']}")
    for mail in new_mail:
        body = f"{mail.get('subject', '')}\n{mail.get('body', '')}"
        if "to" in spec and mail.get("to") != spec["to"]:
            res.add("FAIL", ids, f"outbox: sent to {mail.get('to')!r}, want {spec['to']!r}")
        for group in spec.get("body_all_groups", []):
            if not any(_text_has(body, s) for s in group):
                res.add("FAIL", ids, f"outbox: body has none of {group}")
        for s in set(spec.get("body_none", [])) | {r for r in raw_ids if not r.startswith("+") and "@" not in r}:
            if _text_has(body, s):
                res.add("FAIL", ids + ["P3"], f"outbox: body contains an identity value")
                break


class Run:
    """One conversation against the server: sends turns, applies invariants, keeps the transcript."""

    def __init__(self, sc: dict, client, db: dict, out_dir: Path, label: str | None = None):
        self.sc, self.client, self.db = sc, client, db
        self.res = Result()
        self.name = label or sc["name"]
        self.out_dir = out_dir
        raw_all = checks.raw_identity_values(db)
        cache: dict[str, set[str]] = {}

        def grounding(party: str) -> set[str]:
            if party not in cache:
                cache[party] = checks.grounding_set(db, party)
            return cache[party]

        self.ctx = {
            "leak_tokens": checks.claim_fact_tokens(checks.claims_for(db)),
            "grounding": grounding,
            "raw_ids": raw_all,
            "rep": bool(sc.get("rep")),
        }
        self.state = {"phase": "VERIFY_ID", "verified": False, "consent_ok": False, "user_text": "",
                      "customer": sc["customer"]}
        self.session_id = f"eval-{self.name}-{uuid.uuid4().hex[:8]}"
        self.lines = [f"# {self.name}", "", f"Covers: {', '.join(sc['covers'])}", f"Session: `{self.session_id}`", ""]
        self.all_events: list[dict] = []
        self.last: dict = {}
        self.n = 0
        self.mail_before = 0

    def start(self) -> None:
        self.client.reset(self.session_id)
        self.mail_before = len(self.client.outbox(self.session_id))

    def send(self, say: str, channel: str = "text", switch: bool = False, customer: str | None = None):
        self.n += 1
        if customer:
            self.state["customer"] = customer
        self.state["user_text"] += "\n" + say
        started = time.time()
        resp = self.client.chat(self.session_id, say) if channel == "text" else self.client.chat(self.session_id, say, channel)
        elapsed = time.time() - started
        events = resp.get("events") or []
        self.all_events += events
        self.last = resp
        tag = f"turn {self.n}"
        mark = len(self.res.items)
        check_invariants(self.state, resp, events, self.ctx, self.res, tag, switch)
        self.lines += [
            f"## Turn {self.n} ({elapsed:.1f}s, {channel})", "", f"**Caller:** {say}", "",
            f"**Agent** [{resp.get('phase')}]: {resp.get('reply')}", "",
            "Events: " + "; ".join(json.dumps(e, separators=(",", ":")) for e in events), "",
            "Memory: " + json.dumps(resp.get("memory") or {}, separators=(",", ":")), "",
            "Counters: " + json.dumps(resp.get("counters") or {}), "",
        ]
        self._flush = mark
        return resp, events, tag

    def expect(self, exp: dict, resp: dict, events: list[dict], tag: str, ids: list[str] | None = None):
        mark = len(self.res.items)
        check_expect(exp, resp, events, ids or self.sc["covers"], self.res, "FAIL", tag, self.ctx, self.state["user_text"])
        return self.res.items[mark:]

    def note_turn_results(self) -> None:
        new = self.res.items[self._flush:]
        self.lines += [f"- {lvl} {msg}" for lvl, _, msg in new] or ["- all checks passed"]
        self.lines.append("")

    def finish(self, extra_final: dict | None = None) -> Result:
        """Scenario-level checks on the whole conversation, the mail outbox and the /api/state view."""
        sc = self.sc
        final = extra_final if extra_final is not None else sc.get("final")
        if final and self.last:
            check_expect(final, self.last, self.all_events, sc["covers"], self.res, "FAIL", "final", self.ctx,
                         self.state["user_text"])
        if self.last:
            try:
                live = self.client.state(self.session_id)
                for key in ("phase", "verified"):
                    if key in live and live[key] != self.last.get(key):
                        self.res.add("FAIL", sc["covers"], f"/api/state {key}={live[key]!r} differs from the last turn")
            except (urllib.error.URLError, OSError, ValueError):
                self.res.add("WARN", sc["covers"], "/api/state not readable")
        if "outbox" in sc:
            new_mail = self.client.outbox(self.session_id)[self.mail_before:]
            check_outbox(sc["outbox"], new_mail, sc["covers"], self.res, self.ctx["raw_ids"])
            self.lines += ["## Outbox", "", "```json", json.dumps(new_mail, indent=2), "```", ""]
        self.lines += [f"**Result: {self.res.status}**", ""]
        self.lines += [f"- {lvl} [{', '.join(ids)}] {msg}" for lvl, ids, msg in self.res.items if lvl != "PASS"]
        (self.out_dir / f"{self.name}.md").write_text("\n".join(self.lines) + "\n")
        return self.res


def run_scenario(sc: dict, client, db: dict, out_dir: Path) -> Result:
    run = Run(sc, client, db, out_dir)
    try:
        run.start()
        for turn in sc["turns"]:
            until, limit = turn.get("until"), turn.get("max", 1)
            for attempt in range(1, (limit if until else 1) + 1):
                resp, events, tag = run.send(turn["say"], turn.get("channel", "text"), turn.get("switch", False),
                                             turn.get("customer"))
                if turn.get("each"):
                    run.expect(turn["each"], resp, events, tag)
                satisfied = True
                if until:
                    satisfied = False
                    for alt in until if isinstance(until, list) else [until]:
                        scratch = Result()
                        check_expect(alt, resp, events, [], scratch, "FAIL", "until", run.ctx, run.state["user_text"])
                        satisfied = satisfied or not scratch.items
                if not until or satisfied or attempt == limit:
                    if until and not satisfied:
                        run.res.add("FAIL", sc["covers"], f"{tag}: condition {until} not reached in {limit} turns")
                    run.expect(turn.get("expect", {}), resp, events, tag)
                    run.note_turn_results()
                    break
                run.note_turn_results()
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError, OSError) as exc:
        run.res.add("ERROR", sc["covers"], f"request failed: {exc}")
    return run.finish()


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


def select(only: str | None, mode: str, consent_scenario: str = "default") -> list[dict]:
    chosen = SCENARIOS
    if only:
        keys = {k.strip() for k in only.split(",") if k.strip()}
        chosen = [s for s in SCENARIOS if s["name"] in keys or keys & set(s["covers"])]
    if mode == "fake":
        chosen = [s for s in chosen if not s.get("live_only")]
    return [s for s in chosen if s.get("consent_scenario", "default") == consent_scenario]


def select_sim(only: str | None, consent_scenario: str = "default") -> list[dict]:
    chosen = SIM_SCENARIOS
    if only:
        keys = {k.strip() for k in only.split(",") if k.strip()}
        chosen = [s for s in chosen if s["name"] in keys or keys & set(s["covers"])]
    return [s for s in chosen if s.get("consent_scenario", "default") == consent_scenario]


def run_simulated(sc: dict, client, db: dict, out_dir: Path, complete, run_idx: int, max_turns: int = 14) -> Result:
    """One model-played call. Same invariants as scripted runs, graded on `sc["grade"]` at the end."""
    import simulate

    spec = {**sc, "covers": sc["covers"], "customer": sc["customer"]}
    run = Run(spec, client, db, out_dir, label=f"{sc['name']}-run{run_idx + 1}")
    caller = simulate.Caller(complete, sc, run_idx)
    try:
        run.start()
        reply = None
        for _ in range(max_turns):
            say = caller.next_message(reply)
            if say is None:
                break
            resp, events, tag = run.send(say, sc.get("channel", "text"), sc.get("switch", False))
            run.note_turn_results()
            reply = resp.get("reply") or ""
            if resp.get("ended"):
                break
        else:
            run.res.add("WARN", sc["covers"], f"call still open after {max_turns} turns")
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError, OSError) as exc:
        run.res.add("ERROR", sc["covers"], f"request failed: {exc}")
    except Exception as exc:  # the caller model failed (network, auth); not an agent failure
        run.res.add("ERROR", sc["covers"], f"simulator failed: {type(exc).__name__}")
    return run.finish(extra_final=sc["grade"])


def simulate_all(args, client, db, out_dir: Path) -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("SKIP simulated caller mode: ANTHROPIC_API_KEY is not set. The scripted scenarios do not need it.")
        return 0
    selected = select_sim(args.only, args.consent_scenario)
    if not selected:
        print("no simulated scenarios selected", file=sys.stderr)
        return 1
    try:
        import simulate
        complete = simulate.anthropic_complete(api_key, args.sim_model)
    except ImportError:
        print("SKIP simulated caller mode: the anthropic package is not installed (run with `uv run python evals/run_evals.py ...`).")
        return 0
    sim_dir = out_dir / "sim"
    sim_dir.mkdir(parents=True, exist_ok=True)
    rows = ["| Scenario | pass^k | Runs passed | Per-run results |", "|---|---|---|---|"]
    failed = 0
    for sc in selected:
        results = [run_simulated(sc, client, db, sim_dir, complete, i) for i in range(args.k)]
        statuses = [r.status for r in results]
        ok = [st in ("PASS", "WARN") for st in statuses]
        passed = all(ok)
        failed += 0 if passed else 1
        print(f"{'PASS' if passed else 'FAIL':5} {sc['name']}  pass^{args.k}: {sum(ok)}/{args.k} runs  {statuses}")
        for i, r in enumerate(results, 1):
            for lvl, _, msg in r.items:
                if lvl in ("FAIL", "ERROR"):
                    print(f"        run {i}: {msg}")
        rows.append(f"| {sc['name']} | {'PASS' if passed else 'FAIL'} | {sum(ok)}/{args.k} | {', '.join(statuses)} |")
    rate = 1 - failed / len(selected)
    summary = ["# Simulated caller run", "", f"k = {args.k}, model = {args.sim_model}, scenarios passing pass^k: "
               f"{len(selected) - failed}/{len(selected)} ({rate:.0%})", ""] + rows
    (out_dir / "summary-sim.md").write_text("\n".join(summary) + "\n")
    print(f"\npass^{args.k} overall: {len(selected) - failed}/{len(selected)} scenarios\nTranscripts and summary: {out_dir}")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.environ.get("SOP_BASE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--data", default=os.environ.get("SOP_DATA_DIR") or os.environ.get("SOP_DATA_PATH") or str(DEFAULT_DATA))
    ap.add_argument("--mode", choices=["live", "fake"], default="live")
    ap.add_argument("--only", help="comma-separated scenario names and/or checklist IDs")
    ap.add_argument("--out", default=str(HERE / "runs"))
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--send-key", action="store_true", help="send $ANTHROPIC_API_KEY as X-API-Key")
    ap.add_argument("--list", action="store_true", help="print the coverage matrix and exit")
    ap.add_argument("--consent-scenario", default="default",
                    help="the CLAIMS_CONSENT_SCENARIO the server was started with; only matching scenarios run")
    ap.add_argument("--simulate", action="store_true", help="model-played caller instead of scripted turns")
    ap.add_argument("--k", type=int, default=5, help="runs per scenario in --simulate mode (pass^k)")
    ap.add_argument("--sim-model", default=os.environ.get("EVAL_SIM_MODEL", "claude-sonnet-5-5"))
    args = ap.parse_args()

    selected = select(args.only, args.mode, args.consent_scenario)
    if args.list:
        print("\n".join(print_coverage(selected)))
        return 0
    if args.simulate and not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("SKIP simulated caller mode: ANTHROPIC_API_KEY is not set. The scripted scenarios do not need it.")
        return 0
    if not selected and not args.simulate:
        print("no scenarios selected", file=sys.stderr)
        return 1

    db = checks.load_fixture(args.data)
    api_key = os.environ.get("ANTHROPIC_API_KEY") if args.send_key else None
    client = Client(args.base_url, api_key, args.timeout)
    out_dir = Path(args.out) / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.simulate:
        return simulate_all(args, client, db, out_dir)

    summary = ["# Eval run", "", f"Server: {args.base_url}; mode: {args.mode}; data: {args.data}; "
               f"consent scenario: {args.consent_scenario}", "",
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
    if CHECKLIST.exists():  # the checklist lives outside the repo; without it the tag table would be all zeros
        by_tag = tag_table(runs)
        print("\n" + "\n".join(by_tag))
        summary += ["", "## Checklist results by tag", ""] + by_tag
    summary += ["", "## Coverage", ""] + print_coverage(selected)
    (out_dir / "summary.md").write_text("\n".join(summary) + "\n")
    counts = {lvl: sum(1 for _, r in runs if r.status == lvl) for lvl in ("PASS", "WARN", "FAIL", "ERROR")}
    print(f"\n{len(runs)} scenarios: {counts['PASS']} pass, {counts['WARN']} warn, {counts['FAIL']} fail, {counts['ERROR']} error")
    print(f"Transcripts and summary: {out_dir}")
    return worst


if __name__ == "__main__":
    sys.exit(main())
