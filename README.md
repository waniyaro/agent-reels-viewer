# Agent Reels Viewer 🎬

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-brightgreen.svg)](https://www.python.org/)
[![Compatible with Claude Code & Antigravity](https://img.shields.io/badge/Agent%20Ready-Claude%20Code%20%7C%20Antigravity%20%7C%20OpenClaw-orange.svg)](#agent-integration)

**Agent Reels Viewer** is an open-source, local-first AI agent skill designed to let coding and autonomous agents (such as **Claude Code**, **Google Antigravity**, and **OpenClaw**) seamlessly "watch", understand, and analyze short-form videos (**Instagram Reels**, **TikTok**, and **YouTube Shorts**).

---

## 💡 Why YouTube Summarizers Fail on Reels & TikTok

Most existing video tools only download YouTube subtitles via open APIs. That approach breaks completely on short-form social video:

| Challenge | YouTube Videos | Instagram Reels & TikTok |
| :--- | :--- | :--- |
| **Subtitles** | Clean text track via public API | Hardcoded (burned-in) text overlays or stickers |
| **Core Meaning** | 90% in voice / spoken lecture | 80% in **visuals, memes, POV text, and screen demos** |
| **Audio Track** | Structured speech | Often just trending background music / phonk |
| **Access** | Open HTTP requests | Aggressive bot protection, rate-limits, and login walls |

If an agent only transcribes the audio of a Reel, it hears background music and understands nothing. **Agent Reels Viewer solves this with an adaptive multimodal pipeline.**

---

## 🏗️ Architecture

```mermaid
flowchart TD
    A["Link or Local File (Reels / TikTok / Shorts)"] --> B["Downloader (yt-dlp)"]
    B -->|Extract Metadata| M["meta.json (Creator, Music, Views)"]
    B -->|Optimized Video (480p-720p)| C{"Media Splitter"}
    
    C -->|Audio Track| D["Audio Engine (FFmpeg)"]
    D -->|VAD-Filtered STT| E["Speech Transcript (faster-whisper)"]
    
    C -->|Visual Track| F["Vision Engine (FFmpeg)"]
    F -->|Scene Detection & pHash| G["Keyframes (768px JPEG, 8-14 frames)"]
    
    E & G & M --> H["Timeline Builder"]
    H --> I["Artifact: timeline.md"]
    
    I & G --> J["AI Agent (Vision LLM)"]
    J --> K["Structured Answer to User"]
```

### Key Architectural Decisions:
1. **The skill produces artifacts, not LLM calls**: The CLI generates clean local files (`timeline.md`, `frames/`, `meta.json`). The hosting agent uses its own built-in vision and file-reading tools to inspect them. **No extra API keys or monthly subscriptions required.**
2. **Local execution over cloud proxies**: Downloading runs from the user's IP, avoiding data center IP bans from Instagram and protecting user cookies.
3. **Adaptive frame density**: Silent/music-only clips receive more frequent keyframes to capture meme hooks and text overlays.

---

## 🚀 Quick Start

### 1. Prerequisites
Make sure `ffmpeg` is installed on your system:
```bash
# macOS
brew install ffmpeg

# Ubuntu / Debian
sudo apt install ffmpeg
```

### 2. Installation
Clone and install dependencies:
```bash
git clone https://github.com/waniyaro/agent-reels-viewer.git
cd agent-reels-viewer
pip install -r requirements.txt
```

Run the built-in diagnostic doctor:
```bash
python3 scripts/viewer.py doctor
```

---

## 🛠️ CLI Usage

### Inspect a Video
```bash
# Standard inspection (Metadata + Keyframes + Transcript + Timeline)
python3 scripts/viewer.py inspect "https://www.instagram.com/reel/C3..."

# Quick mode (instant metadata & sound track, without video download)
python3 scripts/viewer.py inspect "https://www.tiktok.com/@user/video/..." --mode quick

# With authenticated cookies (for private or restricted Reels)
python3 scripts/viewer.py inspect "https://www.instagram.com/reel/..." --cookies /path/to/cookies.txt

# JSON output for automated agent tooling
python3 scripts/viewer.py inspect "https://www.instagram.com/reel/..." --json
```

### Second-Pass On-Demand Zoom (Frames Inspector)
When an agent or user wants to examine a fine-grained 5-second slice (e.g., reading a code snippet or recipe):
```bash
python3 scripts/viewer.py frames "output/session_123/video.mp4" --from-sec 12.0 --to-sec 17.0 --count 6 --hires
```

### Cleanup Cache
```bash
# Remove cached sessions older than 3 days
python3 scripts/viewer.py clean --days 3
```

---

## 🤖 Agent Integration (SKILL.md)

This repository includes a production-ready `SKILL.md` compliant with **Claude Code**, **Antigravity IDE**, and **OpenClaw**.

### Installing in Antigravity or Claude Code:
Copy or link the folder into your skills directory:
```bash
# For Antigravity
ln -s "$(pwd)" ~/.gemini/config/skills/agent-reels-viewer

# For Claude Code / OpenClaw
ln -s "$(pwd)" ~/.claude/skills/agent-reels-viewer
```

Once installed, your agent will automatically invoke `agent-reels-viewer` whenever you paste an Instagram Reel, TikTok, or Shorts link!

---

## 🛡️ Security & Prompt Injection Defense

Social media videos are untrusted external inputs. `agent-reels-viewer`:
- **Hardens against Prompt Injection**: The generated `timeline.md` and `SKILL.md` explicitly instruct the reasoning model to treat all on-screen text and speech as passive data to analyze, never as executable instructions.
- **Zero Shell Injection**: All subprocess calls avoid `shell=True` and pass sanitized argument vectors.
- **Privacy First**: Cookies remain local on your machine and are never transmitted to external services.

---

## 📄 License & Disclaimer

Released under the [MIT License](LICENSE).

*Disclaimer: This tool is intended for personal research, educational inspection, and fair-use content analysis. Users are responsible for complying with the terms of service of the respective platforms.*
