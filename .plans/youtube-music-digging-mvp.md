# YouTube Music Digging MVP Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** When a user posts a YouTube link in a Discord music-digging channel, Hermes should download the audio, convert it to MP3, normalize core metadata, and deliver the MP3 back to Discord. Google Drive upload is explicitly out of scope for this MVP.

**Architecture:** Add a new first-class tool that wraps `yt-dlp` + `ffmpeg` + `mutagen`, stores temporary artifacts under `get_hermes_home()`, and returns a local MP3 path plus normalized metadata. Expose the tool in messaging sessions so the model can call it directly, then use a channel-bound skill to bias Discord sessions in the music-digging channel toward calling the tool whenever a YouTube URL appears.

**Tech Stack:** Python 3.11, Hermes tool registry, `yt-dlp` CLI, `ffmpeg`, `mutagen`, Discord MEDIA delivery flow, optional Discord channel skill binding.

---

## Scope Lock

### In scope
- YouTube watch/share URLs only
- Single-link processing per call
- Audio extraction to MP3
- Core ID3 tags: `title`, `artist`, `date/year`, `comment/source_url`
- Safe file naming
- Return `MEDIA:/absolute/path/to/file.mp3` compatible output for Discord delivery
- Clear structured failures for download/convert/tagging stages

### Out of scope
- Google Drive upload
- SoundCloud / Bandcamp / arbitrary sites
- Playlist ingestion
- Batch queueing
- Perfect artist/title inference
- Album / track number / genre automation
- Artwork embedding beyond future follow-up work

---

## Architecture Decisions

### 1. Use a dedicated tool, not a gateway hook
Why:
- Hooks only see coarse `agent:start` / `agent:end` payloads in `gateway/run.py`; they are not the right layer for media transformation.
- Hermes already supports channel-specific skill auto-loading in Discord via `channel_skill_bindings` (`gateway/platforms/discord.py`, `gateway/run.py`).
- A tool keeps the actual side effects explicit, testable, and available in CLI/other platforms if needed.

### 2. Rely on `yt-dlp` executable, not the Python module
Why:
- The environment already has `ffmpeg` but not `yt-dlp`.
- Hermes tools commonly gate availability on external commands.
- Subprocess invocation is easier to mock in tests and matches real operator expectations.

### 3. Add `mutagen` as a Python dependency
Why:
- ID3 writing is core MVP functionality, not an optional research extra.
- `mutagen` is lightweight and stable.

### 4. Store artifacts under `get_hermes_home()`
Suggested layout:
- `~/.hermes/media_cache/youtube-audio/incoming/`
- `~/.hermes/media_cache/youtube-audio/processed/`
- `~/.hermes/media_cache/youtube-audio/failed/`

Do **not** hardcode `~/.hermes` in code.

### 5. Make the tool available to messaging platforms
Because Discord gateway sessions use `_HERMES_CORE_TOOLS`, the new tool must either:
- be added to `_HERMES_CORE_TOOLS`, or
- be included by a composed toolset used by Discord.

For MVP, add it to `_HERMES_CORE_TOOLS` and let the tool’s `check_fn` hide it when prerequisites are missing.

---

## User-Facing Behavior

### Input examples
- `https://www.youtube.com/watch?v=...`
- `https://youtu.be/...`
- `https://music.youtube.com/watch?v=...` (support if URL parsing is trivial)

### Expected response behavior
If the message contains a single YouTube URL and no conflicting instruction:
1. Call the tool
2. Deliver the MP3 as media
3. Reply with a short summary, for example:
   - normalized title
   - inferred artist
   - source URL
   - any caveat if parsing was approximate

### Failure behavior
Return stage-specific errors:
- `unsupported_url`
- `missing_dependency`
- `download_failed`
- `conversion_failed`
- `metadata_failed`
- `file_too_large` (if Discord delivery later surfaces a hard limit)

---

## Normalization Rules (MVP)

### URL validation
Accept only recognized YouTube hosts:
- `youtube.com`
- `www.youtube.com`
- `m.youtube.com`
- `music.youtube.com`
- `youtu.be`

Reject everything else with `unsupported_url`.

### Title cleanup
Strip common noise tokens from the video title when they appear as suffix/promo markers:
- `Official Video`
- `Official Audio`
- `Audio`
- `MV`
- `M/V`
- `Visualizer`
- `Lyrics`
- `Lyric Video`
- `HD`, `4K` when clearly promotional

