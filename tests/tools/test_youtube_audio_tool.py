import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.youtube_audio_tool import (
    SUPPORTED_BITRATES,
    check_youtube_audio_requirements,
    cleanup_youtube_title,
    infer_artist_title,
    is_supported_youtube_url,
    sanitize_filename,
    youtube_to_mp3,
)


class TestSupportedYoutubeUrls:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
        ],
    )
    def test_supported_urls(self, url):
        assert is_supported_youtube_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/watch?v=dQw4w9WgXcQ",
            "https://vimeo.com/12345",
            "not-a-url",
            "",
        ],
    )
    def test_unsupported_urls(self, url):
        assert is_supported_youtube_url(url) is False


class TestTitleCleanup:
    def test_cleanup_removes_common_noise(self):
        assert cleanup_youtube_title("Artist - Song Title (Official Video) [HD]") == "Artist - Song Title"


class TestArtistInference:
    def test_infers_artist_and_title_from_dash(self):
        inferred = infer_artist_title("Daft Punk - Harder Better Faster Stronger")
        assert inferred == {
            "artist": "Daft Punk",
            "title": "Harder Better Faster Stronger",
            "artist_inferred": True,
        }

    def test_does_not_infer_without_separator(self):
        inferred = infer_artist_title("Just The Song Name")
        assert inferred == {
            "artist": None,
            "title": "Just The Song Name",
            "artist_inferred": False,
        }


class TestFilenameSanitization:
    def test_sanitizes_for_filesystem_use(self):
        assert sanitize_filename(' AC/DC: Back In Black?* ') == "AC_DC_ Back In Black"


class TestRequirements:
    def test_missing_dependency_returns_structured_error(self, monkeypatch):
        monkeypatch.setattr("tools.youtube_audio_tool.shutil.which", lambda _name: None)

        result = json.loads(youtube_to_mp3("https://youtu.be/dQw4w9WgXcQ"))

        assert result["success"] is False
        assert result["error"] == "missing_dependency"
        assert "yt-dlp" in result["detail"]

    def test_check_requirements_false_when_dependencies_missing(self, monkeypatch):
        monkeypatch.setattr("tools.youtube_audio_tool.shutil.which", lambda _name: None)
        assert check_youtube_audio_requirements() is False


