"""Server speech: the route, the config flag and (when the model is installed) real synthesis."""

import io
import wave

import pytest
from fastapi.testclient import TestClient

from app import server, tts

client = TestClient(server.app)


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(tts, "available", lambda: True)
    monkeypatch.setattr(tts, "synthesize", lambda text: b"RIFFstub")


def test_tts_returns_wav(stub):
    r = client.post("/api/tts", json={"text": "Hello there."})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    assert r.content == b"RIFFstub"


@pytest.mark.parametrize("text", ["", "   ", "x" * 2001])
def test_tts_rejects_bad_text(stub, text):
    assert client.post("/api/tts", json={"text": text}).status_code == 400


def test_tts_503_when_unavailable(monkeypatch):
    monkeypatch.setattr(tts, "available", lambda: False)
    assert client.post("/api/tts", json={"text": "Hi"}).status_code == 503


def test_tts_503_when_synthesis_fails(monkeypatch):
    monkeypatch.setattr(tts, "available", lambda: True)

    def boom(text):
        raise RuntimeError("model broke")

    monkeypatch.setattr(tts, "synthesize", boom)
    assert client.post("/api/tts", json={"text": "Hi"}).status_code == 503


def test_config_reports_tts(monkeypatch):
    monkeypatch.setattr(tts, "available", lambda: True)
    assert client.get("/api/config").json()["tts"] == "kokoro"
    monkeypatch.setattr(tts, "available", lambda: False)
    assert client.get("/api/config").json()["tts"] == "browser"


def test_browser_override(monkeypatch):
    monkeypatch.setenv("CLAIMS_TTS", "browser")
    assert tts.available() is False


def test_real_synthesis():
    if not tts.available():
        pytest.skip("Kokoro package or model files not installed")
    data = tts.synthesize("Hello, this is a short test.")
    with wave.open(io.BytesIO(data)) as w:
        assert w.getsampwidth() == 2
        assert w.getnframes() > w.getframerate() // 2
