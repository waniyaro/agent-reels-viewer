"""Audio processing and speech-to-text transcription module."""

import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from .video import get_ffmpeg_path


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
) -> Tuple[bool, List[Dict[str, Any]], str]:
    """Transcribe speech with VAD filtering.
    
    Returns (has_speech, segments_list, status_message).
    segments_list items: {"start": float, "end": float, "text": str}
    """
    if not audio_path or not os.path.exists(audio_path):
        return False, [], "No audio track available."

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return False, [], "faster-whisper is not installed. Speech transcription skipped (run: pip install faster-whisper)."

    try:
        # Load model on CPU or CUDA if available
        # Auto-compute type: int8 on CPU for lightweight memory footprint
        model = WhisperModel(model_size, device="auto", compute_type="int8")

        segments, info = model.transcribe(
            audio_path,
            language=language,
            beam_size=5,
            vad_filter=True,  # Crucial: prevents hallucinations on music-only tracks
            vad_parameters=dict(min_silence_duration_ms=500),
        )

        detected_lang = info.language if info else "unknown"
        speech_prob = info.language_probability if info else 0.0

        results = []
        for s in segments:
            txt = s.text.strip()
            if txt:
                results.append({
                    "start": round(s.start, 2),
                    "end": round(s.end, 2),
                    "text": txt,
                })

        if not results:
            return False, [], "No spoken speech detected (music or background sound only)."

        return True, results, f"Detected language: {detected_lang} (confidence: {speech_prob:.2f})"

    except Exception as e:
        return False, [], f"Transcription error: {str(e)}"
