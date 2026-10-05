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

### 3. Multi-Clip Batching & TikTok Collection Importer (`tiktok-to-ytdlp`)
- **Context**: Users often organize research, tutorials, and inspiration into private "Saved" folders and Collections in TikTok and Instagram. Directly scraping authenticated private collections is anti-bot heavy, but browser extractors like [tiktok-to-ytdlp](https://github.com/dinoosauro/tiktok-to-ytdlp) cleanly export lists of URLs from TikTok Collections, Favorites, and Profiles to text files.
- **Goal**:
  - Add `--batch <file.txt|urls.json>` flag to `agent-reels-viewer inspect` to process entire collections in sequence.
  - Add `--limit <N>` to sample the top N clips from large folders.
  - Generate a unified synthesized artifact (`collection_summary.md`) compiling keyframes, transcripts, and takeaways across all clips in the collection.
  - Document zero-friction workflow pairing `tiktok-to-ytdlp` exports with `agent-reels-viewer`.
  - Tracked in: [GitHub Issue #2](https://github.com/waniyaro/agent-reels-viewer/issues/2).
