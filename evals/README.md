# Claims agent evals

Scenario scripts and a simulated caller, run against the HTTP server (`/api/chat`, `/api/state`, `/api/outbox`).
The harness uses the standard library only, so the scripted evals run without installing anything.
The simulated caller also needs the `anthropic` SDK and `ANTHROPIC_API_KEY`.
Data comes from the starter fixtures in `app/data/starter/` (Margaret Chen P9 with CL-2048 and CL-2011, Ma Tian P12 with CL-3001, representative David Chen).

## Running

Start a server first, then run the evals from the repo root.

```sh
SOP_LLM=fake uv run uvicorn app.server:app --port 8000            # deterministic FakeLLM, no key needed
python3 evals/run_evals.py --list                                  # coverage matrix, no server needed
python3 evals/run_evals.py --mode fake --base-url http://127.0.0.1:8000
python3 evals/run_evals.py --only mem1_deferred_question_full_path,V1
python3 evals/run_evals.py                                         # live model; uses the server's ANTHROPIC_API_KEY
uv run pytest -q evals/test_checks.py                              # unit tests, no server and no network
```

`--data` (or `SOP_DATA_DIR`) points at another fixture directory; the default is `app/data/starter/`.
Each run writes `evals/runs/<timestamp>/` with one transcript per scenario and `summary.md` (results plus the coverage matrix).

## What is graded

Grading reads state and events, not reply wording.
The checks are the phase, the `verified` flag, the proposed and selected claim, the memory slots, the counters, the gates and tool calls in the events, the mail outbox and `/api/state`.
Hard expectations FAIL.
Expectations under `soft` (reply wording, caller role, emotion events) only WARN, because they vary run to run or depend on features that land later.

Every turn of every scenario also runs these invariants:

- only allowed tools are called (R5);
- no claim facts or claim reads before the identity gate passes (V1);
- for a representative, no claim facts or claim reads before the policyholder's consent is approved (C1);
- phase changes follow the legal edges, and a verified session never returns to VERIFY_ID, except on a turn marked `switch` (W1);
- memory, the extraction, the transcript and the handoff packet never hold a raw ID number, DOB, phone or email, in values or quotes (P3);
- after verification, every claim ID, dollar amount and date in a reply exists in that caller's records, the guidance data, or what the caller said (R3).

Checks a script cannot prove (UI, README, clean-machine setup, the per-call trace file) are listed as manual in the coverage matrix.

## Scenarios

`evals/scenarios.py` holds the scripts.
The nine memory cases from `research/memory/09-memory-evals.md` are `mem1_...` to `mem8_...` (case 9 is the simulated run below).
Other V2 scenarios cover voice read-back, a representative with consent approved, consent timeout, an unlisted representative, a switch to another person's claim, and the off-topic offer at 2 and handoff at 3.
The turn and scenario keys are documented at the top of the file.
A turn can repeat with `until` and `max`, which is how the consent scenarios wait for the policyholder's answer without fixing the number of turns.

## Consent scenarios need a server setting

Consent comes back from `app/data/starter/consent_scenarios.json`, chosen by the server environment variable `CLAIMS_CONSENT_SCENARIO` (default `default`: pending, then approved).
The timeout scenario needs a second server that never gets an approval.

```sh
CLAIMS_CONSENT_SCENARIO=timeout SOP_LLM=fake uv run uvicorn app.server:app --port 8001
python3 evals/run_evals.py --mode fake --base-url http://127.0.0.1:8001 --consent-scenario timeout
```

`--consent-scenario` tells the runner which setting the server has.
It runs only the scenarios written for that setting, so the default run skips `rep_consent_timeout` and the timeout run skips everything else.

## Simulated caller (pass^k)

```sh
ANTHROPIC_API_KEY=... uv run python evals/run_evals.py --simulate --k 5 --base-url http://127.0.0.1:8000
```

A model plays the caller from a persona, a goal and the facts it knows (`SIM_SCENARIOS` in `scenarios.py`).
Its wording varies each run: a different speaking style is rotated in and the model samples at temperature 1.
The simulator calls the model directly with the `anthropic` SDK (`evals/simulate.py`), default model `claude-sonnet-5-5`, overridable with `--sim-model` or `EVAL_SIM_MODEL`.
Each scenario runs k times against the real server, the same invariants run on every turn, and the end state is graded by the scenario's `grade` expectations.
A scenario passes pass^k only if all k runs pass, and the output lists every run.
Without `ANTHROPIC_API_KEY` the run prints a skip message and exits 0.
The server behind it can be fake or live; a fake server tests the plumbing, a live one tests the agent.
Transcripts go to `evals/runs/<timestamp>/sim/`, the table to `summary-sim.md`.

## Other flags

`--send-key` passes `$ANTHROPIC_API_KEY` to the server as `X-API-Key`.
`--timeout` sets the per-request timeout in seconds.
The exit code is 0 when no scenario FAILs or ERRORs, and 1 otherwise.