Also:
- collapse repeated whitespace
- normalize dash spacing around ` - `
- trim surrounding brackets left empty after cleanup

### Artist/title inference
1. If cleaned title matches `Artist - Title`, split once on the first delimiter.
2. Else use:
   - `artist = uploader or channel`
   - `title = cleaned title`
3. Never pretend high confidence when inference is weak; include `artist_inferred: true/false` in tool output.

### Filename policy
Final filename:
- `{artist} - {title}.mp3`

Sanitize:
- remove path separators and control chars
- collapse long whitespace
- cap final basename length reasonably (e.g. 180 chars)

### ID3 policy
Write at minimum:
- `TIT2` / title
- `TPE1` / artist
- `TDRC` or year when available
- `COMM` / source YouTube URL + extractor note

---

## File and Code Changes

### Task 1: Add dependency and document external requirements

**Objective:** Ensure the codebase declares Python-level tagging support and clearly documents the required external binaries.

**Files:**
- Modify: `pyproject.toml`
- Modify: `AGENTS.md` (only if a short note is needed for future contributors; skip if redundant)
- Test: none yet

**Step 1: Add `mutagen` to base dependencies**
Add a conservative version range in `pyproject.toml` under `[project].dependencies`.

**Step 2: Decide where to document `yt-dlp`**
Preferred: tool schema + error message + plan doc. Avoid bloating `AGENTS.md` unless contributor setup truly depends on it.

**Step 3: Verification**
Run:
```bash
source venv/bin/activate
python - <<'PY'
import mutagen
print(mutagen.__version__)
PY
```
Expected: version prints successfully after install/sync.

**Step 4: Commit**
```bash
git add pyproject.toml .plans/youtube-music-digging-mvp.md
git commit -m "feat: add mutagen dependency for youtube audio tagging"
```

---

### Task 2: Implement the new YouTube audio tool

**Objective:** Create a tool that downloads YouTube audio, converts it to MP3, writes metadata, and returns structured JSON.

**Files:**
- Create: `tools/youtube_audio_tool.py`
- Modify: `model_tools.py`
- Modify: `toolsets.py`
- Test: `tests/tools/test_youtube_audio_tool.py`

**Step 1: Create tool skeleton**
In `tools/youtube_audio_tool.py`:
- import `json`, `os`, `re`, `subprocess`, `tempfile`, `shutil`, `uuid`
- import `Path`
- import `get_hermes_home` from `hermes_constants`
- import `registry` from `tools.registry`
- import `mutagen.id3` helpers

**Step 2: Implement requirement checks**
Create `check_youtube_audio_requirements()` that verifies:
- `ffmpeg` exists
- `yt-dlp` exists
- `mutagen` import succeeds

**Step 3: Implement URL parsing + validation helpers**
Helpers:
- `_is_supported_youtube_url(url: str) -> bool`
- `_extract_video_id(url: str) -> str | None` (optional but useful for naming/temp dirs)

**Step 4: Implement normalization helpers**
Helpers:
- `_clean_title(raw_title: str) -> str`
- `_infer_artist_and_title(clean_title: str, uploader: str | None, channel: str | None) -> tuple[str, str, bool]`
- `_sanitize_filename_component(text: str) -> str`

**Step 5: Implement download step**
Use `yt-dlp` subprocess with JSON metadata extraction first, then bestaudio download.
Suggested approach:
```bash
yt-dlp --dump-single-json --no-playlist URL
yt-dlp --extract-audio --audio-format mp3 --audio-quality 0 --no-playlist -o <temp-template> URL
```
Notes:
- Prefer downloading original/bestaudio first, then let `ffmpeg` produce the final named output for deterministic file placement.
- Capture stderr for structured error messages.

**Step 6: Implement conversion step**
Use `ffmpeg` explicitly on the downloaded source to produce the final MP3 in `processed/`.
Suggested flags:
```bash
ffmpeg -y -i input.ext -vn -codec:a libmp3lame -b:a 320k output.mp3
```
Do not trust yt-dlp postprocessing alone for final file path semantics.

**Step 7: Implement ID3 writing step**
Using `mutagen`, write:
- title
- artist
- date/year when available
- comment containing source URL

