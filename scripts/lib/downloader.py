"""Media downloader wrapper for yt-dlp with platform detection and error handling."""

import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, Optional, Tuple

MAX_DURATION_SECONDS = 360  # 6 minutes max (Shorts / Reels / TikTok)

SUPPORTED_DOMAINS = [
    "instagram.com",
    "instagr.am",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
]


def detect_platform(url_or_path: str) -> str:
    """Identify the platform or detect if input is a local file."""
    if os.path.exists(url_or_path):
        return "local_file"
    
    url_lower = url_or_path.lower()
    if "instagram.com" in url_lower or "instagr.am" in url_lower:
        return "instagram"
    elif "tiktok.com" in url_lower:
        return "tiktok"
    elif "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "youtube"
    return "generic_web"


def validate_url(url_or_path: str) -> Tuple[bool, str]:
    """Validate whether input is a valid supported URL or local file path."""
    if os.path.exists(url_or_path):
        return True, ""
    
    # Must look like an http/https URL
    if not re.match(r"^https?://", url_or_path, re.IGNORECASE):
        return False, "Input must be a valid HTTP(S) URL or existing local file path."
    
    is_supported = any(domain in url_or_path.lower() for domain in SUPPORTED_DOMAINS)
    if not is_supported:
        return False, f"URL domain is not supported. Supported platforms: {', '.join(SUPPORTED_DOMAINS)}"
    
    return True, ""


def get_ytdlp_cmd() -> Optional[list]:
    """Locate yt-dlp binary or python module."""
    bin_path = shutil.which("yt-dlp")
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
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Extract metadata using yt-dlp without downloading media.
    
    Returns (meta_dict, error_code).
    """
    if os.path.exists(url_or_path):
        return {
            "id": "local",
            "title": os.path.basename(url_or_path),
            "uploader": "local_user",
            "duration": None,
            "description": "Local video file",
            "webpage_url": url_or_path,
        }, None

    ytdlp_base = get_ytdlp_cmd()
    if not ytdlp_base:
        return None, "YTDLP_MISSING"

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
            if "login required" in stderr or "checkpoint" in stderr or "rate-limit" in stderr:
                return None, "NEEDS_COOKIES"
            elif "not found" in stderr or "private" in stderr or "removed" in stderr:
                return None, "PRIVATE_OR_REMOVED"
            return None, f"FETCH_META_FAILED: {proc.stderr.strip()[:200]}"

        meta = json.loads(proc.stdout)
        
        # Check duration
        duration = meta.get("duration")
        if duration and duration > MAX_DURATION_SECONDS:
            return None, f"VIDEO_TOO_LONG: Video is {int(duration)}s (limit is {MAX_DURATION_SECONDS}s)"

        return meta, None

    except subprocess.TimeoutExpired:
        return None, "TIMEOUT_METADATA"
    except json.JSONDecodeError:
        return None, "INVALID_METADATA_JSON"
    except Exception as e:
        return None, f"EXCEPTION: {str(e)}"


def download_media(
    url_or_path: str,
    output_dir: str,
    cookies_path: Optional[str] = None,
    timeout: int = 60,
) -> Tuple[Optional[str], Optional[str]]:
    """Download video in optimized size (480p-720p) to output_dir.
    
    Returns (video_filepath, error_code).
    """
    os.makedirs(output_dir, exist_ok=True)

    if os.path.exists(url_or_path):
        # Local file copy or symlink
        target_path = os.path.join(output_dir, "input_video.mp4")
        if not os.path.exists(target_path):
            shutil.copy2(url_or_path, target_path)
        return target_path, None

    ytdlp_base = get_ytdlp_cmd()
    if not ytdlp_base:
        return None, "YTDLP_MISSING"

    target_template = os.path.join(output_dir, "video.%(ext)s")

    cmd = ytdlp_base + [
        "--no-warnings",
        "--no-playlist",
        # Prefer moderate quality for fast processing & small token footprint
        "-f", "worst[height>=480]/best[height<=720]/best",
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
            if "login required" in stderr or "checkpoint" in stderr:
                return None, "NEEDS_COOKIES"
            return None, f"DOWNLOAD_FAILED: {proc.stderr.strip()[:200]}"

        # Find the created video file
        for f in os.listdir(output_dir):
            if f.startswith("video.") and f.endswith((".mp4", ".mkv", ".webm")):
                return os.path.join(output_dir, f), None

        return None, "FILE_NOT_FOUND_AFTER_DOWNLOAD"

    except subprocess.TimeoutExpired:
        return None, "TIMEOUT_DOWNLOAD"
    except Exception as e:
        return None, f"EXCEPTION: {str(e)}"
