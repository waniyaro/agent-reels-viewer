"""Audio processing and speech-to-text transcription module with process isolation."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from scripts.lib.video import get_ffmpeg_path


def extract_audio(video_path: str, output_dir: str) -> Optional[str]:
    """Extract audio track from video to MP3 format."""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return None

    audio_path = os.path.join(output_dir, "audio.mp3")
    cmd = [
        ffmpeg,
        "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "4",
        "-ar", "16000",
        audio_path,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=30)
        if res.returncode == 0 and os.path.exists(audio_path):
            return audio_path
    except Exception:
        pass
    return None


def transcribe_audio(
    audio_path: str,
    model_size: str = "base",
    language: Optional[str] = None,
    timeout_sec: int = 45,
) -> Tuple[Optional[bool], List[Dict[str, Any]], str, str]:
    """Transcribe speech in an isolated subprocess with hard timeout.
    
    Returns: (has_speech, segments_list, speech_status, status_message)
    has_speech: True if speech detected, False if no speech, None if skipped/timeout/error
    speech_status is one of: 'ok', 'none', 'timeout', 'error'
    """
    if not audio_path or not os.path.exists(audio_path):
        return False, [], "none", "No audio track available."

    worker_script = os.path.join(os.path.dirname(__file__), "transcribe_worker.py")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp_out:
        tmp_json = tmp_out.name

    cmd = [
        sys.executable,
        worker_script,
        audio_path,
        model_size,
        str(language) if language else "None",
        tmp_json,
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        if not os.path.exists(tmp_json) or os.path.getsize(tmp_json) == 0:
            err_detail = proc.stderr[:120].strip() if proc.stderr else f"exit code {proc.returncode}"
            return None, [], "error", f"Worker terminated with code {proc.returncode}: {err_detail}"

        with open(tmp_json, "r", encoding="utf-8") as f:
            data = json.load(f)

        if "error" in data:
            return None, [], "error", data["error"]

        segments = data.get("segments", [])
        if not segments:
            return False, [], "none", "No spoken speech detected (music or background sound only)."

        lang = data.get("language", "unknown")
        conf = data.get("confidence", 0.0)
        return True, segments, "ok", f"Detected language: {lang} (confidence: {conf:.2f})"

    except subprocess.TimeoutExpired:
        return None, [], "timeout", f"WHISPER_TIMEOUT: Speech analysis timed out after {timeout_sec}s."
    except Exception as e:
        return None, [], "error", f"Transcription error: {str(e)}"
    finally:
        if os.path.exists(tmp_json):
            try:
                os.remove(tmp_json)
            except OSError:
                pass