**Step 8: Return structured JSON**
Return JSON like:
```json
{
  "success": true,
  "file_path": "/abs/path/to/file.mp3",
  "title": "...",
  "artist": "...",
  "artist_inferred": true,
  "source_url": "...",
  "video_id": "..."
}
```
On failure:
```json
{
  "success": false,
  "error": "download_failed",
  "detail": "stderr excerpt"
}
```

**Step 9: Register the tool**
Register a schema like `youtube_to_mp3` with:
- `url` (required)
- optional `preferred_bitrate` (default `320k`, allow only vetted values if implemented)

Tool description must explicitly say it returns a local MP3 path suitable for `MEDIA:` delivery.

**Step 10: Wire tool discovery**
Add `"tools.youtube_audio_tool"` to `model_tools.py` `_discover_tools()`.

**Step 11: Expose tool in toolsets**
- Add `youtube_to_mp3` to `_HERMES_CORE_TOOLS` in `toolsets.py`
- Optionally add a named toolset entry such as:
  - `"youtube_audio": {"tools": ["youtube_to_mp3"], ...}`

**Step 12: Write focused tool tests**
`tests/tools/test_youtube_audio_tool.py` should cover:
- supported vs unsupported URL detection
- title cleanup behavior
- artist/title inference
- filename sanitization
- graceful missing dependency behavior
- mocked subprocess success path producing structured JSON
- mocked conversion failure path
- mocked metadata write failure path

**Step 13: Verification**
Run:
```bash
source venv/bin/activate
python -m pytest tests/tools/test_youtube_audio_tool.py -q
python -m pytest tests/test_model_tools.py -q
```
Expected: all pass.

**Step 14: Commit**
```bash
git add tools/youtube_audio_tool.py model_tools.py toolsets.py tests/tools/test_youtube_audio_tool.py
git commit -m "feat: add youtube to mp3 tool for messaging workflows"
```

---

### Task 3: Make Discord delivery ergonomic

**Objective:** Ensure the agent can trivially deliver the produced MP3 in Discord after tool execution.

**Files:**
- Test: `tests/gateway/test_discord_document_handling.py`
- Test: `tests/gateway/test_discord_media_metadata.py`
- Optional new test: `tests/gateway/test_discord_music_digging.py`
- Modify code only if current MEDIA flow reveals a gap

**Step 1: Confirm existing MEDIA path supports `.mp3`**
Current regex in `gateway/platforms/base.py` already includes `mp3`. Confirm no extra code is needed.

**Step 2: Add/adjust gateway tests if needed**
Test that a final response containing:
```text
Done.
MEDIA:/tmp/example.mp3
```
results in document delivery and strips the `MEDIA:` line from visible text.

**Step 3: Verification**
Run:
```bash
source venv/bin/activate
python -m pytest tests/gateway/test_discord_document_handling.py -q
python -m pytest tests/gateway/test_discord_media_metadata.py -q
```
Expected: pass unchanged or with minimal new assertions.

**Step 4: Commit**
```bash
git add tests/gateway/test_discord_document_handling.py tests/gateway/test_discord_media_metadata.py
git commit -m "test: cover discord mp3 media delivery for youtube audio workflow"
```

---

### Task 4: Add a reusable channel skill for music-digging sessions

**Objective:** Bias the model toward using the tool automatically in a dedicated Discord channel without requiring the user to restate the workflow every time.

**Files:**
- Create: `skills/media/youtube-music-digging/SKILL.md`
- Optional reference: `skills/media/youtube-music-digging/references/metadata-policy.md`
- Test: `tests/gateway/test_discord_channel_skills.py`

**Step 1: Create the skill**
The skill should instruct the model:
- when a message contains a YouTube URL, call `youtube_to_mp3`
- prefer concise acknowledgment + final delivery
- include caveats when artist inference is approximate
- do not claim Drive upload exists yet

**Step 2: Bind the skill in Discord config (user setup, not repo default)**
Document a config snippet like:
```yaml
platforms:
  discord:
    channel_skill_bindings:
      - id: "<music-channel-or-thread-id>"
        skills: ["youtube-music-digging"]
```
Do not hardcode the user’s real channel ID in the repo.

**Step 3: Add a gateway test if coverage is missing**
Extend `tests/gateway/test_discord_channel_skills.py` to confirm multi-skill or single-skill auto-binding still works with the new skill name.

**Step 4: Verification**
Run:
```bash
source venv/bin/activate
python -m pytest tests/gateway/test_discord_channel_skills.py -q
```
Expected: pass.

