# Agent Reels Viewer

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://www.python.org/)
[![Compatible with Claude Code & Antigravity](https://img.shields.io/badge/Agent%20Ready-Claude%20Code%20%7C%20Antigravity%20%7C%20OpenClaw-orange.svg)](#agent-integration)

**Agent Reels Viewer** is an open-source, local-first AI agent skill designed to let coding and autonomous agents (such as **Claude Code**, **Google Antigravity**, and **OpenClaw**) inspect, "watch", understand, and analyze short-form videos (**Instagram Reels**, **TikTok**, **YouTube Shorts**, and local video files).

---

## Why YouTube Summarizers Fail on Reels & TikTok

Most video summarizers only download YouTube subtitles via open APIs. That approach breaks on short-form social video:

| Challenge | Long-Form YouTube | Instagram Reels, TikTok & Shorts |
| :--- | :--- | :--- |
| **Subtitles** | Clean text track via public API | Hardcoded (burned-in) on-screen text overlays, stickers, or code editors |
| **Core Meaning** | 90% in voice / spoken lecture | 80% in **visuals, memes, POV text, and screen demonstrations** |
| **Audio Track** | Structured speech | Often just background music / phonk / sound effects |
| **Access** | Open HTTP requests | Bot protections, rate-limits, and login checkpoints |

If an agent only transcribes the audio of a silent Reel or meme, it hears background music and understands nothing. **Agent Reels Viewer solves this with an adaptive multimodal pipeline.**

---

## Architecture

```mermaid
flowchart TD
    A["Link or Local File (Reels / TikTok / Shorts)"] --> B{"Input Type"}
    B -->|Local File| C["Local Media Processor"]
    B -->|Social URL| D["Downloader (yt-dlp)"]
    D -->|Metadata| M["meta.json (Creator, Music, Views)"]
    D -->|Optimized Video (480p-720p)| C
    
    C -->|Audio Track| E["Audio Engine (FFmpeg)"]
    E -->|VAD-Filtered STT| F["Speech Transcript (faster-whisper)"]
    
    C -->|Visual Track| G["Vision Engine (FFmpeg)"]
    G -->|Scene Detection & showinfo PTS| H["Keyframes (768px JPEG, 8-20 frames)"]
    
    F & H & M --> I["Timeline Builder"]
    I --> J["Artifact: timeline.md"]
    
    J & H --> K["AI Agent (Vision LLM)"]
    K --> L["Structured Answer to User"]
```

### Key Architectural Decisions:
1. **The skill produces artifacts, not LLM calls**: The CLI generates clean local files (`timeline.md`, `frames/`, `meta.json`). The hosting agent uses its own built-in vision and file-reading tools to inspect them. **No extra API keys or monthly subscriptions required.**
2. **Real PTS Presentation Timestamps**: Frame timestamps are extracted directly from FFmpeg `showinfo` output (`pts_time`), accurately matching speech timestamps and visual scene transitions.
3. **Interval-Bucket Keyframe Capping & Tiled Deduplication**: Partitions the video timeline into `effective_max` equal intervals to guarantee temporal coverage (maximum gap <= 2*duration/cap) while greedily retaining the most visually significant frame within each interval based on non-saturating tile difference metrics. Eliminates static duplicates and tight clustering while preserving subtle on-screen text edits.
4. **Isolated Process Transcription with Hard Timeout**: Transcription executes in an isolated worker process with hard OS timeouts (`max(30, 3*duration)`), preventing Python thread hangs. Model weights are cached outside the timeout window.
5. **Adaptive Frame Density**: Silent/music-only clips receive more frequent keyframes (1.5s step, cap 20) to capture on-screen text and fast scene cuts. Talking-head clips use conversational cadence (3.5s step, cap 12).
6. **Codec Compatibility & H.264 Priority**: The media downloader explicitly prioritizes H.264 (`avc1`) in MP4 containers over AV1, ensuring universal hardware acceleration and compatibility across minimal FFmpeg distributions. The `doctor` diagnostic checks for AV1 decoders (`libdav1d`/`libaom`) and provides troubleshooting hints if AV1 decoding fails.

---

## Quick Start

### 1. Prerequisites
Make sure `ffmpeg` is installed on your system:
```bash
# macOS
brew install ffmpeg

# Ubuntu / Debian
sudo apt update && sudo apt install -y ffmpeg

# Windows (via Chocolatey)
choco install ffmpeg
```

### 2. Installation
Install core package (video downloading + visual keyframes):
```bash
pip install .
```

To enable local speech-to-text transcription (Whisper):
```bash
pip install ".[speech]"
```

Run the built-in diagnostic doctor:
```bash
agent-reels-viewer doctor
# Optional: pre-cache Whisper model weights before first run
agent-reels-viewer doctor --download-model --model base
```

---

## CLI Usage

### Inspect a Video (Social URL or Local File)
```bash
# Standard inspection (Metadata + Keyframes + Transcript + Timeline)
agent-reels-viewer inspect "https://www.youtube.com/shorts/..." --json

# Inspect a local video file (bypasses URL whitelists and downloads)
agent-reels-viewer inspect ./my_video.mp4 --json

# Speech transcription with recommended model (base is recommended minimum for dialogue accuracy)
agent-reels-viewer inspect "https://www.youtube.com/shorts/..." --model base --json

# Fast CPU smoke-test using tiny model
agent-reels-viewer inspect "https://www.tiktok.com/@user/video/..." --model tiny --json

# Pure visual mode (skip audio transcription pass)
agent-reels-viewer inspect "https://www.instagram.com/reel/..." --no-speech --json

# Quick mode (instant metadata & sound track, without video download)
agent-reels-viewer inspect "https://www.tiktok.com/@user/video/..." --mode quick --json

# Authenticated cookies (for private or restricted Reels)
agent-reels-viewer inspect "https://www.instagram.com/reel/..." --cookies /path/to/cookies.txt --json
```

### Second-Pass On-Demand Zoom (Frames Inspector)
When an agent or user wants to examine a fine-grained 5-second slice (e.g. reading tiny UI text or code):
```bash
agent-reels-viewer frames "session_abc123" --from-sec 12.0 --to-sec 17.0 --count 6 --hires
```

### Cleanup Cache & Retention
```bash
# Remove all cached sessions immediately
agent-reels-viewer clean --days 0

# Remove cached sessions older than 3 days
agent-reels-viewer clean --days 3
```

---

## Where Data is Stored & Privacy Policy

### Session Data Layout
By default, inspected media sessions are stored locally in `~/.cache/agent-reels-viewer/session_<hash>/`:
- `video.mp4`: Downloaded video stream (~5–20 MB). Retained to allow subsequent targeted second-pass zooming (`frames` command).
- `frames/`: Extracted visual scene keyframes (768px JPEG).
- `zoom_frames/`: High-resolution zoom frames extracted during second-pass inspections.
- `timeline.md`: Chronological multimodal timeline artifact.
- `meta.json`: Session metadata, speech status, and token estimations.
*(Note: `audio.mp3` is deleted automatically in a `finally` block immediately after speech transcription completes or errors).*

### Data Retention & Cleanup
- **Automatic pruning on inspect**: Sessions older than the configured TTL are automatically cleaned during each `inspect` call. Age is calculated using the newest `mtime` among all files inside each session folder (not merely the folder directory timestamp).
- **Configurable TTL**: You can customize retention by setting the `AGENT_REELS_TTL_HOURS` environment variable (default: `24` hours).
- **Manual Cleanup**:
  - `agent-reels-viewer clean --days 0` cleans all cached sessions immediately.
  - `agent-reels-viewer clean --days N` cleans sessions older than N days.
  *(Custom `--output` directories are never automatically cleaned).*

### Disk Space Optimization (`--no-video`)
- Passing `--no-video` deletes `video.mp4` immediately after initial keyframe extraction to conserve disk space.
- *Notice*: `--no-video` permanently disables subsequent second-pass zooming on that session (`frames` will return `VIDEO_NOT_FOUND`).

### Data Privacy & Host Model Transmission
- All downloaded video files, extracted frames, audio tracks, and transcripts remain strictly local on your machine.
- **Host LLM Transmission Disclosure**: When your host agent (Claude Code, Antigravity, OpenClaw, etc.) reads `timeline.md` or views image files in `frames/`, those text excerpts and visual images are transmitted to the host agent's AI model provider (such as Anthropic, Google, or OpenAI) as part of your conversation context.

---

## Agent Integration (SKILL.md)

This repository includes a production-ready `SKILL.md` compliant with **Claude Code**, **Antigravity IDE**, and **OpenClaw**.

### Installing in Antigravity or Claude Code:
Copy or link the folder into your skills directory:
```bash
# For Antigravity
ln -s "$(pwd)" ~/.gemini/config/skills/agent-reels-viewer

# For Claude Code / OpenClaw
ln -s "$(pwd)" ~/.claude/skills/agent-reels-viewer
```

---

## Platform Verification Status
 
| Platform / Feature | Status | Verification Details |
| :--- | :---: | :--- |
| **YouTube Shorts / Clips** | **Verified** | Tested locally on multiple real videos (`RRqwSLMK1hQ` vertical coding meme with on-screen text, `8Zx04h24uBs` Steve Jobs speech, `x7X9w_GIm1s` Fireship). Real speech transcribed via `faster-whisper` + scene keyframes extracted. |
| **Local Video Files (`.mp4`)** | **Verified (Synthetic Fixtures Only)** | Tested locally on synthetic video fixtures (`testsrc` with audio tone, Pillow generated text frames); skips URL downloading, analyzes container audio streams, extracts keyframes and builds `timeline.md`. |
| **TikTok Videos** | **Partially Verified (1 clip tested)** | Tested locally on 1 TikTok clip (`7106594312292453675`, 24s). Music-only track correctly detected by VAD (`has_speech: false`). |
| **Instagram Reels** | **Unverified with Cookies** | Missing cookies error handling is verified (`NEEDS_COOKIES`); extraction with real user cookies has not been verified. |
| **Second-Pass Zoom (`frames`)** | **Verified** | Tested locally on both downloaded video sessions and local file sessions (`--from-sec` / `--to-sec`). |
| **CI: Linux, macOS & Windows (Py 3.10–3.13)** | **CI unit-тесты зелёные** | Unit-тесты и шаг doctor успешно пройдены на всех 12 матричных конфигурациях (Ubuntu, macOS, Windows на Python 3.10, 3.11, 3.12, 3.13) для коммита a8f9873: [Run 37277567454](https://github.com/waniyaro/agent-reels-viewer/actions/runs/37277567454). Реальные загрузки и Whisper на настоящем аудио в CI не покрыты; шаг live-canary в CI не запускался (пропущен). |

> **Note on CI Live Canary**: Social platforms like YouTube and Instagram aggressively challenge data center IPs (such as GitHub Actions runners). In CI, the `live-canary` job is configured with `continue-on-error: true` so data center IP blocks do not fail build validation.

---

## Security & Prompt Injection Defense

Social media videos are untrusted external inputs. `agent-reels-viewer`:
- **Hardens against Prompt Injection**: The generated `timeline.md` and `SKILL.md` explicitly instruct the reasoning model to treat all on-screen text and speech as passive data to analyze, never as executable instructions.
- **Zero Shell Injection**: All subprocess calls avoid `shell=True` and pass sanitized argument vectors.
- **Privacy First**: Cookies remain local on your machine and are never transmitted to external services.
- **Safe Session Cleanup**: Cache cleanup strictly prunes `~/.cache/agent-reels-viewer` and never touches custom `--output` paths.

---

## License & Disclaimer

Released under the [MIT License](LICENSE).

*Disclaimer: This tool is intended for personal research, educational inspection, and fair-use content analysis. Users are responsible for complying with the terms of service of the respective platforms.*
