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
    ["off_topic", "off-topic questions", "offer at 2, handoff at 3"],
    ["resistance", "resisted steps", "explain at 1, offer at 2, handoff at 3"],
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
    case "resistance": return `resisted ${e.step} step (#${e.count})`;
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
  let streaming = false;
  try {
    const headers = { "content-type": "application/json" };
    const key = $("apiKey").value.trim();
    if (key) headers["x-api-key"] = key;
    const body = JSON.stringify({ session_id: sessionId, message: text, channel, heard });
    if (channel === "voice" && Voice.on) {
      streaming = true;
      const sres = await fetch("/api/chat/stream", { method: "POST", headers, body });
      if (sres.status !== 404) { // 404: an older server without the stream endpoint
        await readChatStream(sres, typing);
        return;
      }
    }
    const res = await fetch("/api/chat", { method: "POST", headers, body });
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
    if (streaming) Voice.finish(false);
  } finally {
    sending = false;
    Voice.flush();
    if (!$("input").disabled) {
      $("send").disabled = false;
      $("input").focus();
    }
  }
}

// Voice turns: read the ndjson stream, speak sentences as they arrive, then render the final result.
async function readChatStream(res, typing) {
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail; } catch { /* not json */ }
    typing.remove();
    showError(detail || `Request failed (${res.status}).`);
    return;
  }
  let bubble = null;
  let textNode = null;
  let streamed = "";
  let finished = false;
  const handle = async (msg) => {
    if (msg.type === "sentence") {
      if (!bubble) {
        typing.remove();
        bubble = addMessage("agent", "");
        textNode = bubble.firstChild || bubble.appendChild(document.createTextNode(""));
        Voice.beginReply(bubble);
      }
      streamed = streamed ? `${streamed} ${msg.text}` : msg.text;
      textNode.data = streamed;
      $("log").scrollTop = $("log").scrollHeight;
      Voice.enqueue(msg.text);
    } else if (msg.type === "done") {
      finished = true;
      typing.remove();
      const spoke = !!bubble;
      if (!bubble) {
        bubble = addMessage("agent", msg.reply);
        textNode = bubble.firstChild;
      } else {
        textNode.data = msg.reply;
      }
      if (msg.phase) textNode.after(el("span", "meta", msg.phase));
      render(msg);
      if (spoke) Voice.streamDone(msg.reply, bubble, msg.ended);
      else Voice.speak(msg.reply, bubble, msg.ended);
      await refreshOutbox();
    } else if (msg.type === "error") {
      finished = true;
      typing.remove();
      showError(msg.detail || "The reply failed.");
      Voice.finish(false);
    }
  };
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  const drain = async (final) => {
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (line) await handle(JSON.parse(line));
    }
    if (final && buf.trim()) { const line = buf.trim(); buf = ""; await handle(JSON.parse(line)); }
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    await drain(false);
  }
  buf += dec.decode();
  await drain(true);
  if (!finished) {
    typing.remove();
    showError("The reply stream ended early.");
    Voice.finish(false);
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
      badge.textContent = `${cfg.provider}: ${cfg.extract_model} reads, ${cfg.respond_model} replies` + (cfg.effort ? ` (${cfg.effort})` : "");
      if (cfg.server_has_key) $("apiKey").placeholder = "optional: server has a key";
    }
    ttsMode = cfg.tts === "kokoro" ? "kokoro" : "browser";
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
const ASKS_NUMBER = /\b(date of birth|birth ?date|birthday|dob|phone|last (four|4)|digits|id number|zip|e-?mail)\b/i;
const VAD_EXTRA_NUMBER_MS = 1200; // extra wait after speech end when a date or number is expected
const VAD_SAFETY_MS = 1500;       // flush this long after the last final result if the VAD missed the end
const VAD_END_FLOOR_MS = 200;     // never flush sooner than this after speech end (non-digit questions)
const VAD_FLUSH_LATE_MS = 150;    // flush this long after a final result that arrives after the VAD end
const ORT_BASE = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.22.0/dist/";
const VAD_BASE = "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.31/dist/";
const MIN_SENTENCE = 25;
const MAX_TTS_INFLIGHT = 2;

let ttsMode = "browser"; // "kokoro" when the server can speak (config)
let pendingHeard = null; // spoken part of an interrupted reply, sent with the next message
function takeHeard() { const h = pendingHeard; pendingHeard = null; return h; }

