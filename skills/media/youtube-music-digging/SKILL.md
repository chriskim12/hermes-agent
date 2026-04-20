---
name: youtube-music-digging
description: Handle YouTube music links in a dedicated digging channel by converting them to MP3, normalizing core metadata, and returning the file to chat. Use with Discord channel_skill_bindings for music-digging channels.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [YouTube, Music, MP3, Metadata, Discord, Media]
    related_skills: [youtube-content]
---

# YouTube Music Digging

Use this skill in channels where users post YouTube music links and expect a cleaned MP3 back.

## Goal

When a message contains a supported YouTube URL, prefer producing a downloadable MP3 with sane metadata over giving a long conversational answer.

## Rules

1. If the message contains a supported YouTube URL, call `youtube_to_mp3`.
2. Treat this as a media-processing workflow, not a summarization task.
3. Keep the reply short and operational:
   - title
   - artist
   - source URL
   - any caveat if artist/title was inferred
4. If the tool returns a local `file_path`, include it as:
   - `MEDIA:/absolute/path/to/file.mp3`
5. Do not claim Google Drive upload exists yet unless the user explicitly asked for a later phase and the system has actually been configured for it.
6. If conversion fails, explain the failing stage briefly and concretely.
7. Do not overpromise metadata accuracy. If the artist was inferred from uploader/title parsing, say so.

## Preferred Response Shape

```text
정리했어요.
- Title: ...
- Artist: ...
- Source: ...
- Note: artist inferred from uploader metadata

MEDIA:/absolute/path/to/file.mp3
```

Omit the note line when not needed.

## When Not to Use

- Non-YouTube links
- Requests for transcript/summary instead of audio extraction
- Google Drive upload requests that have not been configured yet

## Suggested Discord Binding

Bind this skill to the target music channel or forum thread via config:

```yaml
platforms:
  discord:
    channel_skill_bindings:
      - id: "<channel-or-thread-id>"
        skills: ["youtube-music-digging"]
```

Do not hardcode real user/server IDs into the repository.