class TestYoutubeToMp3:
    def test_successful_conversion_returns_structured_payload(self, monkeypatch, tmp_path):
        monkeypatch.setattr("tools.youtube_audio_tool.get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr("tools.youtube_audio_tool.check_youtube_audio_requirements", lambda: True)
        monkeypatch.setattr("tools.youtube_audio_tool._mutagen_available", lambda: True)

        def fake_run(command, capture_output, text, timeout):
            if command[0] == "yt-dlp" and "--dump-single-json" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "id": "dQw4w9WgXcQ",
                            "title": "Rick Astley - Never Gonna Give You Up (Official Video)",
                            "uploader": "Rick Astley",
                            "channel": "Rick Astley",
                            "upload_date": "20091025",
                        }
                    ),
                    stderr="",
                )
            if command[0] == "yt-dlp":
                output_path = Path(command[command.index("-o") + 1])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"raw-audio")
                return subprocess.CompletedProcess(command, 0, stdout="downloaded", stderr="")
            if command[0] == "ffmpeg":
                output_path = Path(command[-1])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"mp3-audio")
                return subprocess.CompletedProcess(command, 0, stdout="converted", stderr="")
            raise AssertionError(f"unexpected command: {command}")

        metadata_calls = []

        monkeypatch.setattr("tools.youtube_audio_tool.subprocess.run", fake_run)
        monkeypatch.setattr(
            "tools.youtube_audio_tool._extract_media_info",
            lambda path: {"title": "Never Gonna Give You Up", "artist": None, "warnings": []},
        )
        monkeypatch.setattr(
            "tools.youtube_audio_tool._write_id3_tags",
            lambda path, metadata: metadata_calls.append((path, metadata)),
            raising=False,
        )

        result = json.loads(
            youtube_to_mp3(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                preferred_bitrate="320k",
            )
        )

        assert result["success"] is True
        assert result["title"] == "Never Gonna Give You Up"
        assert result["artist"] == "Rick Astley"
        assert result["artist_inferred"] is True
        assert result["source_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert result["video_id"] == "dQw4w9WgXcQ"
        assert Path(result["file_path"]).exists()
        assert Path(result["file_path"]).parent == tmp_path / "media_cache" / "youtube-audio" / "processed"
        assert metadata_calls, "expected ID3 tag writer to be called"
        written_path, written_metadata = metadata_calls[0]
        assert Path(written_path) == Path(result["file_path"])
        assert written_metadata["artist"] == "Rick Astley"
        assert written_metadata["title"] == "Never Gonna Give You Up"
        assert written_metadata["source_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert written_metadata["year"] == "2009"

    def test_successful_conversion_uses_youtube_title_when_output_tags_fall_back_to_filename(self, monkeypatch, tmp_path):
        monkeypatch.setattr("tools.youtube_audio_tool.get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr("tools.youtube_audio_tool.check_youtube_audio_requirements", lambda: True)
        monkeypatch.setattr("tools.youtube_audio_tool._mutagen_available", lambda: True)

        def fake_run(command, capture_output, text, timeout):
            if command[0] == "yt-dlp" and "--dump-single-json" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "id": "dQw4w9WgXcQ",
                            "title": "Rick Astley - Never Gonna Give You Up (Official Video)",
                            "uploader": "Rick Astley",
                            "channel": "Rick Astley",
                            "upload_date": "20091025",
                        }
                    ),
                    stderr="",
                )
            if command[0] == "yt-dlp":
                output_path = Path(command[command.index("-o") + 1])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"raw-audio")
                return subprocess.CompletedProcess(command, 0, stdout="downloaded", stderr="")
            if command[0] == "ffmpeg":
                output_path = Path(command[-1])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"mp3-audio")
                return subprocess.CompletedProcess(command, 0, stdout="converted", stderr="")
            raise AssertionError(f"unexpected command: {command}")

        monkeypatch.setattr("tools.youtube_audio_tool.subprocess.run", fake_run)
        monkeypatch.setattr(
            "tools.youtube_audio_tool._extract_media_info",
            lambda path: {"title": path.stem, "artist": None, "warnings": []},
        )
        monkeypatch.setattr("tools.youtube_audio_tool._write_id3_tags", lambda path, metadata: None)

        result = json.loads(youtube_to_mp3("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

        assert result["success"] is True
        assert result["title"] == "Never Gonna Give You Up"
        assert result["artist"] == "Rick Astley"

    def test_conversion_failure_returns_structured_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr("tools.youtube_audio_tool.get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr("tools.youtube_audio_tool.check_youtube_audio_requirements", lambda: True)
        monkeypatch.setattr("tools.youtube_audio_tool._mutagen_available", lambda: True)

        def fake_run(command, capture_output, text, timeout):
            if command[0] == "yt-dlp" and "--dump-single-json" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({"id": "dQw4w9WgXcQ", "title": "Song", "uploader": "Uploader"}),
                    stderr="",
                )
            if command[0] == "yt-dlp":
                output_path = Path(command[command.index("-o") + 1])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"raw-audio")
                return subprocess.CompletedProcess(command, 0, stdout="downloaded", stderr="")
            if command[0] == "ffmpeg":
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")
            raise AssertionError(f"unexpected command: {command}")

        monkeypatch.setattr("tools.youtube_audio_tool.subprocess.run", fake_run)

        result = json.loads(youtube_to_mp3("https://youtu.be/dQw4w9WgXcQ", preferred_bitrate="256k"))

        assert result["success"] is False
        assert result["error"] == "conversion_failed"
        assert "boom" in result["detail"]
        assert result["video_id"] == "dQw4w9WgXcQ"

    def test_invalid_bitrate_is_rejected(self, monkeypatch):
        monkeypatch.setattr("tools.youtube_audio_tool.check_youtube_audio_requirements", lambda: True)
        result = json.loads(youtube_to_mp3("https://youtu.be/dQw4w9WgXcQ", preferred_bitrate="128k"))
        assert result["success"] is False
        assert result["error"] == "invalid_bitrate"
        assert set(result["supported_bitrates"]) == SUPPORTED_BITRATES
