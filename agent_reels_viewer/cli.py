#!/usr/bin/env python3
"""Main CLI implementation for agent-reels-viewer."""

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.parse
from typing import Optional

from agent_reels_viewer.audio import extract_audio, transcribe_audio
from agent_reels_viewer.doctor import run_doctor
from agent_reels_viewer.downloader import detect_platform, download_media, fetch_metadata, validate_url
from agent_reels_viewer.timeline import assemble_timeline
from agent_reels_viewer.video import (
    PIL_AVAILABLE,
    extract_keyframes,
    extract_range_frames,
    get_ffmpeg_path,
    get_video_duration,
)


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


def get_session_dir(target: str, custom_output: Optional[str] = None) -> str:
    """Generate deterministic session directory from URL or file path, or return custom output directory."""
    if custom_output:
        return os.path.abspath(custom_output)

    url_hash = hashlib.sha256(target.encode("utf-8")).hexdigest()[:10]
    session_id = f"session_{url_hash}"
    base_dir = get_base_cache_dir()
    return os.path.join(base_dir, session_id)


def get_session_latest_mtime(session_dir: str) -> float:
    """Get the latest mtime among all files inside session_dir (fallback to dir mtime if empty)."""
    file_mtimes = []
    try:
        for root, _, files in os.walk(session_dir):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    file_mtimes.append(os.path.getmtime(fp))
                except OSError:
                    pass
    except OSError:
        pass

    if file_mtimes:
        return max(file_mtimes)

    try:
        return os.path.getmtime(session_dir)
    except OSError:
        return 0.0


def get_default_ttl_hours() -> float:
    """Read AGENT_REELS_TTL_HOURS from environment, defaulting to 24."""
    env_val = os.environ.get("AGENT_REELS_TTL_HOURS")
    if env_val:
        try:
            return float(env_val)
        except ValueError:
            pass
    return 24.0


