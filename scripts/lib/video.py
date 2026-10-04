"""Video processing module: scene detection, adaptive keyframe extraction, and deduplication."""

import glob
import math
import os
import re
import shutil
import subprocess
from typing import List, Optional, Tuple

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import imagehash
    IMAGEHASH_AVAILABLE = True
except ImportError:
    IMAGEHASH_AVAILABLE = False


def get_ffmpeg_path() -> Optional[str]:
    """Find ffmpeg binary."""
    return shutil.which("ffmpeg") or shutil.which("/Users/waniyaro/.local/bin/ffmpeg")


def get_ffprobe_path() -> Optional[str]:
    """Find ffprobe binary."""
    return shutil.which("ffprobe") or shutil.which("/Users/waniyaro/.local/bin/ffprobe")


def get_video_duration(video_path: str) -> float:
    """Get video duration in seconds via ffprobe or ffmpeg."""
    ffprobe = get_ffprobe_path()
    if ffprobe:
        cmd = [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            val = float(res.stdout.strip())
            if val > 0:
                return val
        except Exception:
            pass

    # Fallback to ffmpeg -i
    ffmpeg = get_ffmpeg_path()
    if ffmpeg:
        try:
            res = subprocess.run([ffmpeg, "-i", video_path], capture_output=True, text=True, timeout=10)
            # Look for Duration: 00:00:15.30
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", res.stderr)
            if m:
                hrs, mins, secs = m.groups()
                return int(hrs) * 3600 + int(mins) * 60 + float(secs)
        except Exception:
            pass

    return 0.0


def deduplicate_frames(frame_paths: List[str], max_distance: int = 6) -> List[str]:
    """Filter out near-duplicate consecutive frames using perceptual hashing."""
    if not frame_paths or not (PIL_AVAILABLE and IMAGEHASH_AVAILABLE):
        return frame_paths

    kept = [frame_paths[0]]
    try:
        prev_hash = imagehash.phash(Image.open(frame_paths[0]))
    except Exception:
        return frame_paths

    for p in frame_paths[1:]:
        try:
            curr_hash = imagehash.phash(Image.open(p))
            diff = curr_hash - prev_hash
            if diff >= max_distance:
                kept.append(p)
                prev_hash = curr_hash
            else:
                # Remove duplicate file from disk to save space
                try:
                    os.remove(p)
                except OSError:
                    pass
        except Exception:
            kept.append(p)

    return kept


def extract_keyframes(
    video_path: str,
    output_dir: str,
    max_frames: int = 14,
    has_speech: bool = True,
) -> List[Tuple[float, str]]:
    """Extract representative scene keyframes.
    
    If video has speech: relies on scene changes + key interval.
    If video has no speech (visual meme / music only): samples more frequently (adaptive).
    Returns list of (timestamp_seconds, frame_filepath).
    """
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return []

    frames_dir = os.path.join(output_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    duration = get_video_duration(video_path)
    if duration <= 0:
        duration = 30.0  # reasonable fallback

    # Adaptive interval: if no speech, sample more aggressively
    step = 2.0 if not has_speech else max(2.5, duration / max_frames)
    
    # Scene detection filter:
    # Captures significant scene cuts or periodic fallbacks
    # Scale to max 768px along height or width, preserving vertical aspect ratio
    scale_filter = "scale='if(gt(iw,ih),min(768,iw),-2)':'if(gt(iw,ih),-2,min(768,ih))'"
    
    # 1. First pass: extract by scene cuts or periodic fps
    pattern = os.path.join(frames_dir, "frame_%03d.jpg")
    
    # If duration is short (< 60s), sample every `step` seconds
    fps_val = 1.0 / step
    filter_expr = f"fps={fps_val:.3f},{scale_filter}"

    cmd = [
        ffmpeg,
        "-y",
        "-i", video_path,
        "-vf", filter_expr,
        "-q:v", "3",
        pattern,
    ]

    try:
        subprocess.run(cmd, capture_output=True, timeout=40)
    except Exception:
        return []

    raw_frames = sorted(glob.glob(os.path.join(frames_dir, "frame_*.jpg")))
    if not raw_frames:
        return []

    # Optional deduplication
    filtered_frames = deduplicate_frames(raw_frames)

    # Cap to max_frames
    if len(filtered_frames) > max_frames:
        indices = [int(i * (len(filtered_frames) - 1) / (max_frames - 1)) for i in range(max_frames)]
        to_keep = set([filtered_frames[i] for i in indices])
        for f in filtered_frames:
            if f not in to_keep:
                try:
                    os.remove(f)
                except OSError:
                    pass
        filtered_frames = sorted(list(to_keep))

    # Rename with timestamp info for clarity: frame_01_00-04s.jpg
    final_results = []
    total_count = len(filtered_frames)
    for idx, fpath in enumerate(filtered_frames):
        ts = round(idx * (duration / max(1, total_count)), 1)
        mins = int(ts // 60)
        secs = int(ts % 60)
        ts_str = f"{mins:02d}-{secs:02d}s"
        new_name = os.path.join(frames_dir, f"frame_{idx+1:02d}_{ts_str}.jpg")
        os.rename(fpath, new_name)
        final_results.append((ts, new_name))

    return final_results


def extract_range_frames(
    video_path: str,
    start_sec: float,
    end_sec: float,
    output_dir: str,
    count: int = 6,
    hires: bool = False,
) -> List[Tuple[float, str]]:
    """Second-pass on-demand inspection: extract frames in a specific time range."""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return []

    range_dir = os.path.join(output_dir, "zoom_frames")
    os.makedirs(range_dir, exist_ok=True)

    duration = max(0.5, end_sec - start_sec)
    fps_val = count / duration

    scale_filter = "scale=1080:-2" if hires else "scale='if(gt(iw,ih),min(768,iw),-2)':'if(gt(iw,ih),-2,min(768,ih))'"
    pattern = os.path.join(range_dir, "zoom_%03d.jpg")

    cmd = [
        ffmpeg,
        "-y",
        "-ss", str(start_sec),
        "-t", str(duration),
        "-i", video_path,
        "-vf", f"fps={fps_val:.3f},{scale_filter}",
        "-q:v", "2" if hires else "4",
        pattern,
    ]

    try:
        subprocess.run(cmd, capture_output=True, timeout=30)
    except Exception:
        return []

    raw = sorted(glob.glob(os.path.join(range_dir, "zoom_*.jpg")))
    results = []
    for idx, p in enumerate(raw):
        ts = round(start_sec + (idx * duration / max(1, len(raw))), 1)
        mins = int(ts // 60)
        secs = int(ts % 60)
        new_p = os.path.join(range_dir, f"zoom_{idx+1:02d}_{mins:02d}-{secs:02d}s.jpg")
        os.rename(p, new_p)
        results.append((ts, new_p))

    return results
