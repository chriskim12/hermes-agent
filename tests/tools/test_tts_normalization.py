import json
import logging
from unittest.mock import MagicMock

from tools.tts_tool import text_to_speech_tool


class TestDiscordTtsNormalization:
    def test_discord_mp3_is_normalized_before_media_tag(self, tmp_path, monkeypatch):
        original_path = tmp_path / "speech.mp3"
        normalized_path = tmp_path / "speech.normalized.mp3"
        ffmpeg_calls = []

        async def fake_generate_edge_tts(text, output_path, tts_config):
            assert text == "hello"
            assert output_path == str(original_path)
            original_path.write_bytes(b"original-mp3")
            return output_path

        def fake_run(cmd, capture_output=False, timeout=None, **kwargs):
            ffmpeg_calls.append(cmd)
            assert cmd[0] == "/usr/bin/ffmpeg"
            assert cmd[1:4] == ["-i", str(original_path), "-af"]
            assert cmd[4] == "loudnorm=I=-18:TP=-2.0:LRA=9"
            assert cmd[-2:] == [str(normalized_path), "-y"]
            normalized_path.write_bytes(b"normalized-mp3")
            return MagicMock(returncode=0, stderr=b"")

        monkeypatch.setattr("tools.tts_tool._load_tts_config", lambda: {
            "provider": "edge",
            "postprocess": {
                "normalize": {
                    "enabled": True,
                    "platforms": ["discord"],
                    "filter": "loudnorm=I=-18:TP=-2.0:LRA=9",
                    "suffix": ".normalized",
                }
            },
        })
        monkeypatch.setattr("tools.tts_tool._generate_edge_tts", fake_generate_edge_tts)
        monkeypatch.setattr("tools.tts_tool.shutil.which", lambda cmd: "/usr/bin/ffmpeg" if cmd == "ffmpeg" else None)
        monkeypatch.setattr("tools.tts_tool.subprocess.run", fake_run)
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")

        result = json.loads(text_to_speech_tool("hello", output_path=str(original_path)))

        assert result["success"] is True
        assert result["provider"] == "edge"
        assert result["file_path"] == str(normalized_path)
        assert result["media_tag"] == f"MEDIA:{normalized_path}"
        assert ffmpeg_calls, "expected ffmpeg normalization to run"

    def test_discord_mp3_falls_back_to_original_when_normalization_fails(self, tmp_path, monkeypatch, caplog):
        original_path = tmp_path / "speech.mp3"

        async def fake_generate_edge_tts(text, output_path, tts_config):
            original_path.write_bytes(b"original-mp3")
            return output_path

        def fake_run(cmd, capture_output=False, timeout=None, **kwargs):
            return MagicMock(returncode=1, stderr=b"ffmpeg exploded")

        monkeypatch.setattr("tools.tts_tool._load_tts_config", lambda: {
            "provider": "edge",
            "postprocess": {"normalize": {"enabled": True, "platforms": ["discord"]}},
        })
        monkeypatch.setattr("tools.tts_tool._generate_edge_tts", fake_generate_edge_tts)
        monkeypatch.setattr("tools.tts_tool.shutil.which", lambda cmd: "/usr/bin/ffmpeg" if cmd == "ffmpeg" else None)
        monkeypatch.setattr("tools.tts_tool.subprocess.run", fake_run)
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
        caplog.set_level(logging.WARNING, logger="tools.tts_tool")

        result = json.loads(text_to_speech_tool("hello", output_path=str(original_path)))

        assert result["success"] is True
        assert result["file_path"] == str(original_path)
        assert result["media_tag"] == f"MEDIA:{original_path}"
        assert any("normalization" in record.message.lower() for record in caplog.records)

    def test_discord_mp3_falls_back_when_normalized_suffix_is_invalid(self, tmp_path, monkeypatch, caplog):
        original_path = tmp_path / "speech.mp3"

        async def fake_generate_edge_tts(text, output_path, tts_config):
            original_path.write_bytes(b"original-mp3")
            return output_path

        monkeypatch.setattr("tools.tts_tool._load_tts_config", lambda: {
            "provider": "edge",
            "postprocess": {"normalize": {"enabled": True, "platforms": ["discord"], "suffix": "../bad"}},
        })
        monkeypatch.setattr("tools.tts_tool._generate_edge_tts", fake_generate_edge_tts)
        monkeypatch.setattr("tools.tts_tool.shutil.which", lambda cmd: "/usr/bin/ffmpeg" if cmd == "ffmpeg" else None)
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
        caplog.set_level(logging.WARNING, logger="tools.tts_tool")

        result = json.loads(text_to_speech_tool("hello", output_path=str(original_path)))

        assert result["success"] is True
        assert result["file_path"] == str(original_path)
        assert result["media_tag"] == f"MEDIA:{original_path}"
        assert result["normalization"] == {
            "attempted": True,
            "applied": False,
            "reason": "ffmpeg unavailable or normalization failed; using original audio",
        }
        assert any("normalization" in record.message.lower() for record in caplog.records)
