#!/usr/bin/env python3
"""Main CLI entrypoint for agent-reels-viewer."""

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from typing import Optional

# Ensure package root is in sys.path when invoked directly as a standalone script
_pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _pkg_root not in sys.path:
    sys.path.insert(0, _pkg_root)

from scripts.lib.audio import extract_audio, transcribe_audio
from scripts.lib.doctor import run_doctor
from scripts.lib.downloader import detect_platform, download_media, fetch_metadata, validate_url
from scripts.lib.timeline import assemble_timeline
from scripts.lib.video import extract_keyframes, extract_range_frames, get_ffmpeg_path, get_video_duration


def get_base_cache_dir() -> str:
    """Resolve base cache directory per platform specifications.
    
    Priority:
    1. AGENT_REELS_CACHE env variable
    2. Windows: %LOCALAPPDATA%/agent-reels-viewer
    3. Linux/macOS: $XDG_CACHE_HOME/agent-reels-viewer or ~/.cache/agent-reels-viewer
    """
    env_cache = os.environ.get("AGENT_REELS_CACHE")
    if env_cache:
        return os.path.abspath(env_cache)

    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local"))
        return os.path.join(local_app, "agent-reels-viewer")
    else:
        xdg_cache = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
        return os.path.join(xdg_cache, "agent-reels-viewer")


def get_session_dir(url: str, custom_output: Optional[str] = None) -> str:
    """Generate deterministic session directory from URL or return custom output directory."""
    if custom_output:
        return os.path.abspath(custom_output)

    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    session_id = f"session_{url_hash}"
    base_dir = get_base_cache_dir()
    return os.path.join(base_dir, session_id)


def auto_clean_old_sessions(cache_root: str, max_age_hours: int = 24) -> None:
    """Auto-prune cache directories older than max_age_hours."""
    if not os.path.exists(cache_root):
        return

    now = time.time()
    cutoff = now - (max_age_hours * 3600)

    try:
        for entry in os.listdir(cache_root):
            entry_path = os.path.join(cache_root, entry)
            if os.path.isdir(entry_path) and entry.startswith("session_"):
                try:
                    mtime = os.path.getmtime(entry_path)
                    if mtime < cutoff:
                        shutil.rmtree(entry_path, ignore_errors=True)
                except OSError:
                    pass
    except OSError:
        pass


def resolve_cookies(cookies_arg: Optional[str]) -> Optional[str]:
    """Find cookies.txt from argument or standard config path."""
    if cookies_arg and os.path.exists(cookies_arg):
        return os.path.abspath(cookies_arg)

    default_paths = [
        os.path.expanduser("~/.config/agent-reels-viewer/cookies.txt"),
        os.path.abspath("./cookies.txt"),
    ]
    for p in default_paths:
        if os.path.exists(p):
            return p
    return None


