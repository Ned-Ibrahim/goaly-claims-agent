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

async function send(text, channel = "text") {
  if (sending || !text.trim()) return;
  sending = true;
  showError("");
  $("send").disabled = true;
  addMessage("caller", text);
  $("input").value = "";
  autosize();
  const typing = addMessage("agent typing", "Ava is typing…");
  const heard = takeHeard();
  try {
    const headers = { "content-type": "application/json" };
    const key = $("apiKey").value.trim();
    if (key) headers["x-api-key"] = key;
    const res = await fetch("/api/chat", {
      method: "POST",
      headers,
      body: JSON.stringify({ session_id: sessionId, message: text, channel, heard }),
    });
    const data = await res.json();
    typing.remove();
    if (!res.ok) {
      showError(data.detail || `Request failed (${res.status}).`);
      return;
    }
    const bubble = addMessage("agent", data.reply, data.phase);
    render(data);
    Voice.speak(data.reply, bubble, data.ended);
    await refreshOutbox();
  } catch (err) {
    typing.remove();
    showError("Could not reach the server. Is it running?");
  } finally {
    sending = false;
    Voice.flush();
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
  Voice.reset();
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

/* ---- voice: on-device recognition, local voices only (decisions 3a to 3d, 4a, 4b) ---- */

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const LANG = "en-US";
const SILENCE_MS = 1300;        // end of turn after normal replies
const SILENCE_NUMBER_MS = 2800; // longer after the agent asked for a date or number
const ASKS_NUMBER = /\b(date of birth|birth ?date|birthday|dob|phone|number|digits|last (four|4)|ssn|id|policy|claim|zip|code)\b/i;

let pendingHeard = null; // spoken part of an interrupted reply, sent with the next message
function takeHeard() { const h = pendingHeard; pendingHeard = null; return h; }

const Voice = {
  on: false, rec: null, stream: null, track: null,
  buffer: "", timer: null, silenceMs: SILENCE_MS,
  utter: null, bubble: null, fullText: "", charIndex: 0, endAfter: false,
  noVoiceSaid: false, usable: false,

  note(msg) { $("voiceNote").textContent = msg || ""; },

  async init() {
    $("mic").addEventListener("click", () => (this.on ? this.stop() : this.start()));
    $("skip").addEventListener("click", () => this.cancelSpeech(false));
    $("installSpeech").addEventListener("click", () => this.install());
    $("pauseMic").addEventListener("change", () => this.applyMute());
    try { window.speechSynthesis?.addEventListener?.("voiceschanged", () => {}); } catch { /* optional */ }
    await this.check();
  },

  disable(msg) {
    this.usable = false;
    $("mic").disabled = true;
    $("installSpeech").hidden = true;
    this.note(msg || "Voice needs on-device speech in this browser; please type.");
  },

  async check() {
    try {
      if (!SR || !("processLocally" in SR.prototype) || typeof SR.available !== "function"
          || !navigator.mediaDevices?.getUserMedia) {
        return this.disable();
      }
      const status = await SR.available({ langs: [LANG], processLocally: true });
      if (status === "available") {
        this.usable = true;
        $("mic").disabled = false;
        $("installSpeech").hidden = true;
        this.note("");
      } else if (status === "downloadable" && typeof SR.install === "function") {
        this.usable = false;
        $("mic").disabled = true;
        $("installSpeech").hidden = false;
        this.note("On-device speech needs a one-time language pack download.");
      } else {
        this.disable();
      }
    } catch { this.disable(); }
  },

  async install() {
    const btn = $("installSpeech");
    btn.disabled = true;
    this.note("Downloading the on-device speech pack...");
    try {
      await SR.install({ langs: [LANG], processLocally: true });
    } catch { /* checked below */ }
    btn.disabled = false;
    await this.check();
    if (this.usable) this.start();
    else this.disable();
  },

  async start() {
    if (!this.usable || this.on) return;
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      this.track = this.stream.getAudioTracks()[0] || null;
    } catch {
      this.note("Microphone access was not granted; please type.");
      return;
    }
    this.on = true;
    this.buildRecognition();
    this.startRecognition();
    $("mic").classList.add("on");
    $("mic").setAttribute("aria-pressed", "true");
    $("micLabel").textContent = "Listening";
    $("skip").hidden = false;
    this.note("Hands-free: just talk. Interrupt the agent any time.");
  },

  buildRecognition() {
    const rec = new SR();
    rec.lang = LANG;
    rec.processLocally = true;
    rec.continuous = true;
    rec.interimResults = true;
    rec.onspeechstart = () => this.onCallerSpeech();
    rec.onresult = (e) => this.onResult(e);
    rec.onerror = (e) => this.onError(e);
    rec.onend = () => { if (this.on) setTimeout(() => this.startRecognition(), 150); };
    this.rec = rec;
  },

  startRecognition() {
    if (!this.on || !this.rec) return;
    const live = this.track && this.track.readyState === "live";
    try {
      if (live) {
        try { this.rec.start(this.track); return; }
        catch (err) { if (err && err.name === "InvalidStateError") return; }
      }
      this.rec.start();
    } catch { /* already started, or retried on next end */ }
  },

  onError(e) {
    const code = e && e.error;
    if (code === "no-speech" || code === "aborted") return;
    if (code === "not-allowed" || code === "service-not-allowed" || code === "language-not-supported") {
      this.stop();
      this.note(code === "language-not-supported"
        ? "Voice needs on-device speech in this browser; please type."
        : "Microphone access was not granted; please type.");
    }
  },

  stop() {
    this.on = false;
    clearTimeout(this.timer);
    try { this.rec?.abort(); } catch { /* ignore */ }
    this.rec = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null; this.track = null;
    this.buffer = "";
    this.cancelSpeech(false);
    $("mic").classList.remove("on", "heard");
    $("mic").setAttribute("aria-pressed", "false");
    $("micLabel").textContent = "Voice";
    $("skip").hidden = true;
    clearInterim();
    this.note("");
  },

  get muted() { return $("pauseMic").checked && !!this.utter; },

  applyMute() {
    if (this.track) this.track.enabled = !this.muted;
  },

  // Caller started talking: barge in on the agent (3a).
  onCallerSpeech() {
    $("mic").classList.add("heard");
    if (this.utter && !this.muted) this.cancelSpeech(true);
  },

  onResult(e) {
    if (this.muted) return;
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const r = e.results[i];
      const t = r[0].transcript;
      if (r.isFinal) { this.buffer = (this.buffer + " " + t).trim(); }
      else interim += t;
    }
    if (interim || this.buffer) this.onCallerSpeech();
    showInterim((this.buffer + " " + interim).trim());
    clearTimeout(this.timer);
    if (this.buffer && !interim) this.timer = setTimeout(() => this.flush(), this.silenceMs);
    else if (this.buffer) this.timer = setTimeout(() => this.flush(), this.silenceMs + 1500);
  },

  // Send accumulated final results as one voice message.
  flush() {
    clearTimeout(this.timer);
    if (!this.buffer) return;
    if (sending) { this.timer = setTimeout(() => this.flush(), 300); return; }
    const text = this.buffer;
    this.buffer = "";
    $("mic").classList.remove("heard");
    clearInterim();
    send(text, "voice");
  },

  reset() {
    clearTimeout(this.timer);
    this.buffer = "";
    pendingHeard = null;
    this.cancelSpeech(false);
    clearInterim();
  },

  pickVoice() {
    const voices = window.speechSynthesis.getVoices().filter((v) => v.localService);
    return voices.find((v) => v.lang === LANG) || voices.find((v) => /^en/i.test(v.lang)) || null;
  },

  speak(text, bubble, ended) {
    this.silenceMs = ASKS_NUMBER.test(text) ? SILENCE_NUMBER_MS : SILENCE_MS;
    if (!this.on) return;
    if (!window.speechSynthesis) return;
    const voice = this.pickVoice();
    if (!voice) {
      if (!this.noVoiceSaid) {
        this.noVoiceSaid = true;
        this.note("No on-device voice found, so replies are text only.");
      }
      return;
    }
    this.cancelSpeech(false);
    const u = new SpeechSynthesisUtterance(text);
    u.voice = voice;
    u.lang = voice.lang;
    this.utter = u; this.bubble = bubble; this.fullText = text; this.charIndex = 0; this.endAfter = !!ended;
    u.onboundary = (ev) => { if (this.utter === u) this.charIndex = ev.charIndex; };
    const done = () => {
      if (this.utter !== u) return;
      this.utter = null; this.bubble = null;
      this.applyMute();
      if (this.endAfter) this.stop();
    };
    u.onend = done;
    u.onerror = done;
    window.speechSynthesis.speak(u);
    this.applyMute();
  },

  // Stop speech. interrupted=true records the heard part for the next message (3b, 3c).
  cancelSpeech(interrupted) {
    const u = this.utter;
    if (!u) { try { window.speechSynthesis?.cancel(); } catch { /* ignore */ } return; }
    this.utter = null;
    try { window.speechSynthesis.cancel(); } catch { /* ignore */ }
    this.applyMute();
    if (interrupted) {
      pendingHeard = this.fullText.slice(0, this.charIndex).trim();
      if (this.bubble) {
        this.bubble.classList.add("was-interrupted");
        this.bubble.appendChild(el("span", "interrupted", "(interrupted)"));
      }
    }
    this.bubble = null;
  },
};

function showInterim(text) {
  let n = $("log").querySelector(".msg.interim");
  if (!text) { if (n) n.remove(); return; }
  if (!n) { n = el("div", "msg caller interim"); $("log").appendChild(n); }
  n.textContent = text;
  $("log").scrollTop = $("log").scrollHeight;
}
function clearInterim() { showInterim(""); }

$("composer").addEventListener("submit", (e) => { e.preventDefault(); send($("input").value, "text"); });
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send($("input").value, "text"); }
});
$("input").addEventListener("input", autosize);
$("reset").addEventListener("click", reset);
$("apiKey").value = store.get("sop-api-key");
$("apiKey").addEventListener("change", () => store.set("sop-api-key", $("apiKey").value.trim()));
document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => send(c.dataset.msg, "text")));

showWelcome();
renderCounters({});
loadConfig();
Voice.init();
