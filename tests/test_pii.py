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


# ---- security audit repros (spoken dates, tens digits, email spellings, partial fragments) -------------


def _gone(text, *raws):
    r = pii.parse_identity(text)
    for raw in raws:
        assert raw not in r.masked.lower(), (text, r.masked)
    return r


@pytest.mark.parametrize("text,leaks", [
    ("I was born in nineteen eighty five on March fifteenth", ["nineteen", "eighty", "march", "fifteenth"]),
    ("born march 15 85", ["march", "15", "85"]),
    ("March fifteenth, eighty-five", ["march", "fifteenth", "eighty"]),
    ("oh three fifteen eighty five", ["fifteen", "eighty", "three"]),
    ("nineteen eighty five oh three fifteen", ["nineteen", "eighty", "503", "fifteen"]),
    ("one nine eight five dash zero three dash one five", ["1985", "eight", "dash"]),
    ("my dob is 15 03 1985", ["1985", "15 03", "03"]),
])
def test_audit_spoken_dates_are_a_dob(text, leaks):
    r = _gone(text, *leaks)
    assert r.values["dob"] == "1985-03-15"
    assert "[DOB given]" in r.masked
    assert "id_last4" not in r.values


def test_audit_fail_closed_after_birth_cue():
    r = _gone("born in March 1985", "1985", "march")
    assert "dob" not in r.values  # no full date, but the fragment is gone
    assert _gone("my birthday is March 3rd", "march", "3rd")
    assert _gone("date of birth: thirty first of december nineteen ninety", "december", "ninety", "thirty")
    assert _gone("DOB is somewhere around 1985", "1985")


def test_audit_birth_cue_does_not_eat_claim_dates():
    r = pii.parse_identity("born in March 1985 and my claim is from January 2025")
    assert "January 2025" in r.masked and "1985" not in r.masked
    assert pii.parse_identity("claim from January 2025").masked == "claim from January 2025"
    assert pii.parse_identity("I called on March 15 about CL-2048, $1500").masked == (
        "I called on March 15 about CL-2048, $1500"
    )


@pytest.mark.parametrize("text", ["social is forty four seventy two", "last four forty-four seventy-two",
                                  "my SSN ends in forty four seventy two"])
def test_audit_tens_style_id(text):
    r = pii.parse_identity(text)
    assert r.values["id_last4"] == "4472"
    assert "forty" not in r.masked and "seventy" not in r.masked and "4472" not in r.masked


def test_audit_tens_style_phone():
    r = pii.parse_identity("my phone is six five oh five twenty one eighty three thirty six")
    assert r.values["phone"] == "6505218336"
    assert "fifty" not in r.masked and "eighty" not in r.masked


@pytest.mark.parametrize("text", [
    "margaret at email.com", "margaret @ email dot com", "margaret at email dot c o m",
    "my email is margaret at email dot c o m please", "margaret.chen at email dot com",
])
def test_audit_email_spellings(text):
    r = pii.parse_identity(text)
    assert r.values["email"].startswith("margaret") and r.values["email"].endswith("@email.com")
    low = r.masked.lower()
    assert "margaret" not in low and "email.com" not in low and " dot " not in low and "c o m" not in low


@pytest.mark.parametrize("text", [
    "m a r g a r e t at email dot com", "m a r g a r e t c h e n at email dot com",
    "my email is m a r g a r e t at email dot c o m",
])
def test_audit_letter_by_letter_local_part(text):
    r = pii.parse_identity(text)
    assert r.values["email"] in ("margaret@email.com", "margaretchen@email.com")
    assert r.masked in ("[email given]", "my email is [email given]")


def test_audit_email_fail_closed():
    r = pii.parse_identity("my email is margaret at gmail")
    assert "margaret" not in r.masked and "gmail" not in r.masked
    assert pii.parse_identity("email me at 5pm please").masked == "email me at 5pm please"
    assert pii.parse_identity("can you email me a summary").masked == "can you email me a summary"


def test_audit_phone_fragments():
    for text in ("phone is 521-2836", "call me on 521.2836", "my number is 521 2836"):
        r = pii.parse_identity(text)
        assert "521" not in r.masked and "2836" not in r.masked and "[number]" not in r.masked, text
        assert "[phone given]" in r.masked
    assert pii.parse_identity("claim CL-2048, POL-9921").masked == "claim CL-2048, POL-9921"


def test_audit_dob_not_taken_as_id():
    r = pii.parse_identity("one nine eight five dash zero three dash one five")
    assert "id_last4" not in r.values
    r = pii.parse_identity("my SSN last four is 4402 and I was born 1985-03-15")
    assert r.values == {"id_last4": "4402", "dob": "1985-03-15"}


def test_audit_summary_email_never_stored_raw():
    from app import memory

    given = "other.person@example.com"
    agent, llm = Agent(DataStore(), []), Recording()
    s, r = run(agent, llm, MSG, "yes", "that's all", f"yes, send it to {given}")
    blob = json.dumps(r) + json.dumps(memory.snapshot(s)) + json.dumps(s.handoff_packet or {})
    assert given not in blob and "other.person" not in blob
    assert given not in "\n".join(llm.prompts)
    assert "email_address" not in (s.handoff_packet or {}).get("memory", {})
    slot = s.memory.get("email_address")
    assert slot is None or "other.person" not in json.dumps(slot.as_dict())


def test_audit_handoff_packet_filters_email_slot():
    from app import memory
    from app.models import Extraction

    s = Session("p")
    memory.write(s, Extraction(email_address_for_summary="Someone@Example.com"), "send it to [email given]")
    assert s.memory["email_address"].value == "s***@example.com"
    assert "someone" not in json.dumps(memory.snapshot(s)).lower().replace("s***", "")
