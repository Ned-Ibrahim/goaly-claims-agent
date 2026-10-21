"""PII tests: parsing of spoken and written forms, and nothing identity-shaped reaches the model."""

from __future__ import annotations

import json

import pytest

from app import pii
from app.agent import Agent
from app.llm import FakeLLM
from app.models import Session
from app.store import DataStore

MARGARET = DataStore().customers["P9"]
DOB, PHONE, EMAIL, LAST4 = MARGARET["dob"], MARGARET["phone"], MARGARET["email"], MARGARET["id_last4"]
RAW = [DOB, PHONE, EMAIL, LAST4]
MSG = (
    "My name is Margaret Chen, policy POL-9921, about my denied healthcare claim CL-2048 from "
    f"January. DOB is {DOB}, SSN last four is {LAST4}, phone {PHONE}, email {EMAIL}."
)


class Recording(FakeLLM):
    def __init__(self):
        self.prompts: list[str] = []

    def extract(self, system, user):
        self.prompts += [system, user]
        return super().extract(system, user)

    def respond(self, system, transcript):
        self.prompts += [system, *[t["content"] for t in transcript]]
        return super().respond(system, transcript)


# ---- parsing --------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("born March fifteenth nineteen eighty five", "1985-03-15"),
    ("my birthday is the fifteenth of March nineteen eighty-five", "1985-03-15"),
    ("DOB 3/15/1985", "1985-03-15"),
    ("date of birth 1985-03-15", "1985-03-15"),
    ("born 15 March 1985", "1985-03-15"),
    ("born March 15th, 1985", "1985-03-15"),
    ("I was born June twenty first two thousand five", "2005-06-21"),
    ("born march third nineteen oh five", "1905-03-03"),
])
def test_dates(text, expected):
    r = pii.parse_identity(text)
    assert r.values["dob"] == expected
    assert r.masked.endswith("[DOB given]") and "19" not in r.masked.replace("[DOB given]", "")


@pytest.mark.parametrize("text", [
    "my last four is four four oh two", "the last four of my social is 4 4 0 2",
    "SSN ending in 4402", "my national ID last 4 are four four zero two",
    "social security number 123-45-4402",
])
def test_id_last4(text):
    r = pii.parse_identity(text)
    assert r.values["id_last4"] == "4402"
    assert "4402" not in r.masked and "[ID last 4 given]" in r.masked


def test_phone_forms():
    for text in ("call (650) 521-2836", "+1 650 521 2836", "6505212836",
                 "six five oh five two one two eight three six", "six five zero, five two one, two eight three six"):
        r = pii.parse_identity(text)
        assert r.values["phone"] == "6505212836", text
        assert r.masked.endswith("[phone given]") or "[phone given]" in r.masked


def test_email_forms():
    r = pii.parse_identity("it's margaret.chen@Email.com.")
    assert r.values["email"] == "margaret.chen@email.com" and "@" not in r.masked
    r = pii.parse_identity("my email is m chen at example dot com")
    assert r.values["email"] == "mchen@example.com" and r.masked == "my email is [email given]"
    r = pii.parse_identity("email m dot chen underscore 7 at mail dot example dot org please")
    assert r.values["email"] == "m.chen_7@mail.example.org"


def test_bare_number_after_id_question():
    r = pii.parse_identity("4402", asked="Could you give me the last four of your SSN?")
    assert r.values["id_last4"] == "4402"
    assert pii.parse_identity("4402").values == {}  # no question asked: swept, not stored
    assert pii.parse_identity("4402").masked == "[number]"


def test_ordinary_text_is_untouched():
    t = "claim CL-2048 and POL-9921 from January 2025, I waited two weeks, it was $1500"
    assert pii.parse_identity(t).masked == t


def test_sweep_is_fail_closed():
    out = pii.sweep("odd 44021 digits, 03/15/1985, x@y.org, ref CL-2048")
    assert "44021" not in out and "1985" not in out and "x@y.org" not in out and "CL-2048" in out


def test_presidio_is_optional(monkeypatch):
    monkeypatch.setattr(pii, "_presidio_spans", lambda text: [(0, 4, "[number]")] if text.startswith("Zork") else [])
    assert pii.parse_identity("Zork hello").masked == "[number] hello"
    assert pii.parse_identity("hello").masked == "hello"  # also runs without presidio installed


