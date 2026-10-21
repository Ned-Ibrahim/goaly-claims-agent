# Two native model adapters, chosen by the key the reviewer brings

Setup must accept an API token for "an AI model", so a reviewer may bring a key from a provider other than Anthropic.
We decided on a small model interface (extract returns a typed form, respond returns text) with two adapters, Anthropic and OpenAI, each using its own SDK's native structured outputs. The key type picks the adapter, model names are settings, and the OpenAI adapter takes an optional base URL so OpenAI-compatible providers also work.

## Considered Options

- One OpenAI-SDK client for every provider: Anthropic's compatibility layer "is not considered a long-term or production-ready solution" and ignores `response_format` and `strict`, so the extract call's typed form would not be guaranteed on Claude.
- A multi-provider library (LiteLLM): widest coverage for the least code, but structured-output behavior varies by provider and it adds a layer between the controller and the model.
- Anthropic only: least work, but a reviewer without an Anthropic key could not run the agent.
