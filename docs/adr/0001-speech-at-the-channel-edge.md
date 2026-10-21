# Speech stays at the channel edge; the controller only sees text

The agent reaches callers by text chat and by browser voice today, and should be able to take a phone line later.
We decided the procedure controller takes text in and returns text out, and each channel converts speech to and from text at its own edge.
That way the reply guard checks every sentence before any caller hears it.

## Considered Options

- A server-side voice loop now (an open voice framework such as Pipecat or LiveKit Agents over WebRTC): closest to a production phone agent, but it needs paid speech-to-text and text-to-speech keys, and setup should need only the model key.
- A speech-to-speech model that calls the controller as tools: lowest latency, but the model's own speech reaches the caller unchecked, so grounding cannot stop a bad sentence before it is spoken.
- An enterprise contact-center platform: its own procedure language would replace the controller, which is the part this project exists to design.

## Consequences

A phone line later is an adapter in front of the same controller: an open framework process that sends transcripts in, or a managed voice platform calling the controller as a custom LLM endpoint.
Voice adds speech-to-text and text-to-speech time on top of the model calls, so the turn latency budget has to account for it.
