import json
from pathlib import Path

import pytest

from tools import yuuka_voice_tool as mod


class _FakeHTTPResponse:
    def __init__(self, chunks: list[bytes], headers: dict[str, str] | None = None):
        self._chunks = list(chunks)
        self.headers = headers or {}

    def read(self, _size: int = -1) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _write_catalog(tmp_path: Path, entries: list[dict]) -> Path:
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return catalog_path


def test_select_yuuka_voice_entry_prefers_variant_and_situation(monkeypatch, tmp_path):
    catalog = _write_catalog(
        tmp_path,
        [
            {
                "id": "base-warning",
                "variant": "base",
                "clip_class": "semantic_extension",
                "file_name": "Yuuka_Lobby_2.ogg",
                "situations": ["warning"],
                "moods": ["strict"],
                "paraphrase_en": "warning",
            },
            {
                "id": "sportswear-late",
                "variant": "sportswear",
                "clip_class": "semantic_extension",
                "file_name": "Yuuka_(Sportswear)_LogIn_2_1.ogg",
                "situations": ["late", "warning"],
                "moods": ["strict"],
                "paraphrase_en": "you are late",
            },
        ],
    )
    monkeypatch.setattr(mod, "_bundled_catalog_path", lambda: catalog)
    monkeypatch.setattr(mod, "_catalog_override_path", lambda: tmp_path / "missing.json")

    selected = mod.select_yuuka_voice_entry(
        variant="sportswear",
        clip_class="semantic_extension",
        situations=["late"],
        moods=["strict"],
        query="late",
    )

    assert selected is not None
    assert selected["id"] == "sportswear-late"
    assert "variant:sportswear" in selected["reasons"]
    assert "situation:late" in selected["reasons"]



def test_select_yuuka_voice_entry_prefers_situation_match_over_variant_bias(monkeypatch, tmp_path):
    catalog = _write_catalog(
        tmp_path,
        [
            {
                "id": "aaa-situation-match",
                "variant": "base",
                "clip_class": "semantic_extension",
                "file_name": "Yuuka_Late_Base.ogg",
                "situations": ["late", "warning"],
                "moods": ["strict"],
                "paraphrase_en": "you are late",
            },
            {
                "id": "zzz-variant-only",
                "variant": "sportswear",
                "clip_class": "semantic_extension",
                "file_name": "Yuuka_Sportswear_Generic.ogg",
                "situations": ["work_start"],
                "moods": ["strict"],
                "paraphrase_en": "let us get started",
            },
        ],
    )
    monkeypatch.setattr(mod, "_bundled_catalog_path", lambda: catalog)
    monkeypatch.setattr(mod, "_catalog_override_path", lambda: tmp_path / "missing.json")

    selected = mod.select_yuuka_voice_entry(
        variant="sportswear",
        clip_class="semantic_extension",
        situations=["late"],
        moods=["strict"],
        query=None,
    )

    assert selected is not None
    assert selected["id"] == "aaa-situation-match"



def test_select_yuuka_voice_entry_returns_none_when_no_positive_match(monkeypatch, tmp_path):
    catalog = _write_catalog(
        tmp_path,
        [
            {
                "id": "base-rest",
                "variant": "base",
                "clip_class": "mood_coloring",
                "file_name": "Yuuka_Cafe_Act_1.ogg",
                "situations": ["rest"],
                "moods": ["warm"],
                "paraphrase_en": "take a break",
            }
        ],
    )
    monkeypatch.setattr(mod, "_bundled_catalog_path", lambda: catalog)
    monkeypatch.setattr(mod, "_catalog_override_path", lambda: tmp_path / "missing.json")

    selected = mod.select_yuuka_voice_entry(
        variant="sportswear",
        clip_class="semantic_extension",
        situations=["late"],
        moods=["strict"],
        query="late",
    )

    assert selected is None



def test_select_yuuka_voice_entry_returns_none_for_empty_catalog(monkeypatch, tmp_path):
    catalog = _write_catalog(tmp_path, [])
    monkeypatch.setattr(mod, "_bundled_catalog_path", lambda: catalog)
    monkeypatch.setattr(mod, "_catalog_override_path", lambda: tmp_path / "missing.json")

    selected = mod.select_yuuka_voice_entry(situations=["late"], moods=["strict"], query="late")

    assert selected is None



