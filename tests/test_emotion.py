from app.emotion import frustration_shown, record, resisting, tone_lines
from app.fake_llm import _fake_emotions
from app.models import Extraction, Session


def ex_with(**levels) -> Extraction:
    ex = Extraction()
    for name, level in levels.items():
        getattr(ex.emotions, name).level = level
    return ex


def session_with(*turns: dict) -> Session:
    s = Session(session_id="t")
    for i, levels in enumerate(turns, 1):
        s.turn = i
        record(s, ex_with(**levels))
    return s


def test_strong_once_is_shown():
    assert frustration_shown(session_with({}, {"anger": "strong"}))
    assert frustration_shown(session_with({"frustration": "strong"}))


def test_mild_twice_is_shown_mild_once_is_not():
    assert not frustration_shown(session_with({"frustration": "mild"}, {}))
    assert frustration_shown(session_with({"frustration": "mild"}, {}, {"anger": "mild"}))


def test_other_emotions_do_not_count():
    assert not frustration_shown(session_with({"anxiety": "strong"}, {"refusal": "strong"}, {"confusion": "strong"}))


def test_resisting():
    assert resisting(ex_with(refusal="mild"))
    assert resisting(ex_with(frustration="mild"))
    assert not resisting(ex_with(anger="strong", anxiety="mild"))
    assert not resisting(Extraction())


def test_tone_lines():
    assert tone_lines(Extraction()) == []
    assert len(tone_lines(ex_with(frustration="mild", anger="strong"))) == 1
    lines = tone_lines(ex_with(anxiety="mild", confusion="mild"))
    assert len(lines) == 2
    assert "reassure" in lines[0] and "one step at a time" in lines[1]


def test_fake_heuristic_on_task_example():
    r = _fake_emotions("I already told you who I am. This is ridiculous. Just tell me why my claim was denied.")
    assert r.frustration.level == "mild" and r.frustration.words == "already told you"
    assert r.anger.level == "none" and r.refusal.level == "none"


def test_fake_heuristic_levels():
    assert _fake_emotions("you are useless").anger.level == "strong"
    assert _fake_emotions("I'm worried about the deadline").anxiety.level == "mild"
    assert _fake_emotions("what do you mean?").confusion.level == "mild"
    assert _fake_emotions("I'm not telling you that").refusal.level == "strong"
    assert _fake_emotions("why do you need that?").refusal.level == "mild"
    assert _fake_emotions("hello").model_dump() == Extraction().emotions.model_dump()