def cmd_inspect(args: argparse.Namespace) -> int:
    """Primary pipeline command: download, transcribe, extract keyframes, assemble timeline."""
    url = args.url.strip()

    # Preflight validation: schema and domain whitelist
    is_valid, val_err_code, val_err_msg = validate_url(url)
    if not is_valid:
        res = {"status": "error", "error_code": val_err_code or "INVALID_URL", "message": val_err_msg}
        if args.json:
            print(json.dumps(res))
        else:
            print(f"Error [INVALID_URL]: {val_err}", file=sys.stderr)
        return 1

    # Preflight check: ffmpeg is required for inspection
    if args.mode != "quick":
        if not get_ffmpeg_path():
            res = {
                "status": "error",
                "error_code": "FFMPEG_MISSING",
                "message": "ffmpeg is required for video inspection but was not found in PATH or standard system paths.",
            }
            if args.json:
                print(json.dumps(res))
            else:
                print(f"Error [FFMPEG_MISSING]: {res['message']}", file=sys.stderr)
            return 1

    cookies = resolve_cookies(args.cookies)
    output_dir = get_session_dir(url, custom_output=args.output)
    
    # Run auto-cleanup for sessions older than 24 hours
    auto_clean_old_sessions(os.path.dirname(output_dir), max_age_hours=24)
    os.makedirs(output_dir, exist_ok=True)

    # Step 1: Metadata extraction
    meta, meta_err_code, meta_err_msg = fetch_metadata(url, cookies_path=cookies)
    if meta_err_code:
        res = {"status": "error", "error_code": meta_err_code, "message": meta_err_msg}
        if args.json:
            print(json.dumps(res))
        else:
            print(f"Error [{meta_err_code}]: {meta_err_msg}", file=sys.stderr)
        return 2

    # Quick mode: return metadata immediately without downloading video
    if args.mode == "quick":
        meta_file = os.path.join(output_dir, "meta.json")
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        res = {
            "status": "success",
            "mode": "quick",
            "platform": detect_platform(url),
            "title": meta.get("title") or "Untitled",
            "author": meta.get("uploader") or "Unknown",
            "duration": meta.get("duration"),
            "meta_path": meta_file,
        }
        if args.json:
            print(json.dumps(res))
        else:
            print(f"Quick Mode: Metadata saved to {meta_file}")
            print(json.dumps(res, indent=2))
        return 0

    # Step 2: Download media
    video_path, dl_err_code, dl_err_msg = download_media(url, output_dir, cookies_path=cookies)
    if dl_err_code or not video_path:
        res = {
            "status": "error",
            "error_code": dl_err_code or "DOWNLOAD_FAILED",
            "message": dl_err_msg or "Failed to download media",
        }
        if args.json:
            print(json.dumps(res))
        else:
            print(f"Error [{res['error_code']}]: {res['message']}", file=sys.stderr)
        return 3

    # Step 3: Audio extraction & speech transcription
    speech_segments = []
    speech_status = "none"
    transcription_status = "No audio track available"

    if args.no_speech:
        speech_status = "skipped"
        has_speech = None
        transcription_status = "Speech analysis skipped by user flag (--no-speech)"
    else:
        audio_path = extract_audio(video_path, output_dir)
        duration = meta.get("duration") or get_video_duration(video_path) or 30.0
        # Dynamic timeout: max(30, 3 * duration) unless overridden by user
        w_timeout = args.whisper_timeout if args.whisper_timeout else max(30, int(3 * duration))
        has_speech, speech_segments, speech_status, transcription_status = transcribe_audio(
            audio_path,
            timeout_sec=w_timeout,
        )

    # Step 4: Keyframe extraction with explicit cadence based on speech_status
    # Visual memes / no-speech / skipped / error: dense cadence (step = 1.5s, default cap 20)
    # Spoken dialogue / timeout: sparse conversational cadence (step = 3.5s, default cap 12)
    use_dense_cadence = speech_status in ("none", "skipped", "error")
    keyframes = extract_keyframes(
        video_path,
        output_dir,
        max_frames=args.max_frames,
        has_speech=(not use_dense_cadence),
    )

    # Step 5: Assemble timeline.md
    timeline_path = assemble_timeline(
        output_dir,
        meta=meta,
        keyframes=keyframes,
        speech_segments=speech_segments,
        has_speech=has_speech,
        transcription_status=transcription_status,
        speech_status=speech_status,
    )

    # Step 6: Video retention (retained for second-pass zooming, cleaned via 'clean' command)
    if args.no_video and not os.path.exists(args.url):
        try:
            if video_path and os.path.exists(video_path):
                os.remove(video_path)
                video_path = None
        except OSError:
            pass

    session_id = os.path.basename(output_dir)
    res = {
        "status": "success",
        "session_id": session_id,
        "platform": detect_platform(url),
        "author": meta.get("uploader") or "Unknown",
        "duration": meta.get("duration"),
        "has_speech": has_speech,
        "speech_status": speech_status,
        "frames_extracted": len(keyframes),
        "output_dir": output_dir,
        "timeline_path": timeline_path,
        "video_path": video_path,
        "meta_path": os.path.join(output_dir, "meta.json"),
    }

    if args.json:
        print(json.dumps(res))
    else:
        print(f"Inspection complete for @{res['author']} ({detect_platform(url)})")
        print(f"Session directory: {output_dir}")
        print(f"Timeline file:     {timeline_path}")
        print(f"Keyframes saved:    {len(keyframes)} frames in {os.path.join(output_dir, 'frames')}")

    return 0