def test_select_yuuka_voice_entry_blocks_immediate_repeat_even_with_repeated_query(monkeypatch, tmp_path):
    catalog = _write_catalog(
        tmp_path,
        [
            {
                "id": "repeat-me",
                "variant": "base",
                "clip_class": "semantic_extension",
                "file_name": "Yuuka_Late_Base.ogg",
                "situations": ["late"],
                "moods": ["strict"],
                "paraphrase_en": "late late late",
            },
            {
                "id": "fallback",
                "variant": "sportswear",
                "clip_class": "semantic_extension",
                "file_name": "Yuuka_Sportswear_Generic.ogg",
                "situations": ["late"],
                "moods": ["strict"],
                "paraphrase_en": "you are late",
            },
        ],
    )
    monkeypatch.setattr(mod, "_bundled_catalog_path", lambda: catalog)
    monkeypatch.setattr(mod, "_catalog_override_path", lambda: tmp_path / "missing.json")

    selected = mod.select_yuuka_voice_entry(
        situations=["late"],
        moods=["strict"],
        query="late late late late late",
        recent_clip_ids=["repeat-me"],
    )

    assert selected is not None
    assert selected["id"] == "fallback"



def test_select_yuuka_voice_entry_returns_none_without_context(monkeypatch, tmp_path):
    catalog = _write_catalog(
        tmp_path,
        [
            {
                "id": "generic",
                "variant": "base",
                "clip_class": "semantic_extension",
                "file_name": "Yuuka_Generic.ogg",
                "situations": ["late"],
                "moods": ["strict"],
                "paraphrase_en": "you are late",
            }
        ],
    )
    monkeypatch.setattr(mod, "_bundled_catalog_path", lambda: catalog)
    monkeypatch.setattr(mod, "_catalog_override_path", lambda: tmp_path / "missing.json")

    assert mod.select_yuuka_voice_entry() is None



def test_build_redirect_url_percent_encodes_parentheses():
    url = mod._build_redirect_url("Yuuka_(Pajama)_LogIn_1_1.ogg")
    assert url.endswith("Yuuka_%28Pajama%29_LogIn_1_1.ogg")



def test_yuuka_voice_reply_tool_downloads_and_returns_media(monkeypatch, tmp_path):
    clip_path = tmp_path / "clips" / "voice.ogg"
    clip_path.parent.mkdir(parents=True, exist_ok=True)
    clip_path.write_bytes(b"OggS")

    monkeypatch.setattr(
        mod,
        "select_yuuka_voice_entry",
        lambda **kwargs: {
            "id": "sportswear-late",
            "variant": "sportswear",
            "clip_class": "semantic_extension",
            "file_name": "Yuuka_(Sportswear)_LogIn_2_1.ogg",
            "situations": ["late"],
            "moods": ["strict"],
            "paraphrase_en": "you are late",
            "text_jp": "遅いですよ！",
            "score": 12,
            "reasons": ["variant:sportswear", "situation:late"],
            "alternatives": [],
        },
    )
    monkeypatch.setattr(mod, "_download_clip", lambda _file_name: clip_path)

    data = json.loads(
        mod.yuuka_voice_reply_tool(
            variant="sportswear",
            clip_class="semantic_extension",
            situations=["late"],
            moods=["strict"],
            query="late",
            download=True,
        )
    )

    assert data["success"] is True
    assert data["clip"]["id"] == "sportswear-late"
    assert data["file_path"] == str(clip_path)
    assert data["media_tag"] == f"MEDIA:{clip_path}"
    assert data["source_url"].endswith("Yuuka_%28Sportswear%29_LogIn_2_1.ogg")



def test_yuuka_voice_reply_tool_reports_no_match(monkeypatch):
    monkeypatch.setattr(mod, "select_yuuka_voice_entry", lambda **kwargs: None)

    data = json.loads(mod.yuuka_voice_reply_tool(situations=["late"], moods=["strict"]))

    assert data["success"] is False
    assert data["error"] == "no_matching_clip"



def test_download_clip_rejects_oversized_response(monkeypatch, tmp_path):
    target = tmp_path / "oversized.ogg"
    monkeypatch.setattr(mod, "_cache_path_for", lambda _file_name: target)
    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        lambda request, timeout=60: _FakeHTTPResponse(
            [b"OggS"], headers={"Content-Type": "audio/ogg", "Content-Length": str(mod._MAX_CLIP_BYTES + 1)}
        ),
    )

    with pytest.raises(ValueError, match="too large"):
        mod._download_clip("Yuuka_Oversized.ogg")



def test_download_clip_rejects_non_ogg_payload(monkeypatch, tmp_path):
    target = tmp_path / "not-audio.ogg"
    monkeypatch.setattr(mod, "_cache_path_for", lambda _file_name: target)
    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        lambda request, timeout=60: _FakeHTTPResponse(
            [b"<html>not audio</html>"], headers={"Content-Type": "text/html"}
        ),
    )

    with pytest.raises(ValueError, match="Unexpected content type"):
        mod._download_clip("Yuuka_NotAudio.ogg")



def test_normalize_download_flag_parses_false_string():
    assert mod._normalize_download_flag("false") is False
    assert mod._normalize_download_flag("true") is True
