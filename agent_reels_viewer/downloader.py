"""Media downloader and metadata extraction module via yt-dlp."""

import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

# Valid hostnames for short-form platforms
VALID_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "instagr.am",
    "tiktok.com",
    "www.tiktok.com",
    "vm.tiktok.com",
    "m.tiktok.com",
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
}

MAX_DURATION_SECONDS = 360  # 6-minute cap for short-form social videos


def detect_platform(url: str) -> str:
    """Detect social video platform from URL."""
    u = url.lower()
    if "instagram.com" in u or "instagr.am" in u:
        return "instagram"
    elif "tiktok.com" in u:
        return "tiktok"
    elif "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    elif os.path.exists(url):
        return "local"
    return "unknown"


def validate_url(url_or_path: str) -> Tuple[bool, str, str]:
    """Validate URL schema, domain whitelist, or existing local file path.
    
    Returns: (is_valid, error_code, error_message)
    """
    if os.path.isfile(url_or_path):
        return True, "", ""

    parsed = urlparse(url_or_path)
    if parsed.scheme.lower() not in ("http", "https"):
        return False, "INVALID_URL", "Input must be a valid HTTP(S) URL or an existing local file path."

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False, "INVALID_URL", "Invalid URL: missing hostname."

    # Exact hostname or subdomain of valid host
    is_valid = False
    for valid_h in VALID_HOSTS:
        if hostname == valid_h or hostname.endswith("." + valid_h):
            is_valid = True
            break

    if not is_valid:
        return False, "INVALID_URL", f"URL domain '{hostname}' is not supported. Supported: Instagram, TikTok, YouTube."

    return True, "", ""


def get_ytdlp_cmd() -> Optional[list]:
    """Locate yt-dlp binary or python module."""
    bin_path = shutil.which("yt-dlp")
    if not bin_path:
        for c in [os.path.expanduser("~/.local/bin/yt-dlp"), "/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp"]:
            if os.path.exists(c):
                bin_path = c
                break
    if bin_path:
        return [bin_path]

    # Check python -m yt_dlp
    try:
        res = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        pass

    return None


def classify_ytdlp_error(stderr_text: str) -> Tuple[str, str]:
    """Map yt-dlp error output to clean error_code and actionable message."""
    s = stderr_text.lower()

    if any(k in s for k in ["login", "checkpoint", "cookie", "empty media response", "rate-limit"]):
        return (
            "NEEDS_COOKIES",
            "This video requires authentication or cookies. Export cookies from your browser into ~/.config/agent-reels-viewer/cookies.txt or pass --cookies path/to/cookies.txt."
        )

    if any(k in s for k in ["private", "not available", "deleted", "unavailable"]):
        return (
            "PRIVATE_VIDEO",
            "Video is private, restricted, or has been removed by the creator."
        )

    if any(k in s for k in ["unable to extract", "parser error", "regex", "signature", "nsig"]):
        return (
            "EXTRACTOR_BROKEN",
            f"Failed to parse video metadata. Try updating yt-dlp (`yt-dlp -U`): {stderr_text.strip()[:140]}"
        )

    return (
        "DOWNLOAD_FAILED",
        f"yt-dlp failed: {stderr_text.strip()[:140]}"
    )


def fetch_metadata(url: str, cookies_path: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """Fetch video metadata without downloading video stream."""
    cmd_base = get_ytdlp_cmd()
    if not cmd_base:
        return None, "YTDLP_MISSING", "yt-dlp binary is missing. Install with: pip install yt-dlp"

    cmd = cmd_base + [
        "--dump-json",
        "--no-playlist",
        "--skip-download",
    ]

    if cookies_path and os.path.exists(cookies_path):
        cmd.extend(["--cookies", cookies_path])

    cmd.append(url)

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            code, msg = classify_ytdlp_error(res.stderr)
            return None, code, msg

        data = json.loads(res.stdout.strip())
        duration = data.get("duration") or 0
        if duration > MAX_DURATION_SECONDS:
            return None, "VIDEO_TOO_LONG", f"Video duration ({int(duration)}s) exceeds short-form limit ({MAX_DURATION_SECONDS}s)."

        return data, "", ""
    except subprocess.TimeoutExpired:
        return None, "DOWNLOAD_TIMEOUT", "Metadata extraction timed out."
    except json.JSONDecodeError:
        return None, "EXTRACTOR_BROKEN", "Failed to parse JSON metadata from yt-dlp."
    except Exception as e:
        return None, "UNKNOWN_ERROR", str(e)


def download_media(url: str, output_dir: str, cookies_path: Optional[str] = None) -> Tuple[Optional[str], str, str]:
    """Download video in MP4 container."""
    cmd_base = get_ytdlp_cmd()
    if not cmd_base:
        return None, "YTDLP_MISSING", "yt-dlp binary is missing"

    os.makedirs(output_dir, exist_ok=True)
    out_template = os.path.join(output_dir, "video.%(ext)s")

    cmd = cmd_base + [
        "--format", "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]/b",
        "--merge-output-format", "mp4",
        "--output", out_template,
        "--no-playlist",
        "--no-continue",
        "--no-mtime",
    ]

    if cookies_path and os.path.exists(cookies_path):
        cmd.extend(["--cookies", cookies_path])

    cmd.append(url)

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            code, msg = classify_ytdlp_error(res.stderr)
            return None, code, msg

        target_file = os.path.join(output_dir, "video.mp4")
        if os.path.exists(target_file):
            return target_file, "", ""

        # Fallback search for any video file created
        for fname in os.listdir(output_dir):
            if fname.startswith("video.") and fname.endswith((".mp4", ".mkv", ".webm")):
                return os.path.join(output_dir, fname), "", ""

        return None, "FILE_NOT_FOUND", "Downloaded video file could not be located."

    except subprocess.TimeoutExpired:
        return None, "DOWNLOAD_TIMEOUT", "Media download timed out."
    except Exception as e:
        return None, "UNKNOWN_ERROR", str(e)
