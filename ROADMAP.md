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

### 3. Multi-Clip Batching
- Support passing multiple URLs or files in a single invocation to generate comparative timelines.
