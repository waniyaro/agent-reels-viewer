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

from lib.audio import extract_audio, transcribe_audio
from lib.doctor import run_doctor
from lib.downloader import (
    detect_platform,
    download_media,
    fetch_metadata,
    validate_url,
)
from lib.timeline import assemble_timeline
from lib.video import extract_keyframes, extract_range_frames, get_video_duration


def get_default_output_dir(url_or_path: str) -> str:
    """Generate a stable, unique session folder in ./output."""
    url_hash = hashlib.md5(url_or_path.encode("utf-8")).hexdigest()[:10]
    return os.path.abspath(os.path.join("output", f"session_{url_hash}"))


def resolve_cookies(cli_cookies: Optional[str]) -> Optional[str]:
    """Find valid cookies file."""
    if cli_cookies and os.path.exists(cli_cookies):
        return os.path.abspath(cli_cookies)
    
    defaults = [
        os.path.expanduser("~/.config/agent-reels-viewer/cookies.txt"),
        os.path.abspath("cookies.txt"),
    ]
    for p in defaults:
        if os.path.exists(p):
            return p
    return None


def cmd_inspect(args: argparse.Namespace) -> int:
    """Execute video inspection pipeline."""
    url = args.url.strip()
    valid, err_msg = validate_url(url)
    if not valid:
        res = {"status": "error", "error_code": "INVALID_URL", "message": err_msg}
        if args.json:
            print(json.dumps(res))
        else:
            print(f"❌ Error: {err_msg}", file=sys.stderr)
        return 1

    cookies = resolve_cookies(args.cookies)
    output_dir = os.path.abspath(args.output) if args.output else get_default_output_dir(url)
    os.makedirs(output_dir, exist_ok=True)

    # Step 1: Metadata extraction
    meta, meta_err = fetch_metadata(url, cookies_path=cookies)
    if meta_err:
        res = {"status": "error", "error_code": meta_err, "message": f"Failed to fetch metadata: {meta_err}"}
        if args.json:
            print(json.dumps(res))
        else:
            print(f"❌ Metadata Error: {meta_err}", file=sys.stderr)
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
            print(f"✅ [Quick Mode] Metadata saved to {meta_file}")
            print(json.dumps(res, indent=2))
        return 0

    # Step 2: Download media
    video_path, dl_err = download_media(url, output_dir, cookies_path=cookies)
    if dl_err or not video_path:
        res = {"status": "error", "error_code": dl_err or "DOWNLOAD_FAILED", "message": f"Download failed: {dl_err}"}
        if args.json:
            print(json.dumps(res))
        else:
            print(f"❌ Download Error: {dl_err}", file=sys.stderr)
        return 3

    # Step 3: Audio extraction & transcription
    audio_path = extract_audio(video_path, output_dir)
    has_speech, speech_segments, transcription_status = transcribe_audio(audio_path)

    # Step 4: Keyframe extraction
    max_frames = 16 if args.mode == "deep" else 10
    keyframes = extract_keyframes(
        video_path,
        output_dir,
        max_frames=max_frames,
        has_speech=has_speech,
    )

    # Step 5: Assemble timeline.md
    timeline_path = assemble_timeline(
        output_dir,
        meta=meta,
        keyframes=keyframes,
        speech_segments=speech_segments,
        has_speech=has_speech,
        transcription_status=transcription_status,
    )

    # Step 6: Cleanup raw video to preserve user disk space (unless --keep-video requested)
    if not args.keep_video and not os.path.exists(args.url):
        try:
            if video_path and os.path.exists(video_path):
                os.remove(video_path)
        except OSError:
            pass

    res = {
        "status": "success",
        "platform": detect_platform(url),
        "author": meta.get("uploader") or "Unknown",
        "duration": meta.get("duration"),
        "has_speech": has_speech,
        "frames_extracted": len(keyframes),
        "output_dir": output_dir,
        "timeline_path": timeline_path,
        "meta_path": os.path.join(output_dir, "meta.json"),
    }

    if args.json:
        print(json.dumps(res))
    else:
        print(f"✅ Inspection complete for @{res['author']} ({detect_platform(url)})")
        print(f"📁 Artifacts directory: {output_dir}")
        print(f"📄 Timeline file:      {timeline_path}")
        print(f"🖼️  Keyframes saved:     {len(keyframes)} frames in {os.path.join(output_dir, 'frames')}")

    return 0


def cmd_frames(args: argparse.Namespace) -> int:
    """Second-pass on-demand frame zooming."""
    video_path = args.video_path
    if not os.path.exists(video_path):
        res = {"status": "error", "error_code": "FILE_NOT_FOUND", "message": f"Video file {video_path} not found"}
        print(json.dumps(res) if args.json else f"❌ {res['message']}", file=sys.stderr)
        return 1

    output_dir = args.output if args.output else os.path.dirname(video_path)
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
        print(f"✅ Extracted {len(frames)} zoom frames to {output_dir}/zoom_frames")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    """Clean cached sessions older than N days."""
    out_root = os.path.abspath("output")
    if not os.path.exists(out_root):
        print("Nothing to clean.")
        return 0

    now = time.time()
    cutoff = now - (args.days * 86400)
    cleaned = 0

    for item in os.listdir(out_root):
        folder = os.path.join(out_root, item)
        if os.path.isdir(folder) and item.startswith("session_"):
            mtime = os.path.getmtime(folder)
            if mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                cleaned += 1

    print(f"🧹 Cleaned {cleaned} cache session(s) older than {args.days} day(s).")
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
    p_inspect.add_argument("--keep-video", action="store_true", help="Retain original downloaded MP4")
    p_inspect.add_argument("--json", action="store_true", help="Output compact JSON for agent integration")
    p_inspect.set_defaults(func=cmd_inspect)

    # frames (second-pass zoom)
    p_frames = subparsers.add_parser("frames", help="Extract high-density or hi-res frames for a specific time window")
    p_frames.add_argument("video_path", help="Path to downloaded video file")
    p_frames.add_argument("--from-sec", type=float, required=True, help="Start time in seconds")
    p_frames.add_argument("--to-sec", type=float, required=True, help="End time in seconds")
    p_frames.add_argument("--count", type=int, default=6, help="Number of frames to extract")
    p_frames.add_argument("--hires", action="store_true", help="Extract at higher 1080p resolution")
    p_frames.add_argument("--output", help="Custom output directory")
    p_frames.add_argument("--json", action="store_true", help="Output compact JSON")
    p_frames.set_defaults(func=cmd_frames)

    # doctor
    p_doctor = subparsers.add_parser("doctor", help="Check dependencies, ffmpeg, yt-dlp, and whisper status")
    p_doctor.set_defaults(func=lambda args: 0 if run_doctor()["core_ready"] else 1)

    # clean
    p_clean = subparsers.add_parser("clean", help="Clean old cache sessions")
    p_clean.add_argument("--days", type=int, default=3, help="Remove sessions older than N days")
    p_clean.set_defaults(func=cmd_clean)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
