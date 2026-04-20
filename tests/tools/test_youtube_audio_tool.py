import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.youtube_audio_tool import (
    SUPPORTED_BITRATES,
    _google_drive_folder_id,
    _is_google_or_youtube_cookie_domain,
    _refresh_youtube_cookies_from_agent_browser,
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
    @pytest.mark.parametrize(
        ("domain", "expected"),
        [
            ("youtube.com", True),
            (".youtube.com", True),
            ("music.youtube.com", True),
            ("accounts.google.com", True),
            (".google.com", True),
            ("evilgoogle.com", False),
            ("notyoutube.com", False),
            ("youtube.com.evil.example", False),
        ],
    )
    def test_cookie_domain_filter_enforces_dot_boundary(self, domain, expected):
        assert _is_google_or_youtube_cookie_domain(domain) is expected

    def test_google_drive_folder_id_reads_trimmed_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr("tools.youtube_audio_tool.get_hermes_home", lambda: tmp_path)
        config_path = tmp_path / "integrations" / "youtube-audio-google-drive-folder.txt"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("  folder123\n", encoding="utf-8")

        assert _google_drive_folder_id() == "folder123"

    def test_agent_browser_cookie_refresh_writes_filtered_netscape_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr("tools.youtube_audio_tool.get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(
            "tools.youtube_audio_tool.shutil.which",
            lambda name: "/usr/bin/agent-browser" if name == "agent-browser" else None,
        )

        commands = []

        def fake_run(command, capture_output, text, timeout):
            commands.append(command)
            if command[0] == "/usr/bin/agent-browser" and "cookies" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "success": True,
                            "data": {
                                "cookies": [
                                    {
                                        "domain": ".youtube.com",
                                        "name": "VISITOR_INFO1_LIVE",
                                        "value": "visitor-token",
                                        "path": "/",
                                        "secure": True,
                                        "httpOnly": False,
                                        "expires": 1791637640,
                                    },
                                    {
                                        "domain": ".google.com",
                                        "name": "SID",
                                        "value": "sid-token",
                                        "path": "/",
                                        "secure": True,
                                        "httpOnly": True,
                                        "expires": 1810617332,
                                    },
                                    {
                                        "domain": ".example.com",
                                        "name": "ignore_me",
                                        "value": "ignored",
                                        "path": "/",
                                        "secure": False,
                                        "httpOnly": False,
                                        "expires": 1810617332,
                                    },
                                    {
                                        "domain": ".evilgoogle.com",
                                        "name": "ignore_me_too",
                                        "value": "ignored-too",
                                        "path": "/",
                                        "secure": True,
                                        "httpOnly": False,
                                        "expires": 1810617332,
                                    },
                                ]
                            },
                            "error": None,
                        }
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"success": True, "data": {}}), stderr="")

        monkeypatch.setattr("tools.youtube_audio_tool.subprocess.run", fake_run)

        result = _refresh_youtube_cookies_from_agent_browser()

        cookie_path = tmp_path / "secrets" / "youtube-cookies.txt"
        assert result["success"] is True
        assert result["cookie_count"] == 2
        assert result["cookie_path"] == str(cookie_path)
        assert cookie_path.exists()
        content = cookie_path.read_text(encoding="utf-8")
        assert content.startswith("# Netscape HTTP Cookie File\n")
        assert "VISITOR_INFO1_LIVE" in content
        assert "\tSID\t" in content
        assert "ignore_me" not in content
        assert "ignore_me_too" not in content
        assert any(command[0] == "/usr/bin/agent-browser" and "--profile" in command for command in commands)

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
    def test_successful_conversion_passes_cookie_file_to_ytdlp_when_present(self, monkeypatch, tmp_path):
        monkeypatch.setattr("tools.youtube_audio_tool.get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr("tools.youtube_audio_tool.check_youtube_audio_requirements", lambda: True)
        monkeypatch.setattr("tools.youtube_audio_tool._mutagen_available", lambda: True)

        cookie_dir = tmp_path / "secrets"
        cookie_dir.mkdir(parents=True, exist_ok=True)
        cookie_path = cookie_dir / "youtube-cookies.txt"
        cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

        yt_dlp_commands = []

        def fake_run(command, capture_output, text, timeout):
            if command[0] == "yt-dlp":
                yt_dlp_commands.append(command)
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
            lambda path: {"title": "Never Gonna Give You Up", "artist": None, "warnings": []},
        )
        monkeypatch.setattr("tools.youtube_audio_tool._write_id3_tags", lambda path, metadata: None)

        result = json.loads(youtube_to_mp3("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

        assert result["success"] is True
        assert len(yt_dlp_commands) == 2
        for command in yt_dlp_commands:
            assert "--cookies" in command
            assert command[command.index("--cookies") + 1] == str(cookie_path)

    def test_successful_conversion_adds_js_runtime_when_node_is_available(self, monkeypatch, tmp_path):
        monkeypatch.setattr("tools.youtube_audio_tool.get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr("tools.youtube_audio_tool.check_youtube_audio_requirements", lambda: True)
        monkeypatch.setattr("tools.youtube_audio_tool._mutagen_available", lambda: True)
        monkeypatch.setattr(
            "tools.youtube_audio_tool.shutil.which",
            lambda name: "/usr/bin/node" if name == "node" else "/usr/bin/fake",
        )

        yt_dlp_commands = []

        def fake_run(command, capture_output, text, timeout):
            if command[0] == "yt-dlp":
                yt_dlp_commands.append(command)
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
        monkeypatch.setattr("tools.youtube_audio_tool._extract_media_info", lambda path: {"title": "Never Gonna Give You Up", "artist": None, "warnings": []})
        monkeypatch.setattr("tools.youtube_audio_tool._write_id3_tags", lambda path, metadata: None)

        result = json.loads(youtube_to_mp3("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

        assert result["success"] is True
        assert len(yt_dlp_commands) == 2
        for command in yt_dlp_commands:
            assert "--js-runtimes" in command
            assert command[command.index("--js-runtimes") + 1] == "node"
            assert "--remote-components" not in command

    def test_successful_conversion_ignores_unreadable_cookie_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr("tools.youtube_audio_tool.get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr("tools.youtube_audio_tool.check_youtube_audio_requirements", lambda: True)
        monkeypatch.setattr("tools.youtube_audio_tool._mutagen_available", lambda: True)

        cookie_dir = tmp_path / "secrets"
        cookie_dir.mkdir(parents=True, exist_ok=True)
        cookie_path = cookie_dir / "youtube-cookies.txt"
        cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

        original_stat = Path.stat

        def fake_stat(self, *args, **kwargs):
            if self == cookie_path:
                raise OSError("permission denied")
            return original_stat(self, *args, **kwargs)

        yt_dlp_commands = []

        def fake_run(command, capture_output, text, timeout):
            if command[0] == "yt-dlp":
                yt_dlp_commands.append(command)
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
        monkeypatch.setattr("tools.youtube_audio_tool._extract_media_info", lambda path: {"title": "Never Gonna Give You Up", "artist": None, "warnings": []})
        monkeypatch.setattr("tools.youtube_audio_tool._write_id3_tags", lambda path, metadata: None)
        monkeypatch.setattr(Path, "stat", fake_stat)

        result = json.loads(youtube_to_mp3("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

        assert result["success"] is True
        assert len(yt_dlp_commands) == 2
        for command in yt_dlp_commands:
            assert "--cookies" not in command

    def test_retries_download_after_agent_browser_cookie_refresh(self, monkeypatch, tmp_path):
        monkeypatch.setattr("tools.youtube_audio_tool.get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr("tools.youtube_audio_tool.check_youtube_audio_requirements", lambda: True)
        monkeypatch.setattr("tools.youtube_audio_tool._mutagen_available", lambda: True)
        monkeypatch.setattr(
            "tools.youtube_audio_tool.shutil.which",
            lambda name: "/usr/bin/agent-browser" if name == "agent-browser" else "/usr/bin/fake",
        )

        download_attempts = 0
        yt_dlp_commands = []

        def fake_run(command, capture_output, text, timeout):
            nonlocal download_attempts
            if command[0] == "yt-dlp":
                yt_dlp_commands.append(command)
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
                download_attempts += 1
                if download_attempts == 1:
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        stdout="",
                        stderr="ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you’re not a bot.",
                    )
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

        def fake_refresh():
            cookie_dir = tmp_path / "secrets"
            cookie_dir.mkdir(parents=True, exist_ok=True)
            cookie_path = cookie_dir / "youtube-cookies.txt"
            cookie_path.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1791637640\tVISITOR_INFO1_LIVE\tvisitor-token\n", encoding="utf-8")
            return {"success": True, "cookie_count": 1, "cookie_path": str(cookie_path), "source": "agent_browser"}

        monkeypatch.setattr("tools.youtube_audio_tool.subprocess.run", fake_run)
        monkeypatch.setattr("tools.youtube_audio_tool._refresh_youtube_cookies_from_agent_browser", fake_refresh)
        monkeypatch.setattr("tools.youtube_audio_tool._extract_media_info", lambda path: {"title": "Never Gonna Give You Up", "artist": None, "warnings": []})
        monkeypatch.setattr("tools.youtube_audio_tool._write_id3_tags", lambda path, metadata: None)

        result = json.loads(youtube_to_mp3("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

        assert result["success"] is True
        assert result["auth_refresh"]["success"] is True
        assert download_attempts == 2
        download_commands = [command for command in yt_dlp_commands if "--dump-single-json" not in command]
        assert "--cookies" not in download_commands[0]
        assert "--cookies" in download_commands[1]
        assert result["auth_refresh"]["source"] == "agent_browser"

    def test_successful_conversion_includes_drive_upload_metadata_when_configured(self, monkeypatch, tmp_path):
        monkeypatch.setattr("tools.youtube_audio_tool.get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr("tools.youtube_audio_tool.check_youtube_audio_requirements", lambda: True)
        monkeypatch.setattr("tools.youtube_audio_tool._mutagen_available", lambda: True)

        config_path = tmp_path / "integrations" / "youtube-audio-google-drive-folder.txt"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("folder123\n", encoding="utf-8")

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
        monkeypatch.setattr("tools.youtube_audio_tool._extract_media_info", lambda path: {"title": "Never Gonna Give You Up", "artist": None, "warnings": []})
        monkeypatch.setattr("tools.youtube_audio_tool._write_id3_tags", lambda path, metadata: None)
        monkeypatch.setattr(
            "tools.youtube_audio_tool._upload_file_to_google_drive",
            lambda path: {
                "success": True,
                "folder_id": "folder123",
                "file_id": "drive-file-1",
                "name": path.name,
                "web_view_link": "https://drive.google.com/file/d/drive-file-1/view",
            },
        )

        result = json.loads(youtube_to_mp3("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

        assert result["success"] is True
        assert result["drive_upload"]["success"] is True
        assert result["drive_upload"]["folder_id"] == "folder123"
        assert result["drive_upload"]["file_id"] == "drive-file-1"

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
