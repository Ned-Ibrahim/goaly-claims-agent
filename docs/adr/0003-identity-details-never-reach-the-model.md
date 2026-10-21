# Identity details never reach the model

Callers prove who they are with a date of birth, ID last four, phone, email and name, and the model provider (whichever key the reviewer brings) is outside our control.
We decided code parses identity details out of each message before any model call, keeps them in a server-side store, and gives the model only placeholders such as `[DOB given]`. Verification runs in code and takes no values from the model.
An egress check blocks any stored value, and the matched record's name and aliases, in every prompt, reply and API response. An in-process Presidio detector is a second line for formats the parser misses, and a typed field is the fallback when a spoken value cannot be read.

## Considered Options

- Send messages as they are and rely on the provider's terms (Anthropic deletes API inputs and outputs within 30 days and does not train on them): the most natural, but identity values would still sit with a provider the reviewer chooses.
- A typed identity form: the strongest privacy, but verification would stop being a conversation, which the task requires.
- A cloud detector or vault (Bedrock Guardrails, Google Sensitive Data Protection, Azure PII, Skyflow): strong detection, but a second account, key and network hop for the reviewer. These are the production path, not the demo.

## Consequences

Claim data still reaches the model after verification, because the model phrases the answer, so only the selected claim's needed fields are sent. A real carrier would also need a BAA or zero data retention with the model provider.
The same egress check closes V1's leaks of raw details in API responses.