// End at . ? ! followed by space or end; a piece under MIN_SENTENCE chars joins the next one.
function splitSentences(text) {
  const pieces = (text.match(/.+?(?:[.!?]+(?=\s|$)|$)/gs) || [text]).map((p) => p.trim()).filter(Boolean);
  const out = [];
  let carry = "";
  for (const p of pieces) {
    const cur = carry ? `${carry} ${p}` : p;
    if (cur.length < MIN_SENTENCE) carry = cur;
    else { out.push(cur); carry = ""; }
  }
  if (carry) out.push(carry);
  return out;
}

const Voice = {
  on: false, rec: null, stream: null, track: null,
  buffer: "", timer: null, silenceMs: SILENCE_MS,
  utter: null, audio: null, q: null, bubble: null, fullText: "", charIndex: 0, endAfter: false,
  noVoiceSaid: false, prefix: "", usable: false, ticket: 0,
  vad: null, vadEnded: false, vadEndAt: 0, interim: "", asksNumber: false,

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
    this.startVad(); // not awaited: the model download must not delay the recognizer
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
    rec.onspeechstart = () => $("mic").classList.add("heard");
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
    this.stopVad();
    try { this.rec?.abort(); } catch { /* ignore */ }
    this.rec = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null; this.track = null;
    this.buffer = ""; this.interim = ""; this.vadEnded = false;
    this.cancelSpeech(false);
    $("mic").classList.remove("on", "heard");
    $("mic").setAttribute("aria-pressed", "false");
    $("micLabel").textContent = "Voice";
    $("skip").hidden = true;
    clearInterim();
    this.note("");
  },

  get speaking() { return !!(this.utter || this.audio); },
  get fetching() { return !!this.q && !this.audio; },
  get muted() { return $("pauseMic").checked && this.speaking; },

  applyMute() {
    if (this.track) this.track.enabled = !this.muted;
  },

  // Silero VAD on the same echo-cancelled stream. Any failure leaves the recognizer-only behaviour.
  async startVad() {
    const stream = this.stream;
    let v = null;
    try {
      if (!window.vad || !window.vad.MicVAD) throw new Error("vad-web did not load");
      v = await window.vad.MicVAD.new({
        model: "v5",
        minSpeechMs: 300,
        redemptionMs: 700,
        getStream: async () => stream,
        pauseStream: async () => {},          // never stop the tracks: SpeechRecognition shares this one
        resumeStream: async () => stream,
        onnxWASMBasePath: ORT_BASE,
        baseAssetPath: VAD_BASE,
        onSpeechStart: () => this.onVadStart(),
        onSpeechRealStart: () => this.onVadRealStart(),
        onSpeechEnd: () => this.onVadEnd(),
        onVADMisfire: () => { if (this.buffer) this.scheduleEndFlush(0); }, // re-arm only, vadEndAt stays
      });
      if (!this.on || this.stream !== stream) { v.destroy(); return; }
      this.vad = v;
      await v.start();
    } catch (err) {
      if (v) { try { v.destroy(); } catch { /* ignore */ } }
      this.vad = null;
      console.warn("VAD unavailable, using recognizer timers only:", err);
    }
  },

  stopVad() {
    const v = this.vad;
    this.vad = null;
    try { v?.destroy(); } catch { /* ignore */ }
  },

  // A new burst of speech cancels any flush that was waiting for the caller to finish.
  onVadStart() {
    $("mic").classList.add("heard");
    this.vadEnded = false;
    clearTimeout(this.timer);
  },

  // Only confirmed speech (past minSpeechMs) interrupts the agent.
  onVadRealStart() {
    if ((this.speaking || this.fetching) && !this.muted) this.cancelSpeech(true);
  },

  onVadEnd() {
    this.vadEnded = true;
    this.vadEndAt = Date.now();
    this.scheduleEndFlush(0);
  },

  // Flush once the recognizer has final text and nothing interim is pending.
  scheduleEndFlush(minDelay) {
    clearTimeout(this.timer);
    if (!this.buffer || this.interim) return;
    const extra = this.asksNumber ? VAD_EXTRA_NUMBER_MS - (Date.now() - this.vadEndAt) : 0;
    const delay = this.asksNumber ? Math.max(minDelay, extra, 0) : Math.max(minDelay, VAD_END_FLOOR_MS);
    this.timer = setTimeout(() => this.flush(), delay);
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
    this.interim = interim.trim();
    const text = (this.buffer + " " + interim).trim();
    if (text) $("mic").classList.add("heard");
    // Without the VAD, a single noisy word must not cut the agent off.
    if (!this.vad && text.split(/\s+/).length >= 2 && (this.speaking || this.fetching)) this.cancelSpeech(true);
    showInterim(text);
    clearTimeout(this.timer);
    if (this.vad) {
      if (this.vadEnded) this.scheduleEndFlush(VAD_FLUSH_LATE_MS);
      else if (this.buffer) this.timer = setTimeout(() => this.flush(), this.interim ? VAD_SAFETY_MS * 2 : VAD_SAFETY_MS);
      return;
    }
    if (this.buffer && !interim) this.timer = setTimeout(() => this.flush(), this.silenceMs);
    else if (this.buffer) this.timer = setTimeout(() => this.flush(), this.silenceMs + 1500);
  },

  // Send accumulated final results as one voice message.
  flush() {
    clearTimeout(this.timer);
    if (!this.buffer) return;
    if (sending) { this.timer = setTimeout(() => this.flush(), 300); return; }
    const text = this.buffer;
    this.buffer = ""; this.interim = ""; this.vadEnded = false;
    $("mic").classList.remove("heard");
    clearInterim();
    if (this.speaking || this.fetching) this.cancelSpeech(true);
    send(text, "voice");
  },

  reset() {
    clearTimeout(this.timer);
    this.buffer = ""; this.interim = ""; this.vadEnded = false;
    pendingHeard = null;
    this.cancelSpeech(false);
    clearInterim();
  },

  pickVoice() {
    const voices = window.speechSynthesis.getVoices().filter((v) => v.localService);
    return voices.find((v) => v.lang === LANG) || voices.find((v) => /^en/i.test(v.lang)) || null;
  },

  setAsks(text) {
    this.asksNumber = ASKS_NUMBER.test(text);
    this.silenceMs = this.asksNumber ? SILENCE_NUMBER_MS : SILENCE_MS;
  },

  speak(text, bubble, ended) {
    this.setAsks(text);
    if (!this.on) return;
    if (ttsMode === "kokoro") return this.speakServer(text, bubble, ended);
    this.speakBrowser(text, bubble, ended);
  },

  // Server voice (Kokoro): sentences are fetched ahead (two at a time) and played in order.
  // A newer reply or a skip bumps the ticket, which drops everything in flight.
  speakServer(text, bubble, ended) {
    this.beginReply(bubble);
    this.fullText = text;
    for (const s of splitSentences(text)) this.enqueue(s);
    this.finish(ended);
  },

  // Open an empty reply queue. A streamed reply fills it with enqueue() and closes it with finish().
  beginReply(bubble) {
    if (!this.on || ttsMode !== "kokoro") return;
    this.stopReply();
    this.q = { ticket: this.ticket, items: [], pos: 0, inflight: 0, finished: false, played: [] };
    this.bubble = bubble; this.fullText = ""; this.endAfter = false; this.prefix = "";
    this.applyMute();
  },

  // The streamed reply is complete. Kokoro speaks what was queued; the browser voice speaks it whole now.
  streamDone(text, bubble, ended) {
    this.setAsks(text);
    if (!this.on) return;
    if (ttsMode !== "kokoro") return this.speak(text, bubble, ended);
    if (this.q) { this.fullText = text; this.finish(ended); } // else interrupted: nothing left to speak
  },

  // Add one sentence to the current reply. A streamed reply can call this as sentences arrive.
  enqueue(text) {
    if (!this.q) return;
    this.q.items.push({ text, state: "new", url: null, audio: null });
    this.pump();
  },

  // No more sentences are coming.
  finish(ended) {
    if (!this.q) return;
    if (ended !== undefined) this.endAfter = !!ended;
    this.q.finished = true;
    this.tryPlay();
  },

  pump() {
    const q = this.q;
    if (!q) return;
    for (const item of q.items) {
      if (q.inflight >= MAX_TTS_INFLIGHT) break;
      if (item.state === "new") this.fetchItem(q, item);
    }
  },

  async fetchItem(q, item) {
    item.state = "fetching";
    q.inflight++;
    let blob = null;
    try {
      const res = await fetch("/api/tts", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ text: item.text }),
      });
      if (!res.ok) throw new Error(`tts ${res.status}`);
      blob = await res.blob();
    } catch { /* marked failed below */ }
    q.inflight--;
    if (q.ticket !== this.ticket) return; // skipped or superseded
    if (blob) {
      item.url = URL.createObjectURL(blob);
      item.audio = new Audio(item.url);
      item.audio.preload = "auto";
      item.state = "ready";
    } else {
      item.state = "failed";
    }
    this.pump();
    this.tryPlay();
  },

  // Play the next sentence if it is ready and nothing else is speaking.
  tryPlay() {
    const q = this.q;
    if (!q || this.audio) return;
    if (q.pos >= q.items.length) { if (q.finished) this.replyDone(); return; }
    const item = q.items[q.pos];
    if (item.state === "failed") return this.fallbackBrowser();
    if (item.state !== "ready") return;
    const a = item.audio;
    const next = () => {
      if (this.audio !== a || this.q !== q) return;
      a.onended = null; a.onerror = null;
      URL.revokeObjectURL(item.url);
      this.audio = null;
      q.played.push(item.text);
      q.pos++;
      this.tryPlay();
    };
    a.onended = next;
    a.onerror = next;
    this.audio = a;
    this.applyMute();
    a.play().catch(() => {
      if (this.audio !== a || this.q !== q) return;
      a.onended = null; a.onerror = null;
      this.audio = null;
      item.state = "failed";
      this.fallbackBrowser();
    });
  },

  replyDone() {
    this.q = null;
    this.bubble = null;
    this.applyMute();
    if (this.endAfter) this.stop();
  },

  // A sentence could not be fetched or played: speak the rest with the browser voice.
  fallbackBrowser() {
    const q = this.q;
    const rest = q.items.slice(q.pos).map((i) => i.text).join(" ");
    const { bubble, endAfter } = this;
    const prefix = q.played.join(" ");
    this.stopReply();
    this.speakBrowser(rest, bubble, endAfter, prefix);
  },

  // Drop the current reply (audio, queue, browser utterance).
  stopReply() {
    this.ticket++;
    const q = this.q;
    this.q = null;
    if (q) for (const i of q.items) if (i.url) URL.revokeObjectURL(i.url);
    const a = this.audio;
    this.audio = null;
    if (a) { a.onended = null; a.onerror = null; try { a.pause(); } catch { /* ignore */ } }
    if (this.utter) {
      this.utter = null;
      try { window.speechSynthesis.cancel(); } catch { /* ignore */ }
    }
    this.bubble = null;
  },

  // Heard part of an audio reply: cut the text at the played fraction, back to a word boundary.
  heardFromAudio(a, text) {
    const frac = a.duration > 0 ? Math.min(1, a.currentTime / a.duration) : 0;
    const cut = text.slice(0, Math.floor(text.length * frac));
    if (frac >= 1 || /\s$/.test(cut) || text[cut.length] === undefined || /\s/.test(text[cut.length])) return cut.trim();
    return cut.replace(/\s*\S*$/, "").trim();
  },

  speakBrowser(text, bubble, ended, prefix = "") {
    // No way to speak: a reply that ended the call must still stop the mic.
    const giveUp = () => { if (ended) setTimeout(() => this.stop(), 0); };
    if (!window.speechSynthesis) return giveUp();
    const voice = this.pickVoice();
    if (!voice) {
      if (!this.noVoiceSaid) {
        this.noVoiceSaid = true;
        this.note("No on-device voice found, so replies are text only.");
      }
      return giveUp();
    }
    this.stopReply();
    const u = new SpeechSynthesisUtterance(text);
    u.voice = voice;
    u.lang = voice.lang;
    this.utter = u; this.bubble = bubble; this.fullText = text; this.prefix = prefix; this.charIndex = 0; this.endAfter = !!ended;
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

  // Heard part of a queued reply: fully played sentences plus the played fraction of the current one.
  heardFromQueue(q) {
    const parts = [...q.played];
    const item = q.items[q.pos];
    if (this.audio && item) parts.push(this.heardFromAudio(this.audio, item.text));
    return parts.join(" ").trim();
  },

  // Stop speech. interrupted=true records the heard part for the next message (3b, 3c).
  cancelSpeech(interrupted) {
    const q = this.q;
    if (q) {
      if (interrupted) pendingHeard = this.heardFromQueue(q);
      const bubble = this.bubble;
      this.stopReply();
      this.applyMute();
      if (interrupted && bubble) {
        bubble.classList.add("was-interrupted");
        bubble.appendChild(el("span", "interrupted", "(interrupted)"));
      }
      return;
    }
    const u = this.utter;
    if (!u) { try { window.speechSynthesis?.cancel(); } catch { /* ignore */ } this.bubble = null; return; }
    this.utter = null;
    try { window.speechSynthesis.cancel(); } catch { /* ignore */ }
    this.applyMute();
    if (interrupted) {
      pendingHeard = `${this.prefix} ${this.fullText.slice(0, this.charIndex)}`.trim();
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
