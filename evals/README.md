# SOP harness evals

Owner: career-ops-contacts-f4. Scenario scripts keyed to `../checklist.md` IDs, run against the `/api/chat` contract agreed with personal-67.
Standard library only (Python 3.9+), so they run without installing anything.

```sh
python3 evals/run_evals.py --list                 # which checklist IDs each scenario covers; no server needed
python3 evals/run_evals.py --mode fake            # against the FakeLLM server; skips live_only scenarios
python3 evals/run_evals.py                        # live model; uses the server's ANTHROPIC_API_KEY
python3 evals/run_evals.py --only V1,margaret_full_path_email_yes
python3 -m unittest discover -s evals             # unit tests for the leak/grounding helpers
```

Each run writes `evals/runs/<timestamp>/` with one transcript per scenario and `summary.md` (results plus the coverage matrix).

Hard expectations FAIL. Expectations under `soft` (reply wording, LLM labels) only WARN, because they vary run to run.
Every turn of every scenario also runs these invariants:

- only allowed tools are called (R5);
- no claim facts or claim reads before the identity gate passes (V1);
- phase changes follow the legal edges, and a verified session never returns to VERIFY_ID (W1);
- memory, the extraction and the handoff packet never hold a raw SSN, DOB or phone number, in values or quotes (P3);
- after verification, every claim ID, code, dollar amount and date in a reply exists in that caller's records, the knowledge base, or what the caller said (R3).

Checks a script cannot prove (UI, README, clean-machine setup) are listed as manual in the coverage matrix.
