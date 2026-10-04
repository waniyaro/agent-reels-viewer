"""Audio processing and speech-to-text transcription module with process isolation."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from agent_reels_viewer.video import get_ffmpeg_path, has_audio_stream


def ensure_model_downloaded(model_size: str) -> Tuple[bool, str]:
    """Ensure faster-whisper model weights are present in cache before starting transcription timer."""
    try:
        from faster_whisper import download_model
        print(f"Ensuring Whisper '{model_size}' model is cached...", file=sys.stderr)
        download_model(model_size)
        return True, ""
    except ImportError:
        return False, "faster-whisper is not installed. Install with: pip install '.[speech]'"
    except Exception as e:
        return False, f"Failed to download Whisper model '{model_size}': {str(e)}"


def extract_audio(video_path: str, output_dir: str) -> Tuple[Optional[str], str, str]:
    """Extract audio track from video to MP3 format.
    
    Returns: (audio_path, error_code, error_message)
    error_code:
      "" on success
      "NO_AUDIO_STREAM" if video has no audio track
      "AUDIO_EXTRACTION_FAILED" if extraction failed
    """
    if not has_audio_stream(video_path):
        return None, "NO_AUDIO_STREAM", "Video container contains no audio stream (silent video)."

    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return None, "FFMPEG_MISSING", "ffmpeg is required for audio extraction."

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
        if res.returncode == 0 and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
            return audio_path, "", ""
        return None, "AUDIO_EXTRACTION_FAILED", f"Audio extraction failed with exit code {res.returncode}."
    except subprocess.TimeoutExpired:
        return None, "AUDIO_EXTRACTION_FAILED", "Audio extraction timed out after 30s."
    except Exception as e:
        return None, "AUDIO_EXTRACTION_FAILED", f"Audio extraction error: {str(e)}"


def transcribe_audio(
    audio_path: str,
    model_size: str = "base",
    language: Optional[str] = None,
    timeout_sec: int = 45,
    worker_script: Optional[str] = None,
) -> Tuple[Optional[bool], List[Dict[str, Any]], str, str]:
    """Transcribe speech in an isolated subprocess with hard timeout.
    
    Returns: (has_speech, segments_list, speech_status, status_message)
    has_speech: True if speech detected, False if no speech, None if skipped/timeout/error
    speech_status is one of: 'ok', 'none', 'timeout', 'error'
    """
    if not audio_path or not os.path.exists(audio_path):
        return False, [], "none", "No audio track available."

    if not worker_script:
        # Pre-flight: verify model weights are downloaded outside the timeout window
        ok, err_msg = ensure_model_downloaded(model_size)
        if not ok:
            return None, [], "error", err_msg
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
            err_detail = proc.stderr[:160].strip() if proc.stderr else f"exit code {proc.returncode}"
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
        return (
            None,
            [],
            "timeout",
            f"WHISPER_TIMEOUT: Speech analysis timed out after {timeout_sec}s. Try using a smaller Whisper model (--model tiny) or extending --whisper-timeout."
        )
    except Exception as e:
        return None, [], "error", f"Transcription error: {str(e)}"
    finally:
        if os.path.exists(tmp_json):
            try:
                os.remove(tmp_json)
            except OSError:
                pass
