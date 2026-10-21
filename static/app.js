"use strict";

const PHASES = ["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS"];
const $ = (id) => document.getElementById(id);
const store = {
  get(k) { try { return sessionStorage.getItem(k) || ""; } catch { return ""; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};

let sessionId = newId();
let lastPhase = "VERIFY_ID";
let sending = false;

function newId() {
  return (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).slice(0, 18);
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function addMessage(role, text, meta) {
  const welcome = $("log").querySelector(".welcome");
  if (welcome) welcome.remove();
  const m = el("div", `msg ${role}`, text);
  if (meta) m.appendChild(el("span", "meta", meta));
  $("log").appendChild(m);
  $("log").scrollTop = $("log").scrollHeight;
  return m;
}

function showWelcome() {
  $("log").replaceChildren(
    el("p", "welcome",
      "You are the caller. Start with a greeting, or pick a scenario below. " +
      "The right panel shows what the SOP controller decided on each turn.")
  );
}

function renderPhase(phase) {
  const idx = PHASES.indexOf(phase);
  const reached = idx >= 0 ? idx : PHASES.indexOf(lastPhase);
  document.querySelectorAll("#phases li").forEach((li, i) => {
    li.classList.toggle("active", li.dataset.phase === phase);
    li.classList.toggle("done", phase === "ENDED" ? i <= reached : i < reached);
  });
  const term = $("terminal");
  if (phase === "HANDOFF") {
    term.textContent = "Transferred to a human representative. Start a new conversation to try again.";
    term.hidden = false;
  } else if (phase === "ENDED") {
    term.textContent = "Call complete. Start a new conversation to try again.";
    term.hidden = false;
  } else {
    term.hidden = true;
  }
  if (idx >= 0) lastPhase = phase;
}

function renderGates(events) {
  const ul = $("gates");
  ul.replaceChildren();
  const rows = events.filter((e) => ["gate", "handoff_offered", "handoff", "guard"].includes(e.type));
  if (!rows.length) {
    ul.appendChild(el("li", "empty", "No gate was evaluated on this turn."));
    return;
  }
  for (const e of rows) {
    const li = el("li");
    let pill;
    if (e.type === "gate") pill = el("span", `pill ${e.passed ? "ok" : "bad"}`, e.passed ? "PASS" : "BLOCK");
    else if (e.type === "guard") pill = el("span", "pill warn", "GUARD");
    else pill = el("span", "pill warn", e.type === "handoff" ? "HANDOFF" : "OFFER");
    li.appendChild(pill);
    li.appendChild(el("span", "name", e.name || e.reason || ""));
    const detail = e.detail || (e.type !== "gate" ? (e.action || e.reason || "") : "");
    if (detail) li.appendChild(el("span", "detail", detail));
    ul.appendChild(li);
  }
}

function renderMemory(memory, turn) {
  const tb = $("memory");
  tb.replaceChildren();
  const entries = Object.entries(memory || {});
  if (!entries.length) {
    const tr = el("tr");
    const td = el("td", "empty", "Empty.");
    td.colSpan = 3;
    tr.appendChild(td);
    tb.appendChild(tr);
    return;
  }
  for (const [slot, v] of entries) {
    const tr = el("tr", v.source_turn === turn ? "fresh" : "");
    tr.appendChild(el("td", "", slot));
    tr.appendChild(el("td", "", String(v.value)));
    tr.appendChild(el("td", "", `turn ${v.source_turn} · ${v.phase_captured}`));
    tr.title = `Caller said: "${v.quote}"`;
    tb.appendChild(tr);
  }
}

function renderCounters(c) {
  const box = $("counters");
  box.replaceChildren();
  const items = [
    ["verify_failures", "failed verifications", "handoff at 3"],
    ["off_topic", "off-topic questions", "offer at 3, handoff at 5"],
    ["refusals", "ID refusals", "offer at 3"],
  ];
  for (const [k, label, rule] of items) {
    const d = el("div", "counter");
    d.appendChild(el("b", "", String(c?.[k] ?? 0)));
    d.appendChild(el("span", "", label));
    d.title = rule;
    box.appendChild(d);
  }
}

function describe(e) {
  switch (e.type) {
    case "transition": return `→ ${e.from} ⇒ ${e.to}`;
    case "gate": return `gate ${e.name}: ${e.passed ? "pass" : "BLOCK"}${e.detail ? ` (${e.detail})` : ""}`;
    case "tool_call": return `tool ${e.name}()`;
    case "memory_write": return `memory ← ${e.slot}`;
    case "scope": return `scope: ${e.in_scope ? "in scope" : `OUT OF SCOPE (#${e.count})`}`;
    case "emotion": return `emotion: ${e.label}`;
    case "path": return `workflow path: ${e.name}`;
    case "email_sent": return `email sent to ${e.to}`;
    case "handoff_offered": return `offered human (${e.reason})`;
    case "handoff": return `HANDOFF (${e.reason})`;
    case "guard": return `guard ${e.name}: ${e.action}${e.found ? ` [${e.found.join(", ")}]` : ""}`;
    default: return JSON.stringify(e);
  }
}

function addTrace(turn, events, extraction) {
  const ol = $("trace");
  const empty = ol.querySelector(".empty");
  if (empty) empty.remove();
  const li = el("li", "turn");
  li.appendChild(el("h3", "", `Turn ${turn}`));
  if (extraction && Object.keys(extraction).length) {
    li.appendChild(el("span", "ev extracted", `LLM read: ${JSON.stringify(extraction)}`));
  }
  for (const e of events) {
    const blocked = (e.type === "gate" && !e.passed) || e.type === "guard";
    li.appendChild(el("span", `ev ${e.type === "transition" ? "transition" : ""} ${blocked ? "blocked" : ""}`, describe(e)));
  }
  ol.prepend(li);
}

async function refreshOutbox() {
  const res = await fetch(`/api/outbox?session_id=${encodeURIComponent(sessionId)}`);
  const mails = await res.json();
  $("outboxCard").hidden = !mails.length;
  const box = $("outbox");
  box.replaceChildren();
  for (const m of mails) {
    const d = el("div", "mail");
    d.appendChild(el("div", "hdr", `To: ${m.to} · ${m.subject}`));
    d.appendChild(el("pre", "", m.body));
    box.appendChild(d);
  }
}

function render(data) {
  renderPhase(data.phase);
  renderMemory(data.memory, data.turn);
  renderCounters(data.counters);
  if (data.events) {
    renderGates(data.events);
    addTrace(data.turn, data.events, data.extraction);
  }
  $("handoffCard").hidden = !data.handoff_packet;
  if (data.handoff_packet) $("handoff").textContent = JSON.stringify(data.handoff_packet, null, 2);
  $("input").disabled = data.ended;
  $("send").disabled = data.ended;
}

function showError(msg) {
  $("error").textContent = msg;
  $("error").hidden = !msg;
}

async function send(text) {
  if (sending || !text.trim()) return;
  sending = true;
  showError("");
  $("send").disabled = true;
  addMessage("caller", text);
  $("input").value = "";
  autosize();
  const typing = addMessage("agent typing", "Ava is typing…");
  try {
    const headers = { "content-type": "application/json" };
    const key = $("apiKey").value.trim();
    if (key) headers["x-api-key"] = key;
    const res = await fetch("/api/chat", {
      method: "POST",
      headers,
      body: JSON.stringify({ session_id: sessionId, message: text }),
    });
    const data = await res.json();
    typing.remove();
    if (!res.ok) {
      showError(data.detail || `Request failed (${res.status}).`);
      return;
    }
    addMessage("agent", data.reply, data.phase);
    render(data);
    await refreshOutbox();
  } catch (err) {
    typing.remove();
    showError("Could not reach the server. Is it running?");
  } finally {
    sending = false;
    if (!$("input").disabled) {
      $("send").disabled = false;
      $("input").focus();
    }
  }
}

function autosize() {
  const t = $("input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 140) + "px";
}

async function reset() {
  await fetch("/api/reset", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  }).catch(() => {});
  sessionId = newId();
  lastPhase = "VERIFY_ID";
  showWelcome();
  $("trace").replaceChildren(el("li", "empty", "Send a message to see what the controller did."));
  $("gates").replaceChildren(el("li", "empty", "Nothing yet."));
  $("outboxCard").hidden = true;
  $("handoffCard").hidden = true;
  showError("");
  render({ phase: "VERIFY_ID", memory: {}, counters: {}, turn: 0, ended: false, handoff_packet: null });
  $("input").focus();
}

async function loadConfig() {
  try {
    const cfg = await (await fetch("/api/config")).json();
    const badge = $("mode");
    if (cfg.fake_llm) {
      badge.textContent = "offline test mode (FakeLLM)";
      $("apiKey").placeholder = "not needed in test mode";
    } else {
      badge.textContent = cfg.model;
      if (cfg.server_has_key) $("apiKey").placeholder = "optional: server has a key";
    }
    if (cfg.today) {
      $("asof").textContent = `data date ${cfg.today}`;
      $("asof").hidden = false;
    }
  } catch { /* config is cosmetic */ }
}

$("composer").addEventListener("submit", (e) => { e.preventDefault(); send($("input").value); });
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send($("input").value); }
});
$("input").addEventListener("input", autosize);
$("reset").addEventListener("click", reset);
$("apiKey").value = store.get("sop-api-key");
$("apiKey").addEventListener("change", () => store.set("sop-api-key", $("apiKey").value.trim()));
document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => send(c.dataset.msg)));

showWelcome();
renderCounters({});
loadConfig();
