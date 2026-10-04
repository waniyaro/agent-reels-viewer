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


def get_ffprobe_path() -> Optional[str]:
    """Find ffprobe binary."""
    path = shutil.which("ffprobe")
    if not path:
        for c in [
            os.path.expanduser("~/.local/bin/ffprobe"),
            "/opt/homebrew/bin/ffprobe",
            "/usr/local/bin/ffprobe",
            "/usr/bin/ffprobe",
        ]:
            if os.path.exists(c):
                path = c
                break
    return path


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

    ffmpeg = get_ffmpeg_path()
    if ffmpeg:
        try:
            res = subprocess.run([ffmpeg, "-i", video_path], capture_output=True, text=True, timeout=10)
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", res.stderr)
            if m:
                hrs, mins, secs = m.groups()
                return int(hrs) * 3600 + int(mins) * 60 + float(secs)
        except Exception:
            pass

    return 0.0


def deduplicate_frames(
    frames: List[Tuple[float, str]],
    max_distance: int = 6,
) -> List[Tuple[float, str]]:
    """Filter out near-duplicate consecutive frames using perceptual hashing.
    Preserves true PTS timestamps for surviving frames."""
    if not frames or not (PIL_AVAILABLE and IMAGEHASH_AVAILABLE):
        return frames

    kept = [frames[0]]
    try:
        prev_hash = imagehash.phash(Image.open(frames[0][1]))
    except Exception:
        return frames

    for pts, p in frames[1:]:
        try:
            curr_hash = imagehash.phash(Image.open(p))
            diff = curr_hash - prev_hash
            if diff >= max_distance:
                kept.append((pts, p))
                prev_hash = curr_hash
            else:
                try:
                    os.remove(p)
                except OSError:
                    pass
        except Exception:
            kept.append((pts, p))

    return kept


def extract_keyframes(
    video_path: str,
    output_dir: str,
    max_frames: Optional[int] = None,
    has_speech: bool = True,
) -> List[Tuple[float, str]]:
    """Extract scene keyframes using hybrid scene detection + adaptive cadence floor.
    Uses real PTS timestamps extracted from FFmpeg showinfo filter.
    """
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return []

    frames_dir = os.path.join(output_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    # Clear any previous frame files
    for old_f in glob.glob(os.path.join(frames_dir, "frame_*.jpg")):
        try:
            os.remove(old_f)
        except OSError:
            pass
    for old_raw in glob.glob(os.path.join(frames_dir, "raw_frame_*.jpg")):
        try:
            os.remove(old_raw)
        except OSError:
            pass

    duration = get_video_duration(video_path)
    if duration <= 0:
        duration = 30.0

    if not has_speech:
        step = 1.5
        default_cap = 20
    else:
        step = max(3.0, duration / 12.0)
        default_cap = 12

    if max_frames is not None:
        effective_max = max(1, max_frames)
    else:
        effective_max = default_cap

    scale_filter = "scale='if(gt(iw,ih),min(768,iw),-2)':'if(gt(iw,ih),-2,min(768,ih))'"
    raw_pattern = os.path.join(frames_dir, "raw_frame_%04d.jpg")

    # Hybrid filter: scene cut (> 0.3) OR cadence floor (step).
    # showinfo outputs accurate pts_time in stderr.
    filter_expr = f"select='isnan(prev_selected_t)+gt(scene,0.3)+gte(t-prev_selected_t,{step:.2f})',{scale_filter},showinfo"

    cmd = [
        ffmpeg,
        "-y",
        "-i", video_path,
        "-vf", filter_expr,
        "-fps_mode", "vfr",
        "-pix_fmt", "yuvj420p",
        "-q:v", "3",
        raw_pattern,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception:
        return []

    # Parse PTS time from showinfo lines in stderr
    pts_map = {}
    for line in proc.stderr.splitlines():
        m = re.search(r"n:\s*(\d+)\s+pts:\s*\d+\s+pts_time:([0-9.]+)", line)
        if m:
            pts_map[int(m.group(1))] = float(m.group(2))

    raw_files = sorted(glob.glob(os.path.join(frames_dir, "raw_frame_*.jpg")))
    if not raw_files:
        return []

    frames_with_pts: List[Tuple[float, str]] = []
    for idx, fpath in enumerate(raw_files):
        pts = pts_map.get(idx, round(idx * (duration / max(1, len(raw_files))), 2))
        frames_with_pts.append((pts, fpath))

    # Deduplicate near-identical frames (perceptual hashing)
    filtered = deduplicate_frames(frames_with_pts)

    # Cap to effective_max if needed, preserving spacing and real PTS
    if len(filtered) > effective_max:
        if effective_max == 1:
            indices = [0]
        else:
            indices = [int(round(i * (len(filtered) - 1) / (effective_max - 1))) for i in range(effective_max)]

        to_keep_indices = set(indices)
        selected_frames = []
        for i, item in enumerate(filtered):
            if i in to_keep_indices:
                selected_frames.append(item)
            else:
                try:
                    os.remove(item[1])
                except OSError:
                    pass
        filtered = selected_frames

    # Rename with final zero-padded index and real PTS timestamp
    final_results: List[Tuple[float, str]] = []
    for idx, (pts, fpath) in enumerate(filtered):
        pts_rounded = round(pts, 1)
        mins = int(pts_rounded // 60)
        secs = int(pts_rounded % 60)
        ts_str = f"{mins:02d}-{secs:02d}s"
        final_name = os.path.join(frames_dir, f"frame_{idx+1:02d}_{ts_str}.jpg")
        if fpath != final_name:
            os.rename(fpath, final_name)
        final_results.append((pts_rounded, final_name))

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
    fps_val = max(1, count) / duration

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
