"""Media downloader wrapper for yt-dlp with platform detection and error handling."""

import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

MAX_DURATION_SECONDS = 360  # 6 minutes max (Shorts / Reels / TikTok)

VALID_HOSTS = {
    "instagram.com", "www.instagram.com", "instagr.am",
    "tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com",
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
}


def detect_platform(url_or_path: str) -> str:
    """Identify the platform or detect if input is a local file."""
    if os.path.exists(url_or_path):
        return "local_file"
    
    parsed = urlparse(url_or_path)
    host = (parsed.hostname or "").lower()
    
    if "instagram.com" in host or "instagr.am" in host:
        return "instagram"
    elif "tiktok.com" in host:
        return "tiktok"
    elif "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    return "generic_web"


def validate_url(url_or_path: str) -> Tuple[bool, str, str]:
    """Validate URL with strict domain checking against SSRF and userinfo spoofing.
    
    Returns: (is_valid, error_code, error_message)
    """
    if os.path.exists(url_or_path):
        return True, "", ""
    
    parsed = urlparse(url_or_path)
    if parsed.scheme.lower() not in ("http", "https"):
        return False, "INVALID_URL", "Input must be a valid HTTP(S) URL or existing local file path."
    
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


def fetch_metadata(
    url_or_path: str,
    cookies_path: Optional[str] = None,
    timeout: int = 25,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """Extract metadata using yt-dlp without downloading media.
    
    Returns (meta_dict, error_code, error_message).
    """
    if os.path.exists(url_or_path):
        return {
            "id": "local",
            "title": os.path.basename(url_or_path),
            "uploader": "local_user",
            "duration": None,
            "description": "Local video file",
            "webpage_url": url_or_path,
        }, None, None

    ytdlp_base = get_ytdlp_cmd()
    if not ytdlp_base:
        return None, "YTDLP_MISSING", "yt-dlp is not installed. Install via: pip install -U yt-dlp"

    cmd = ytdlp_base + [
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--no-playlist",
    ]

    if cookies_path and os.path.exists(cookies_path):
        cmd.extend(["--cookies", cookies_path])

    cmd.append(url_or_path)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.lower()
            if any(k in stderr for k in ["login required", "checkpoint", "rate-limit", "empty media response", "without being logged-in"]):
                return None, "NEEDS_COOKIES", "Instagram/TikTok login required. Use --cookies path/to/cookies.txt"
            elif any(k in stderr for k in ["not found", "private", "removed", "not available"]):
                return None, "PRIVATE_VIDEO", "Video is private, age-restricted, not available, or removed by creator"
            elif "unsupported url" in stderr:
                return None, "INVALID_URL", "The provided URL is not recognized by extractor"
            return None, "EXTRACTOR_BROKEN", f"Failed to parse video metadata. Try updating yt-dlp (`yt-dlp -U`): {proc.stderr.strip()[:180]}"

        meta = json.loads(proc.stdout)
        
        # Check duration
        duration = meta.get("duration")
        if duration and duration > MAX_DURATION_SECONDS:
            return None, "VIDEO_TOO_LONG", f"Video duration ({int(duration)}s) exceeds short-form limit ({MAX_DURATION_SECONDS}s)"

        return meta, None, None

    except subprocess.TimeoutExpired:
        return None, "TIMEOUT_METADATA", f"Metadata fetch timed out after {timeout}s"
    except json.JSONDecodeError:
        return None, "INVALID_METADATA_JSON", "yt-dlp returned malformed JSON"
    except Exception as e:
        return None, "EXCEPTION", f"Unexpected error: {str(e)}"


def download_media(
    url_or_path: str,
    output_dir: str,
    cookies_path: Optional[str] = None,
    timeout: int = 60,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Download video in optimized size (480p-720p) to output_dir.
    
    Returns (video_filepath, error_code, error_message).
    """
    os.makedirs(output_dir, exist_ok=True)

    if os.path.exists(url_or_path):
        target_path = os.path.join(output_dir, "input_video.mp4")
        if not os.path.exists(target_path):
            shutil.copy2(url_or_path, target_path)
        return target_path, None, None

    ytdlp_base = get_ytdlp_cmd()
    if not ytdlp_base:
        return None, "YTDLP_MISSING", "yt-dlp is not installed. Install via: pip install -U yt-dlp"

    target_template = os.path.join(output_dir, "video.%(ext)s")

    cmd = ytdlp_base + [
        "--no-warnings",
        "--no-playlist",
        "-f", "b[height<=720]/bv*[height<=720]+ba/b/best",
        "--recode-video", "mp4",
        "-o", target_template,
    ]

    if cookies_path and os.path.exists(cookies_path):
        cmd.extend(["--cookies", cookies_path])

    cmd.append(url_or_path)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.lower()
            if any(k in stderr for k in ["login required", "checkpoint", "empty media response", "without being logged-in"]):
                return None, "NEEDS_COOKIES", "Authentication required to download video. Provide --cookies path/to/cookies.txt"
            elif any(k in stderr for k in ["private", "not found", "removed"]):
                return None, "PRIVATE_VIDEO", "Video is private or deleted"
            return None, "EXTRACTOR_BROKEN", f"Download error. Check yt-dlp version (`yt-dlp -U`): {proc.stderr.strip()[:180]}"

        # Find the created video file
        for f in os.listdir(output_dir):
            if f.endswith((".mp4", ".mkv", ".webm")):
                return os.path.join(output_dir, f), None, None

        return None, "FILE_NOT_FOUND_AFTER_DOWNLOAD", "yt-dlp reported success but no output video file found"

    except subprocess.TimeoutExpired:
        return None, "TIMEOUT_DOWNLOAD", f"Video download timed out after {timeout}s"
    except Exception as e:
        return None, "EXCEPTION", f"Unexpected error: {str(e)}"
