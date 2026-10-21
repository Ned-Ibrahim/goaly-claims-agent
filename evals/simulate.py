"""Model-played caller for the simulated eval mode (decision 12a).

The caller model gets a persona, a goal and the facts it knows, and talks to the real server.
Grading never reads the caller's wording: run_evals.py scores the same invariants and the
scenario's `grade` expectations on state and events, so a run passes or fails on what the agent did.

Only `anthropic_complete` touches the network, and it imports the SDK lazily. Tests pass a stub
`complete(system, messages) -> str` instead.
"""
from __future__ import annotations

import os
from typing import Callable

DEFAULT_MODEL = os.environ.get("EVAL_SIM_MODEL", "claude-sonnet-5-5")
END = "[END]"

# Wording styles rotated across runs so pass^k sees varied callers. None of them refuses or insults,
# because the tau2-bench lesson is that an unconstrained simulated user pollutes the results.
STYLES = [
    "terse and hurried, short sentences",
    "chatty and polite, mentions small details of your day",
    "worried about the bill, a little anxious but cooperative",
    "slightly confused, sometimes asks the agent to repeat or explain a term",
    "casual, mostly lowercase, uses everyday slang",
    "formal and precise, full sentences",
]

RULES = (
    "You are playing a person on a call with an insurance claims support agent. Stay in character.\n"
    "Write only the caller's next message: plain spoken words, no stage directions, no quotes, no labels.\n"
    "Only use the facts listed under FACTS. Never invent a detail you were not given; if asked for something "
    "not listed, say you do not have it handy.\n"
    "Give identity details only when the agent asks for them or when your goal says to, and give what is asked "
    "(any three of your details is enough if the agent asks for three).\n"
    "Follow the BEHAVIOR script in order, one step per message, and react naturally to what the agent just said.\n"
    f"When your goal is done, or the agent has handed you to a human or ended the call, reply with exactly {END}."
)


def build_system(spec: dict, style: str) -> str:
    return (
        f"{RULES}\n\nPERSONA: {spec['persona']}\nSPEAKING STYLE THIS CALL: {style}\n\n"
        f"FACTS:\n{spec['facts']}\n\nGOAL: {spec['goal']}\n\nBEHAVIOR:\n{spec['behavior']}\n"
    )


def style_for(run_idx: int) -> str:
    return STYLES[run_idx % len(STYLES)]


class Caller:
    """Holds the caller side of one simulated call."""

    def __init__(self, complete: Callable[[str, list[dict]], str], spec: dict, run_idx: int):
        self.complete = complete
        self.system = build_system(spec, style_for(run_idx))
        self.messages: list[dict] = [{"role": "user", "content": "The call connects. Say your first message to the agent."}]

    def next_message(self, agent_reply: str | None) -> str | None:
        """The caller's next line given the agent's last reply; None when the caller is done."""
        if agent_reply is not None:
            self.messages.append({"role": "user", "content": f"The agent said: {agent_reply}"})
        text = (self.complete(self.system, self.messages) or "").strip()
        if not text or text.startswith(END):
            return None
        self.messages.append({"role": "assistant", "content": text})
        return text[:1800]


def anthropic_complete(api_key: str, model: str = DEFAULT_MODEL) -> Callable[[str, list[dict]], str]:
    import anthropic  # lazy: the scripted evals need no SDK

    client = anthropic.Anthropic(api_key=api_key)

    def complete(system: str, messages: list[dict]) -> str:
        out = client.messages.create(model=model, max_tokens=300, temperature=1.0, system=system, messages=messages)
        return "".join(b.text for b in out.content if getattr(b, "type", "") == "text")

    return complete