**Step 5: Commit**
```bash
git add skills/media/youtube-music-digging/SKILL.md tests/gateway/test_discord_channel_skills.py
git commit -m "feat: add discord auto-skill for youtube music digging"
```

---

### Task 5: End-to-end local verification

**Objective:** Prove the whole MVP works on a real YouTube URL before touching Drive integration.

**Files:**
- No permanent code changes required
- Optional note: append outcomes to this plan or a dedicated implementation log only if it stays maintained

**Step 1: Install external requirements locally**
Example:
```bash
source venv/bin/activate
python -m pip install mutagen
# install yt-dlp by package manager or pipx/pip, but prefer system executable availability
```
Verify:
```bash
command -v yt-dlp
command -v ffmpeg
```

**Step 2: Run the tool through Hermes or a direct unit harness**
Use a real test URL that is public and short.
Verify:
- MP3 file created
- file opens
- ID3 tags readable

**Step 3: Run targeted test suite**
```bash
source venv/bin/activate
python -m pytest tests/tools/test_youtube_audio_tool.py -q
python -m pytest tests/gateway/test_discord_document_handling.py -q
python -m pytest tests/gateway/test_discord_channel_skills.py -q
python -m pytest tests/test_model_tools.py -q
```

**Step 4: Run broader safety net**
```bash
source venv/bin/activate
python -m pytest tests/tools/ -q
python -m pytest tests/gateway/ -q
```
If time allows, run full suite:
```bash
source venv/bin/activate
python -m pytest tests/ -q
```

**Step 5: Commit**
```bash
git add -A
git commit -m "test: verify youtube music digging mvp end to end"
```

---

## Suggested Tool Contract

### Tool name
`youtube_to_mp3`

### Input schema
```json
{
  "type": "object",
  "properties": {
    "url": {
      "type": "string",
      "description": "A single YouTube video URL to extract as an MP3 file"
    },
    "preferred_bitrate": {
      "type": "string",
      "enum": ["192k", "256k", "320k"],
      "description": "Optional MP3 bitrate. Default: 320k"
    }
  },
  "required": ["url"]
}
```

### Output shape
```json
{
  "success": true,
  "file_path": "/abs/path/out.mp3",
  "title": "Song Title",
  "artist": "Artist Name",
  "artist_inferred": true,
  "source_url": "https://www.youtube.com/watch?v=...",
  "video_id": "abc123",
  "warnings": []
}
```

---

## Failure Taxonomy

```json
{
  "success": false,
  "error": "missing_dependency",
  "detail": "yt-dlp not found in PATH"
}
```

Error values:
- `unsupported_url`
- `missing_dependency`
- `metadata_lookup_failed`
- `download_failed`
- `conversion_failed`
- `metadata_failed`
- `unexpected_error`

Keep `detail` concise and operator-readable.

---

## Testing Notes

### Unit tests should mock subprocesses
Do not hit YouTube in normal test runs.
Mock:
- metadata JSON retrieval
- source file download
- ffmpeg conversion

### Use temp directories, not real `~/.hermes`
Tests must respect the existing `HERMES_HOME` isolation fixtures.

### Verify MEDIA compatibility by behavior, not assumptions
If an `.mp3` attachment path is already supported by base delivery, do not duplicate logic in the Discord adapter.

---

## Rollout Notes

### Phase 1 rollout
- Ship tool + tests
- Ship skill + config snippet
- Manually bind the skill to the target Discord channel/thread
- Verify on one known-good YouTube link

### Phase 2 rollout
After MVP is stable, add:
- Drive upload with `gws`
- duplicate detection
- artwork embedding
- playlist support only if truly needed

---

## Acceptance Criteria

The MVP is done when all are true:
- A Discord session with the channel skill sees a YouTube URL and calls the tool
- The tool produces a valid MP3 file
- The MP3 has sane `title` and `artist` tags
- Hermes delivers the MP3 back to Discord as an attachment
- Failure messages clearly identify the failing stage
- Tests for tool behavior and Discord media delivery pass

---

## Recommended First Implementation Slice

If implementing right now, do it in this order:
1. Task 2 (tool implementation)
2. Task 3 (Discord delivery verification)
3. Task 1 (`mutagen` dependency)
4. Task 4 (channel skill)
5. Task 5 (real end-to-end verification)

Reason: the tool is the critical path. Everything else is wrapper surface around it.