def cmd_frames(args: argparse.Namespace) -> int:
    """Second-pass on-demand frame zooming."""
    target = args.target.strip()
    video_path = None
    output_dir = None

    if os.path.isfile(target):
        video_path = os.path.abspath(target)
        output_dir = args.output if args.output else os.path.dirname(video_path)
    elif os.path.isdir(target):
        output_dir = os.path.abspath(target)
        for f in os.listdir(output_dir):
            if f.endswith((".mp4", ".mkv", ".webm")):
                video_path = os.path.join(output_dir, f)
                break
    else:
        cand = os.path.join(get_base_cache_dir(), target)
        if os.path.isdir(cand):
            output_dir = cand
            for f in os.listdir(output_dir):
                if f.endswith((".mp4", ".mkv", ".webm")):
                    video_path = os.path.join(output_dir, f)
                    break

    if not video_path or not os.path.exists(video_path):
        res = {
            "status": "error",
            "error_code": "VIDEO_NOT_FOUND",
            "message": f"Could not find video file for '{target}'. If it was cleaned up, rerun 'inspect' first.",
        }
        print(json.dumps(res) if args.json else f"Error [VIDEO_NOT_FOUND]: {res['message']}", file=sys.stderr)
        return 1

    frames = extract_range_frames(
        video_path,
        start_sec=args.from_sec,
        end_sec=args.to_sec,
        output_dir=output_dir,
        count=args.count,
        hires=args.hires,
    )

    res = {
        "status": "success",
        "range": f"{args.from_sec}s - {args.to_sec}s",
        "frames_extracted": len(frames),
        "frames": [f[1] for f in frames],
    }
    if args.json:
        print(json.dumps(res))
    else:
        print(f"Extracted {len(frames)} zoom frames to {output_dir}/zoom_frames")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    """Clean cached sessions older than N days."""
    cache_root = get_base_cache_dir()
    if not os.path.exists(cache_root):
        print("Nothing to clean.")
        return 0

    now = time.time()
    cutoff = now - (args.days * 86400)
    cleaned = 0

    for item in os.listdir(cache_root):
        folder = os.path.join(cache_root, item)
        if os.path.isdir(folder) and item.startswith("session_"):
            mtime = os.path.getmtime(folder)
            if mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                cleaned += 1

    print(f"Cleaned {cleaned} cache session(s) older than {args.days} day(s) from {cache_root}.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="agent-reels-viewer",
        description="Lightweight multimodal agent skill to inspect and understand Instagram Reels, TikTok, and YouTube Shorts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # inspect
    p_inspect = subparsers.add_parser("inspect", help="Inspect and analyze a video link or local file")
    p_inspect.add_argument("url", help="Instagram Reel, TikTok, or YouTube Shorts URL / local file path")
    p_inspect.add_argument("--mode", choices=["quick", "standard", "deep"], default="standard", help="Inspection depth")
    p_inspect.add_argument("--cookies", help="Path to cookies.txt for authenticated extraction")
    p_inspect.add_argument("--output", help="Custom output directory")
    p_inspect.add_argument("--max-frames", type=int, help="Override maximum frames cap")
    p_inspect.add_argument("--no-speech", action="store_true", help="Skip speech transcription pass")
    p_inspect.add_argument("--whisper-timeout", type=int, help="Override whisper timeout in seconds")
    p_inspect.add_argument("--no-video", action="store_true", help="Immediately delete original video file to save disk space")
    p_inspect.add_argument("--json", action="store_true", help="Output compact JSON for agent integration")
    p_inspect.set_defaults(func=cmd_inspect)

    # frames (second-pass zoom)
    p_frames = subparsers.add_parser("frames", help="Extract high-density or hi-res frames for a specific time window")
    p_frames.add_argument("target", help="Session folder (e.g. session_xxx), session ID, or direct path to video file")
    p_frames.add_argument("--from-sec", type=float, required=True, help="Start time in seconds")
    p_frames.add_argument("--to-sec", type=float, required=True, help="End time in seconds")
    p_frames.add_argument("--count", type=int, default=6, help="Number of frames to extract")
    p_frames.add_argument("--hires", action="store_true", help="Extract at higher 1080p resolution")
    p_frames.add_argument("--output", help="Custom output directory")
    p_frames.add_argument("--json", action="store_true", help="Output compact JSON")
    p_frames.set_defaults(func=cmd_frames)

    # doctor
    p_doctor = subparsers.add_parser("doctor", help="Check dependencies, ffmpeg, yt-dlp, and whisper status")
    p_doctor.add_argument("--download-model", action="store_true", help="Pre-download and cache Whisper speech model")
    p_doctor.set_defaults(func=lambda args: 0 if run_doctor(download_model=getattr(args, "download_model", False))["core_ready"] else 1)

    # clean
    p_clean = subparsers.add_parser("clean", help="Clean old cache sessions")
    p_clean.add_argument("--days", type=int, default=3, help="Remove sessions older than N days")
    p_clean.set_defaults(func=cmd_clean)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