def auto_clean_old_sessions(cache_root: str, max_age_hours: Optional[float] = None) -> None:
    """Auto-prune cache directories older than max_age_hours strictly inside base cache."""
    base_cache = get_base_cache_dir()
    if os.path.abspath(cache_root) != os.path.abspath(base_cache):
        return

    if not os.path.exists(cache_root):
        return

    if max_age_hours is None:
        max_age_hours = get_default_ttl_hours()

    now = time.time()
    cutoff = now - (max_age_hours * 3600)

    try:
        for entry in os.listdir(cache_root):
            entry_path = os.path.join(cache_root, entry)
            if os.path.isdir(entry_path) and entry.startswith("session_"):
                mtime = get_session_latest_mtime(entry_path)
                if mtime < cutoff:
                    shutil.rmtree(entry_path, ignore_errors=True)
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
    """Primary pipeline command: inspect local video file or download social media URL."""
    target = args.url.strip()
    is_local_file = os.path.isfile(target)

    # 1. Validation
    if not is_local_file:
        parsed_target = urllib.parse.urlparse(target)
        if not parsed_target.scheme or parsed_target.scheme not in ("http", "https"):
            res = {
                "status": "error",
                "error_code": "LOCAL_FILE_NOT_FOUND",
                "message": f"Local video file not found at: {target}",
            }
            if args.json:
                print(json.dumps(res))
            else:
                print(f"Error [LOCAL_FILE_NOT_FOUND]: {res['message']}", file=sys.stderr)
            return 1

        is_valid, val_err_code, val_err_msg = validate_url(target)
        if not is_valid:
            res = {"status": "error", "error_code": val_err_code or "INVALID_URL", "message": val_err_msg}
            if args.json:
                print(json.dumps(res))
            else:
                print(f"Error [{res['error_code']}]: {res['message']}", file=sys.stderr)
            return 1

    # 2. Preflight check: ffmpeg is required for video inspection
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
    output_dir = get_session_dir(target, custom_output=args.output)

    # Safe auto-cleanup strictly on the base cache root
    auto_clean_old_sessions(get_base_cache_dir(), max_age_hours=24)
    os.makedirs(output_dir, exist_ok=True)

    # 3. Handle Local File vs Remote URL
    if is_local_file:
        video_path = os.path.abspath(target)
        duration = get_video_duration(video_path) or 30.0
        file_hash = hashlib.sha256(video_path.encode("utf-8")).hexdigest()[:10]
        meta = {
            "id": file_hash,
            "title": os.path.basename(video_path),
            "uploader": "local_user",
            "webpage_url": video_path,
            "source_path": video_path,
            "duration": duration,
            "extractor_key": "LocalFile",
        }
        platform = "local"
    else:
        platform = detect_platform(target)
        # Remote: Fetch metadata
        meta, meta_err_code, meta_err_msg = fetch_metadata(target, cookies_path=cookies)
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
                "platform": platform,
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

        # Remote: Download media
        video_path, dl_err_code, dl_err_msg = download_media(target, output_dir, cookies_path=cookies)
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

    # 4. Audio extraction & speech transcription
    speech_segments = []
    speech_status = "none"
    transcription_status = "No audio track available"

    if args.no_speech:
        speech_status = "skipped"
        has_speech = None
        transcription_status = "Speech analysis skipped by user flag (--no-speech)"
    else:
        audio_path, audio_err_code, audio_err_msg = extract_audio(video_path, output_dir)
        try:
            if audio_err_code == "NO_AUDIO_STREAM":
                has_speech = False
                speech_status = "none"
                transcription_status = "Video container has no audio track (silent video)."
            elif audio_err_code == "AUDIO_EXTRACTION_FAILED":
                has_speech = None
                speech_status = "error"
                transcription_status = f"Audio extraction failed: {audio_err_msg}"
            elif audio_path:
                duration = meta.get("duration") or get_video_duration(video_path) or 30.0
                w_timeout = args.whisper_timeout if args.whisper_timeout else max(30, int(3 * duration))
                has_speech, speech_segments, speech_status, transcription_status = transcribe_audio(
                    audio_path,
                    model_size=getattr(args, "model", "base"),
                    timeout_sec=w_timeout,
                )
            else:
                has_speech = None
                speech_status = "error"
                transcription_status = "Audio extraction unavailable."
        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass

    # 5. Keyframe extraction with explicit cadence based on speech_status
    use_dense_cadence = speech_status in ("none", "skipped", "error")
    keyframes, ts_type, kf_err_code, kf_err_msg, kf_stats = extract_keyframes(
        video_path,
        output_dir,
        max_frames=args.max_frames,
        has_speech=(not use_dense_cadence),
        mode=args.mode,
        return_stats=True,
    )

    if kf_err_code:
        res = {
            "status": "error",
            "error_code": kf_err_code,
            "message": kf_err_msg,
        }
        if args.json:
            print(json.dumps(res))
        else:
            print(f"Error [{res['error_code']}]: {res['message']}", file=sys.stderr)
        return 4

    # 6. Assemble timeline.md
    timeline_path = assemble_timeline(
        output_dir,
        meta=meta,
        keyframes=keyframes,
        speech_segments=speech_segments,
        has_speech=has_speech,
        transcription_status=transcription_status,
        speech_status=speech_status,
        timestamps_type=ts_type,
    )

    # 7. Video retention (for remote downloads only; local files are never deleted)
    if not is_local_file and args.no_video:
        try:
            if video_path and os.path.exists(video_path):
                os.remove(video_path)
                video_path = None
        except OSError:
            pass

    # Estimate vision tokens according to Anthropic formula (width * height / 750)
    estimated_image_tokens = 0
    if PIL_AVAILABLE:
        try:
            from PIL import Image
            for _, fpath in keyframes:
                try:
                    with Image.open(fpath) as img:
                        w, h = img.size
                        estimated_image_tokens += int(round((w * h) / 750.0))
                except Exception:
                    estimated_image_tokens += 786
        except Exception:
            estimated_image_tokens = len(keyframes) * 786
    else:
        estimated_image_tokens = len(keyframes) * 786

    meta["keyframe_stats"] = kf_stats
    meta["estimated_image_tokens"] = estimated_image_tokens
    meta["frames_total"] = len(keyframes)
    try:
        with open(os.path.join(output_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

    if getattr(args, "debug_frames", False):
        print(f"\n[DEBUG_FRAMES] Video: {video_path}", file=sys.stderr)
        print(f"[DEBUG_FRAMES] Mode: {args.mode} | has_speech: {not use_dense_cadence}", file=sys.stderr)
        print(f"[DEBUG_FRAMES] Raw candidates: {kf_stats.get('raw_candidates')}", file=sys.stderr)
        print(f"[DEBUG_FRAMES] Kept after dedup: {kf_stats.get('kept_after_dedup')}", file=sys.stderr)
        print(f"[DEBUG_FRAMES] Dropped by dedup: {kf_stats.get('dropped_by_dedup')}", file=sys.stderr)
        print(f"[DEBUG_FRAMES] Dropped by cap: {kf_stats.get('dropped_by_cap')}", file=sys.stderr)
        print(f"[DEBUG_FRAMES] Final frames: {kf_stats.get('final_frames')}", file=sys.stderr)
        print(f"[DEBUG_FRAMES] Estimated image tokens: {estimated_image_tokens}", file=sys.stderr)
        for pair in kf_stats.get("pairwise_diffs", []):
            print(f"  [{pair['from_sec']:05.2f}s -> {pair['to_sec']:05.2f}s] diff={pair['diff']:.4f} thr={pair['threshold']:.3f} -> {pair['decision'].upper()}", file=sys.stderr)

    session_id = os.path.basename(output_dir)
    res = {
        "status": "success",
        "session_id": session_id,
        "platform": platform,
        "author": meta.get("uploader") or "Unknown",
        "duration": meta.get("duration"),
        "has_speech": has_speech,
        "speech_status": speech_status,
        "timestamps": ts_type,
        "frames_extracted": len(keyframes),
        "frames_total": len(keyframes),
        "estimated_image_tokens": estimated_image_tokens,
        "output_dir": output_dir,
        "timeline_path": timeline_path,
        "video_path": video_path,
        "meta_path": os.path.join(output_dir, "meta.json"),
    }

    if args.json:
        print(json.dumps(res))
    else:
        print(f"Inspection complete for @{res['author']} ({platform})")
        print(f"Session directory: {output_dir}")
        print(f"Timeline file:     {timeline_path}")
        print(f"Keyframes saved:    {len(keyframes)} frames in {os.path.join(output_dir, 'frames')} ({ts_type} timestamps)")

    return 0


def cmd_frames(args: argparse.Namespace) -> int:
    """Second-pass on-demand frame zooming."""
    if args.from_sec < 0 or args.from_sec >= args.to_sec:
        res = {
            "status": "error",
            "error_code": "INVALID_RANGE",
            "message": f"Invalid time range: --from-sec ({args.from_sec}) must be non-negative and strictly less than --to-sec ({args.to_sec}).",
        }
        if args.json:
            print(json.dumps(res))
        else:
            print(f"Error [INVALID_RANGE]: {res['message']}", file=sys.stderr)
        return 1

    target = args.target.strip()
    video_path = None
    output_dir = None

    if os.path.isfile(target):
        video_path = os.path.abspath(target)
        output_dir = args.output if args.output else get_session_dir(video_path)
        os.makedirs(output_dir, exist_ok=True)
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

    # If video file is not stored directly inside the session folder (e.g. for local video files),
    # check source_path recorded in meta.json
    if output_dir and (not video_path or not os.path.exists(video_path)):
        meta_file = os.path.join(output_dir, "meta.json")
        if os.path.isfile(meta_file):
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    meta_data = json.load(f)
                src = meta_data.get("source_path")
                if src and os.path.isfile(src):
                    video_path = src
            except Exception:
                pass

    if not video_path or not os.path.exists(video_path):
        res = {
            "status": "error",
            "error_code": "VIDEO_NOT_FOUND",
            "message": f"Could not find video file for '{target}'. If it was cleaned up, rerun 'inspect' first.",
        }
        print(json.dumps(res) if args.json else f"Error [VIDEO_NOT_FOUND]: {res['message']}", file=sys.stderr)
        return 1

    total_dur = get_video_duration(video_path)
    if total_dur and args.from_sec >= total_dur:
        res = {
            "status": "error",
            "error_code": "INVALID_RANGE",
            "message": f"Invalid time range: --from-sec ({args.from_sec}s) exceeds total video duration ({total_dur:.1f}s).",
        }
        if args.json:
            print(json.dumps(res))
        else:
            print(f"Error [INVALID_RANGE]: {res['message']}", file=sys.stderr)
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
    if hasattr(args, "days") and args.days == 0:
        cutoff = now + 1.0  # Clean everything immediately
    elif hasattr(args, "days") and args.days is not None:
        cutoff = now - (args.days * 86400)
    else:
        cutoff = now - (get_default_ttl_hours() * 3600)
    cleaned = 0

    for item in os.listdir(cache_root):
        folder = os.path.join(cache_root, item)
        if os.path.isdir(folder) and item.startswith("session_"):
            mtime = get_session_latest_mtime(folder)
            if mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                cleaned += 1

    print(f"Cleaned {cleaned} cache session(s) older than {args.days} day(s) from {cache_root}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent-reels-viewer",
        description="Lightweight multimodal agent skill to inspect and understand Instagram Reels, TikTok, and YouTube Shorts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # inspect
    p_inspect = subparsers.add_parser("inspect", help="Inspect and analyze a video link or local file")
    p_inspect.add_argument("url", help="Instagram Reel, TikTok, or YouTube Shorts URL / local file path")
    p_inspect.add_argument("--mode", choices=["quick", "standard", "deep"], default="standard", help="Inspection depth")
    p_inspect.add_argument("--model", choices=["tiny", "base", "small"], default="base", help="Whisper speech model size (default: base)")
    p_inspect.add_argument("--cookies", help="Path to cookies.txt for authenticated extraction")
    p_inspect.add_argument("--output", help="Custom output directory")
    p_inspect.add_argument("--max-frames", type=int, help="Override maximum frames cap")
    p_inspect.add_argument("--debug-frames", action="store_true", help="Print detailed frame extraction and deduplication metrics to stderr")
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
    p_doctor.add_argument("--model", choices=["tiny", "base", "small"], default="base", help="Whisper speech model to check/cache (default: base)")
    p_doctor.set_defaults(func=lambda args: 0 if run_doctor(download_model=args.download_model, model_name=args.model)["core_ready"] else 1)

    # clean
    p_clean = subparsers.add_parser("clean", help="Clean old cache sessions")
    p_clean.add_argument("--days", type=int, default=3, help="Remove sessions older than N days")
    p_clean.set_defaults(func=cmd_clean)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