def test_egress_hits_digit_forms():
    hits = pii.egress_hits("call 650-521-2836, or 4 4 0 2, born March 15, 1985, CL-2048",
                           ["6505212836", "4402", "1985-03-15", "2048"])
    assert hits == ["6505212836", "4402", "1985-03-15"]
    assert pii.egress_hits("Mail M@Email.com", ["m@email.com"]) == ["m@email.com"]


# ---- the model never sees identity values ------------------------------------------------


def run(agent, llm, *messages, s=None, **kw):
    s = s or Session("p")
    return s, [agent.handle(s, m, llm, **kw) for m in messages]


def test_model_prompts_never_hold_identity_values():
    agent, llm = Agent(DataStore(), []), Recording()
    s, r = run(agent, llm, MSG, "yes", "that's all", "yes please")
    assert r[0]["verified"] and r[0]["extraction"]
    blob = "\n".join(llm.prompts)
    for v in RAW:
        assert v not in blob, "identity value reached a prompt"
    assert "[DOB given]" in blob
    assert "Margaret" in blob  # names are visible to the model by design


def test_results_and_transcript_never_hold_identity_values():
    agent, llm = Agent(DataStore(), []), Recording()
    s, r = run(agent, llm, MSG, "yes", "that's all", "yes, send it to me")
    for res in r:
        blob = json.dumps(res)
        for v in RAW:
            assert v not in blob
    assert json.dumps(s.transcript).count(LAST4) == 0
    assert "[DOB given]" in s.transcript[0]["content"]
    assert "question" not in (r[0]["extraction"] or {}) or LAST4 not in r[0]["extraction"]["question"]


def test_spoken_details_verify_without_reaching_the_model():
    agent, llm = Agent(DataStore(), []), Recording()
    spoken = ("My name is Margaret Chen. I was born March fifteenth nineteen eighty five and the last four "
              "of my social is four four seven two")
    s, r = run(agent, llm, spoken)
    assert s.verified, "the check ran on the parsed values"
    assert "nineteen eighty" not in "\n".join(llm.prompts) and "four four seven" not in "\n".join(llm.prompts)


def test_model_values_for_identity_fields_are_ignored():
    class Liar(FakeLLM):
        def extract(self, system, user):
            ex = super().extract(system, user)
            ex.dob, ex.id_last4, ex.phone, ex.email = DOB, LAST4, PHONE, EMAIL
            return ex

    agent = Agent(DataStore(), [])
    s, r = run(agent, Liar(), "My name is Margaret Chen, hello")
    assert not s.verified and not {"dob", "id_last4", "phone", "email"} & set(s.identity)


def test_egress_blocks_a_leaking_reply():
    class Leaky(FakeLLM):
        def respond(self, system, transcript):
            return f"Thanks, I have your date of birth as {DOB}."

    agent = Agent(DataStore(), [])
    s, r = run(agent, Leaky(), MSG)
    assert DOB not in r[0]["reply"]
    assert any(e.get("name") == "pii_egress" for e in r[0]["events"])
    assert DOB not in json.dumps(r[0]["events"])


def test_result_masks_values_outside_reply():
    agent, llm = Agent(DataStore(), []), FakeLLM()
    s, r = run(agent, llm, MSG)
    s.memory["x"] = type(next(iter(s.memory.values())))(f"phone {PHONE}", 1, "VERIFY_ID", "q")
    out = agent.result(s, "hi", None)
    assert PHONE not in json.dumps(out)


# ---- channel and heard -----------------------------------------------------------------


def test_heard_marks_the_transcript():
    agent, llm = Agent(DataStore(), []), FakeLLM()
    s, r = run(agent, llm, "hello")
    full = s.transcript[-1]["content"]
    _, r2 = run(agent, llm, "wait, stop", s=s, heard=full[:20])
    assert s.transcript[-3]["content"] == full[:20] + " [interrupted]"
    assert s.turn == 2


def test_channel_is_set():
    agent, llm = Agent(DataStore(), []), FakeLLM()
    s, _ = run(agent, llm, "hello", channel="voice")
    assert s.channel == "voice"
