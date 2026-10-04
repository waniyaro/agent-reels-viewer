"""Standalone transcription worker process executed with hard timeout."""

import json
import os
import shutil
import subprocess
import sys


def get_ffmpeg_path():
    path = shutil.which("ffmpeg")
    if not path:
        for c in [
            os.path.expanduser("~/.local/bin/ffmpeg"),
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/usr/bin/ffmpeg",
        ]:
            if os.path.exists(c):
                path = c
                break
    return path


def main():
    if len(sys.argv) < 5:
        sys.exit(1)

    audio_path = sys.argv[1]
    model_size = sys.argv[2]
    language = None if sys.argv[3] == "None" else sys.argv[3]
    output_json = sys.argv[4]

    try:
        from faster_whisper import WhisperModel
        import numpy as np
    except ImportError as e:
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump({"error": f"ImportError: {e}"}, f)
        sys.exit(2)

    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump({"error": "ffmpeg missing for audio decoding"}, f)
        sys.exit(3)

    decode_cmd = [
        ffmpeg,
        "-y",
        "-i", audio_path,
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-ac", "1",
        "-ar", "16000",
        "-",
    ]
    try:
        proc = subprocess.run(decode_cmd, capture_output=True, timeout=30, check=True)
        audio_np = np.frombuffer(proc.stdout, dtype=np.int16).flatten().astype(np.float32) / 32768.0
    except Exception as e:
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump({"error": f"FFmpeg decode error: {e}"}, f)
        sys.exit(4)

    try:
        model = WhisperModel(model_size, device="auto", compute_type="int8")
        segments_gen, info = model.transcribe(
            audio_np,
            language=language,
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )

        results = []
        for s in segments_gen:
            txt = s.text.strip()
            if txt:
                results.append({
                    "start": round(s.start, 2),
                    "end": round(s.end, 2),
                    "text": txt,
                })

        detected_lang = info.language if info else "unknown"
        speech_prob = info.language_probability if info else 0.0

        with open(output_json, "w", encoding="utf-8") as f:
            json.dump({
                "segments": results,
                "language": detected_lang,
                "confidence": speech_prob,
            }, f, ensure_ascii=False)

        sys.exit(0)

    except Exception as e:
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump({"error": str(e)}, f)
        sys.exit(5)


if __name__ == "__main__":
    main()
