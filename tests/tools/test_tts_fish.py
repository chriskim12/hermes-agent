"""Tests for the Fish Audio TTS provider in tools/tts_tool.py."""

import json
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("FISH_AUDIO_API_KEY", "HERMES_SESSION_PLATFORM"):
        monkeypatch.delenv(key, raising=False)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TestGenerateFishTts:
    def test_missing_api_key_raises_value_error(self, tmp_path):
        from tools.tts_tool import _generate_fish_tts

        with pytest.raises(ValueError, match="FISH_AUDIO_API_KEY"):
            _generate_fish_tts("안녕하세요", str(tmp_path / "out.mp3"), {})

    def test_successful_generation_uses_voice_id_as_reference_id(self, tmp_path, monkeypatch):
        from tools.tts_tool import _generate_fish_tts

        monkeypatch.setenv("FISH_AUDIO_API_KEY", "fish-test-key")
        captured = {}

        def fake_urlopen(request, timeout=60):
            captured["url"] = request.full_url
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            captured["body"] = json.loads(request.data.decode())
            return _FakeResponse(b"fish-audio-bytes")

        config = {
            "fish": {
                "voice_id": "abd16dec3c5c40189ce54fa4fff2a130",
                "model": "s2-pro",
            }
        }

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = _generate_fish_tts("안녕하세요", str(tmp_path / "out.mp3"), config)

        assert result == str(tmp_path / "out.mp3")
        assert (tmp_path / "out.mp3").read_bytes() == b"fish-audio-bytes"
        assert captured["url"] == "https://api.fish.audio/v1/tts"
        assert captured["headers"]["authorization"] == "Bearer fish-test-key"
        assert captured["headers"]["model"] == "s2-pro"
        assert captured["body"]["text"] == "안녕하세요"
        assert captured["body"]["reference_id"] == "abd16dec3c5c40189ce54fa4fff2a130"
        assert captured["body"]["format"] == "mp3"

    def test_missing_voice_id_raises_value_error(self, tmp_path, monkeypatch):
        from tools.tts_tool import _generate_fish_tts

        monkeypatch.setenv("FISH_AUDIO_API_KEY", "fish-test-key")

        with pytest.raises(ValueError, match="voice_id"):
            _generate_fish_tts("안녕하세요", str(tmp_path / "out.mp3"), {"fish": {}})


class TestTtsDispatcherFish:
    def test_dispatcher_routes_to_fish(self, tmp_path, monkeypatch):
        from tools.tts_tool import text_to_speech_tool

        monkeypatch.setenv("FISH_AUDIO_API_KEY", "fish-test-key")

        def fake_urlopen(request, timeout=60):
            return _FakeResponse(b"fish-audio")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen), patch(
            "tools.tts_tool._load_tts_config",
            return_value={
                "provider": "fish",
                "fish": {"voice_id": "abd16dec3c5c40189ce54fa4fff2a130"},
            },
        ):
            result = json.loads(text_to_speech_tool("테스트", output_path=str(tmp_path / "out.mp3")))

        assert result["success"] is True
        assert result["provider"] == "fish"
        assert result["file_path"].endswith("out.mp3")

    def test_fish_missing_key_fails_closed(self, tmp_path):
        from tools.tts_tool import text_to_speech_tool

        with patch(
            "tools.tts_tool._load_tts_config",
            return_value={
                "provider": "fish",
                "fish": {"voice_id": "abd16dec3c5c40189ce54fa4fff2a130"},
            },
        ):
            result = json.loads(text_to_speech_tool("테스트", output_path=str(tmp_path / "out.mp3")))

        assert result["success"] is False
        assert "FISH_AUDIO_API_KEY" in result["error"]

    def test_unknown_provider_does_not_fall_back_to_edge(self, tmp_path):
        from tools.tts_tool import text_to_speech_tool

        with patch("tools.tts_tool._load_tts_config", return_value={"provider": "definitely-not-real"}):
            result = json.loads(text_to_speech_tool("테스트", output_path=str(tmp_path / "out.mp3")))

        assert result["success"] is False
        assert "Unsupported TTS provider" in result["error"]


class TestCheckTtsRequirementsFish:
    def test_fish_key_counts_as_available_provider(self, monkeypatch):
        from tools.tts_tool import check_tts_requirements

        monkeypatch.setenv("FISH_AUDIO_API_KEY", "fish-test-key")
        with patch("tools.tts_tool._import_edge_tts", side_effect=ImportError), \
             patch("tools.tts_tool._import_elevenlabs", side_effect=ImportError), \
             patch("tools.tts_tool._import_openai_client", side_effect=ImportError), \
             patch("tools.tts_tool._import_mistral_client", side_effect=ImportError), \
             patch("tools.tts_tool._check_neutts_available", return_value=False):
            assert check_tts_requirements() is True
