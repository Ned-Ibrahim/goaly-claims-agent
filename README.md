# Goaly claims support agent

A procedure harness for an insurance claims support agent.
Code owns the procedure: it decides every Phase change, every safety gate and every tool call.
The model does only two jobs, reading each caller message into a structured form and phrasing the reply inside limits that code sets.
The test page offers text chat and browser voice, and shows the Phase, the gate results, the stored Slots and the controller trace next to the conversation.

## Quick start

You need either [uv](https://docs.astral.sh/uv/) or Docker.
Open http://localhost:8000 in Chrome when the server is up.

### With uv

```sh
cd claims-agent
uv sync
SOP_LLM=fake uv run uvicorn app.server:app --port 8000
```

`SOP_LLM=fake` runs a deterministic stand-in for the model, so no key and no network are needed.
For a live model, copy the example settings and add your own key:

```sh
cp .env.example .env
# edit .env: set ANTHROPIC_API_KEY (or CLAIMS_PROVIDER=openai with OPENAI_API_KEY and both model names)
uv run uvicorn app.server:app --port 8000
```

You can also leave the key out of `.env` and paste it into the field at the top of the page.
The page sends it as an `X-API-Key` header, and the server does not store or log it.

To run on OpenAI instead, start the server with `CLAIMS_PROVIDER=openai CLAIMS_EXTRACT_MODEL=gpt-4.1-mini CLAIMS_RESPOND_MODEL=gpt-4.1-mini` and paste an OpenAI key.

### With Docker

Fake mode, no key:

```sh
cd claims-agent
echo SOP_LLM=fake > .env
docker compose up --build
```

Live mode: `cp .env.example .env`, fill in your key, then `docker compose up --build`.
Masked trace files appear in `./traces`.

### Voice and typing

Typing always works.
Voice needs Chrome with on-device speech recognition.
On first use the page offers to install the on-device language pack (the "Install on-device speech" button).
If on-device recognition is not available, voice stays off and the page says why.
Recognition runs on the device and replies are spoken with a local system voice, so neither the caller's audio nor the spoken reply goes to a speech service.
Microphone access works on `localhost` without HTTPS.

Turn-taking uses Silero VAD in the browser (`@ricky0123/vad-web` 0.0.31, loaded from jsdelivr; the audio stays on the device).
About 0.3 s of real speech interrupts the agent, so a cough or a short "mm" does not.
The caller's turn ends about 0.9 s after they stop talking, or about 2.1 s after a question that asks for a date, phone, email or digits.
Voice turns use `POST /api/chat/stream`: the reply streams from the model, each sentence passes the same gates before it is sent, and the page speaks it while the next one is written.

#### Natural voice (optional)

By default the agent speaks with the browser's local voice, and its quality depends on your operating system.
For the same natural voice everywhere, the server can speak with Kokoro, an open model that runs on your machine.
Reply text never leaves the server.

```bash
uv sync --extra voice
uv run python scripts/get_voice_model.py
```

The script downloads about 142 MB into `models/` and skips files you already have.
Restart the server and the page uses Kokoro automatically.
If the package or the files are missing, or a request fails, the browser's local voice speaks instead.

#### Realtime voice (OpenAI)

The "Realtime voice" button next to "Voice" starts a speech-to-speech call.
OpenAI's `gpt-realtime` model listens, handles turn-taking and interruptions, and speaks.
Our code stays the brain: every caller turn goes through `/api/chat` (Phases, gates, PII masking), and the model only speaks the reply the server returns.
To use it, paste an OpenAI API key with Realtime access into the key field (or set `OPENAI_API_KEY`), then click the button and allow the microphone.
Clicking it again, or finishing the call, closes the connection and the mic.
Realtime and the on-device Voice mode cannot run together, and `CLAIMS_REALTIME_MODEL` overrides the model.

The trade-off: the caller's raw audio goes to OpenAI's realtime model, identity details included, which the on-device Voice mode avoids.
Verification, claim access and every reply still come from our code.

## How one caller turn flows

```text
Caller speaks or types
  |
  v
Browser speech to text (on-device; skipped when typing)
  |
  v
PII parser (server, code)       Identity details are pulled out and stored by code;
  |                             the text now holds Placeholders such as [DOB given]
  v
Extract call (model)            Placeholder text in, structured form out:
  |                             intent, claim hints, five Emotion readings, scope
  v
Controller (code)               Verifies identity, moves the Phase, calls the claim tools,
  |                             decides what data the reply may use
  v
Reply call (model)              Writes the reply from the allowed data only
  |
  v
Reply check (code)              Blocks leaked identity values and any ID, amount or date
  |                             not found in the data handed over; rewrites once, then uses a safe line
  v
Browser text to speech (local voice; skipped when typing)
```

Speech stays at the channel edge, so the controller only ever sees text (see ADR 0001).
Voice-captured Identity details get a Read-back before the check runs.

## The four Phases

| Phase | What happens | Who decides |
|---|---|---|
| VERIFY_ID | The caller proves who they are with 3 of 5 Identity details (name, date of birth, phone, email, ID last four) that match one record with no conflicts. Nothing about any claim is said before this passes. Anything the caller says early (a claim hint, a question) is kept as a Slot. | Code |
| RESOLVE_INTENT | The agent picks the claim, using the remembered hint and the verified person's own records, and asks only if it is still unclear. | Code picks the options, the model reads messy wording |
| PROCESS_CASE | The agent answers questions about the claim in natural language, using only the claim fields code hands over. | The model phrases, code limits the data |
| POST_PROCESS | The agent offers a summary email. The caller opts in or skips. The email is written by code from a fixed template and goes only to the address on the Policyholder's file. Sending is mocked into an outbox. | Code |

Off-topic questions are declined in one sentence and the agent steers back to the current step.
The second off-topic message in a call offers a human, and the third hands off.

## The core case: Margaret, CL-2048

The caller says: "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472."

1. The PII parser stores the date of birth and ID last four, and the model sees Placeholders in their place. The name goes to the model, which reads it for the check.
2. The extract call records the hint (healthcare, denied, January) as Slots while the call is still in VERIFY_ID.
3. Code checks the identity: 3 or more details match record P9 and none conflict. The call moves to RESOLVE_INTENT.
4. The stored hint singles out CL-2048 (Margaret also has a closed January healthcare claim, CL-2011, which the word "denied" rules out). The agent proposes CL-2048 instead of asking "which claim?" from scratch.
5. In PROCESS_CASE the agent explains the denial reason, the documents needed and the appeal deadline, all taken from the claim record.
6. In POST_PROCESS the agent offers the summary email and respects a yes or a skip.

Deadlines are judged against a demo date of 2026-03-02 (set `SOP_TODAY` to change it), because on the real date every appeal deadline in the sample data has already passed.

## Emotional support and recovery

Each caller message gets an Emotion reading for frustration, anger, anxiety, confusion and refusal.
Each one is none, mild or strong, with the caller's own words that show it.
The agent responds as follows when the caller resists a required step:

1. First time: one sentence acknowledging the feeling, one sentence on why the step protects the caller, and the allowed options (any 3 of the 5 details, typing them, or a human).
2. Second time, or as soon as Frustration shown is true: offer a human.
3. Third time, a strong insult after the offer, or a request for a person: hand off with a packet that holds no identity values.

Anxiety and confusion change the tone (reassure with the next concrete step, one step at a time) but never trigger a handoff on their own.
No amount of frustration lowers the gate: claim details stay hidden until verification passes.

## Privacy

What never reaches the model:

- Identity details other than the name. Code parses them out of the caller's message first, so the model sees Placeholders, and verification takes no values from the model (ADR 0003).
- Claim data before verification.
- Claim fields the current answer does not need. Only the selected claim's needed fields are sent.

Other controls:

- Speech recognition and speech output run on the device, so audio and spoken text do not go to a speech vendor.
- An exact-match check runs on every prompt, reply and API response for the stored date of birth, phone, email and ID last four.
- Memory, traces and API responses carry masked values.

What production would need:

- A signed BAA with the model provider (HIPAA readiness) or zero data retention, because claim data still reaches the model after verification and healthcare claims carry health information.
- A cloud detection service or a gateway as a stronger second line than the in-process detector.

The sample data here is fictional.
See ADR 0003 for the decision.

## Tests and evals

```sh
uv run pytest -q                                   # unit and flow tests, fake model, no key
python3 evals/run_evals.py --list                  # which requirement each scenario covers, no server needed
SOP_LLM=fake uv run uvicorn app.server:app --port 8000 &
python3 evals/run_evals.py --mode fake             # scenarios against the running fake-mode server
```

In fake mode all 46 scenarios pass, and so does the consent timeout scenario (start the server with `CLAIMS_CONSENT_SCENARIO=timeout` and add `--consent-scenario timeout`).
The live models were tried by hand in the browser (Claude Haiku 5.5 and OpenAI GPT-4.1 mini); the eval suite has not been run against a live model, so no live scores are claimed here.
With a key, `python3 evals/run_evals.py --simulate --k 5` plays a model-written caller five times per scenario and scores pass^5.
See `evals/README.md` for the options.

## Known limits

- Every scored result above comes from the deterministic stand-in; live runs were by hand only.
- Voice was tried by hand with a real microphone. With Claude Haiku 5.5 at low effort, the first spoken sentence came 1.8 to 3.8 s after the caller's turn ended (3 turns, measured in the browser). The VAD thresholds are not tuned yet.
- A speech-to-speech model (such as OpenAI Realtime) would sound more natural, but it hears the caller's raw audio, identity details included, and speaks before code can check a sentence. That is why it is not the default here.
- Presidio is optional and stays off unless installed with the `presidio` extra. Without it, the parser's own patterns and the fail-closed sweep do the work.
- When a Representative describes a claim instead of giving its number, a hint that matches one claim starts a Consent request, while a hint that matches none gets "which claim number?". That difference tells the caller whether a matching claim exists.

## What we would do differently

**Measure the voice loop first.**
We built the whole pipeline and only then timed it.
The first live call had about 10 s of silence per turn: 2.8 s waiting for the caller to stop, 6.2 s in two model calls run back to back, and 1.3 s turning the whole reply into audio.
A voice activity detector, sentence-by-sentence streaming and a faster reply model brought the first spoken sentence down to 1.8 to 3.8 s on Claude Haiku.
Those belong in the first design, not in a fix after the fact.

**Pick the voice architecture up front, and price the privacy cost.**
A speech-to-speech model sounds the most natural, but it hears the caller's raw audio and talks before code can check what it says.
We kept text in the middle so identity details never reach a model, which is the right call for a claims line.
We should still have built both versions in the first hour and compared them on real calls instead of on paper.

**Use a real microphone on day one.**
Two problems only showed up live.
Speech recognition heard "Margaret Shin" for "Margaret Chen", and verification failed because the read-back covered digits and dates but not the name.
A short "Got it" clip played the moment the caller stopped, which sounds odd after "Hey, how's it going?".
The name is now read back and the clip is gone, and an hour of real calls on day one would have caught both.

**Run the live evals on a schedule.**
The 46 scripted scenarios run on a deterministic stand-in.
The simulated-caller run (`--simulate --k 5`) works against a live model, but nothing runs it regularly, so a model regression would reach a caller before it reached us.

## Using two models

The agent already splits each turn across two calls: one reads the caller's message into fields, the other writes the reply.
Two more splits would raise quality without slowing the call down.

1. **A voice model in front, this controller behind it.**
   A speech-to-speech model (OpenAI Realtime, for example) does the listening, turn-taking, interruptions and speaking, and passes every caller turn to this server as a tool call.
   Verification, the gates and the claim data stay in code.
   We have a working prototype of this on a separate branch; it is left out of this submission because it has not passed a full live test.
   The cost is the one above: the voice model hears the caller's raw audio, identity details included.
2. **A fast model for routine turns, a stronger one for hard turns.**
   Most turns are routine: a greeting, a read-back, a status question.
   When the controller sees a hard turn (conflicting details, strong emotion, a question about documents or deadlines), it sends that one turn to a stronger model and plays a short holding line while it works.
   The controller already knows which turns are hard, so the routing is a code decision, not something a model has to guess.

## Improving the system with a feedback loop

Every call leaves a masked trace in `traces/<session>.jsonl`: the phase, each memory write, every gate result, tool call and emotion reading.
That is enough to find the layer where a failure starts, and each layer has its own levers.

| Layer | What the failure looks like | Levers to change |
|---|---|---|
| Channel (speech in and out) | misheard name or digits, the caller cut off mid-sentence, the agent stopped by a cough, a long silence before the reply | read-back fields, voice detector thresholds, end-of-turn wait, speech engine, streaming |
| Controller (the SOP harness) | wrong phase change, a gate that blocks a real caller or lets a wrong one through, a detail asked for twice | gate rules, slot lifetimes, read-back rules, retry and handoff limits |
| Model | wrong field extracted, emotion misread, a reply that fails a gate and needs a retry, a slow turn | prompt, schema, model choice, effort, examples |
| Data and tools | claim not found, wrong document guideline, consent stuck pending | fixtures, tool contracts, timeouts |

The loop has four steps.

1. **Collect** every failed or slow turn from the traces, plus every call where the caller asked for a person.
2. **Locate** the first layer where the trace goes wrong. A gate that blocked a caller whose extracted details were right points at the controller. The same block with a wrong date in the extracted fields points at the model or the channel. Rules sort the clear cases, and a model can label the rest for a person to confirm.
3. **Change one lever** in that layer.
4. **Prove it.** Turn the failing call into a scripted scenario, then rerun the scripted evals and the simulated caller (pass^k). Keep the change only if the new scenario passes and nothing else drops.

Today's fixes went through this loop by hand.
The "Margaret Shin" failure started in the channel: the model and the controller did exactly what they should with a wrong name, so the lever was the read-back.
The 10 s silence came from the model layer and the channel together, so the levers were the reply model, streaming and the voice detector.
Automating the first two steps turns each batch of calls into a ranked list of fixes, each tied to the layer and the lever that owns it.

## Configuration

Set these in `.env` (see `.env.example`) or the shell.

| Variable | Default | Meaning |
|---|---|---|
| `SOP_LLM` | unset | `fake` uses the deterministic stand-in model, no key needed |
| `CLAIMS_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `ANTHROPIC_API_KEY` | unset | Key for the Anthropic provider (or paste it in the page) |
| `OPENAI_API_KEY` | unset | Key for the OpenAI provider (or paste it in the page) |
| `OPENAI_BASE_URL` | unset | Base URL for an OpenAI-compatible provider |
| `CLAIMS_EXTRACT_MODEL` | `claude-haiku-5-5` (anthropic), required for openai | Model for the extract call |
| `CLAIMS_RESPOND_MODEL` | `claude-haiku-5-5` (anthropic), required for openai | Model for the reply call |
| `CLAIMS_EFFORT` | `low` | Reply-call effort for anthropic: low, medium, high, xhigh, max |
| `CLAIMS_CONSENT_SCENARIO` | `default` | `default` approves on the second check, `timeout` never approves |
| `CLAIMS_TTS` | auto | Set to `browser` to force the browser voice even when Kokoro is installed |
| `CLAIMS_TTS_MODEL` | `models/kokoro-v1.0.int8.onnx` | Kokoro model file |
| `CLAIMS_TTS_VOICES` | `models/voices-v1.0.bin` | Kokoro voices file |
| `CLAIMS_REALTIME_MODEL` | `gpt-realtime` | Model for Realtime voice |
| `CLAIMS_TTS_VOICE` | `af_heart` | Kokoro voice name |
| `SOP_TODAY` | 2026-03-02 | Date deadlines are judged against, or `real` for the system clock |
| `SOP_DATA_DIR` | `app/data/starter` | Folder with the sample JSON files |
| `PII_PRESIDIO` | `1` | `0` turns off the optional Presidio detector (it runs only if installed with the `presidio` extra) |
| `CLAIMS_TRACE_DIR` | `traces` | Folder for the masked per-call trace files |
| `SOP_DEBUG_TRACE` | unset | `1` adds match and conflict counts to the identity gate trace line |

`GET /api/config` shows the provider, models and date the running server uses.

## More

- [docs/design.md](docs/design.md): the decisions behind the design, step by step.
- [CONTEXT.md](CONTEXT.md): the glossary this project uses.
- [ADR 0001](docs/adr/0001-speech-at-the-channel-edge.md): speech stays at the channel edge.
- [ADR 0002](docs/adr/0002-two-native-model-adapters.md): two native model adapters.
- [ADR 0003](docs/adr/0003-identity-details-never-reach-the-model.md): identity details never reach the model.
