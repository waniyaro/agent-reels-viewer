# Roadmap — Agent Reels Viewer

This document tracks planned features, improvements, and architectural milestones for upcoming releases.

---

## Upcoming Milestones (v0.2.0)

### 1. Alternative Speech Engines: GigaAM Backend
- **Context**: Whisper (`base`/`small`) is great for universal multi-language transcription, but Sber's **GigaAM** (`ai-forever/GigaAM`) achieves SOTA accuracy and speed on Russian speech recognition.
- **Goal**:
  - Add `--stt-engine {whisper,gigaam}` flag (default: `whisper`).
  - Introduce optional extras group: `pip install "agent-reels-viewer[gigaam]"`.
  - Maintain subprocess isolation architecture for reliable worker execution.
  - Tracked in: [GitHub Issue #1](https://github.com/waniyaro/agent-reels-viewer/issues/1).

### 2. OCR Pre-Filtering (Local Optical Character Recognition)
- Lightweight fast OCR (e.g. `rapidocr` or `tesseract`) to automatically tag keyframes containing code snippets or large header text.

### 3. Universal Multi-Clip Batching: YouTube Playlists & Social Collections (Reels / TikTok / Shorts)
- **Context**: Users want to research topics across multiple clips at once rather than one-by-one:
  - **YouTube Playlists**: Direct playlist URLs (`youtube.com/playlist?list=...`) or channel Shorts feeds.
  - **TikTok Collections**: Private folders and favorites exported via tools like [tiktok-to-ytdlp](https://github.com/dinoosauro/tiktok-to-ytdlp).
  - **Instagram Reels Collections**: Saved collections exported from browser sessions into link lists.
  - **Mixed Link Lists**: Plain text files with arbitrary combinations of Reels, TikToks, Shorts, and local `.mp4` clips.
- **Goal**:
  - **Native Playlist Support**: Automatically detect YouTube & TikTok playlist URLs and unroll items (`--limit <N>`, default 5).
  - **Universal Batch Mode**: `--batch <links.txt>` processing URLs in order with graceful per-item error handling (continue-on-error).
  - **Unified Collection Artifact (`collection_summary.md`)**:
    - Cross-clip summary with key takeaways.
    - Side-by-side or chronological index with direct links to individual session timelines (`timeline.md`) and keyframes.
    - Token budget guardrails so batch runs do not overwhelm the host agent's context.
  - Tracked in: [GitHub Issue #2](https://github.com/waniyaro/agent-reels-viewer/issues/2).

