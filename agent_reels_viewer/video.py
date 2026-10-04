"""Video processing module: scene detection, adaptive keyframe extraction, and pure Pillow deduplication."""

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


def get_ffmpeg_version() -> Tuple[int, int]:
    """Parse FFmpeg major and minor version numbers. Returns e.g. (9, 0) or (4, 4)."""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return (0, 0)
    try:
        res = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=5)
        first_line = (res.stdout or res.stderr).splitlines()[0]
        m = re.search(r"ffmpeg version (?:n)?(\d+)\.(\d+)", first_line)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return (5, 1)


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


def has_audio_stream(video_path: str) -> bool:
    """Check if video container contains at least one audio stream."""
    ffprobe = get_ffprobe_path()
    if ffprobe:
        cmd = [
            ffprobe,
            "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            video_path,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            return len(res.stdout.strip()) > 0
        except Exception:
            pass

    ffmpeg = get_ffmpeg_path()
    if ffmpeg:
        try:
            res = subprocess.run([ffmpeg, "-i", video_path], capture_output=True, text=True, timeout=5)
            return "Audio:" in (res.stderr or "")
        except Exception:
            pass

    return True


def compute_frame_signature(image_path: str, target_width: int = 128) -> Optional[Tuple[bytes, Tuple[int, int]]]:
    """Load image as downscaled grayscale bytes for precise pixel difference."""
    if not PIL_AVAILABLE:
        return None
    try:
        with Image.open(image_path) as img:
            w, h = img.size
            if w <= 0 or h <= 0:
                return None
            target_height = max(1, int(h * target_width / w))
            resized = img.convert("L").resize((target_width, target_height), Image.Resampling.BILINEAR)
            return resized.tobytes(), (target_width, target_height)
    except Exception:
        return None


def calculate_frame_difference(sig1: Tuple[bytes, Tuple[int, int]], sig2: Tuple[bytes, Tuple[int, int]], pixel_threshold: int = 16) -> float:
    """Calculate the fraction of pixels with difference > pixel_threshold.
    Returns float in range [0.0, 1.0]."""
    bytes1, size1 = sig1
    bytes2, size2 = sig2
    if size1 != size2 or len(bytes1) != len(bytes2) or len(bytes1) == 0:
        return 1.0
    diff_count = sum(1 for p1, p2 in zip(bytes1, bytes2) if abs(p1 - p2) > pixel_threshold)
    return diff_count / len(bytes1)


def deduplicate_frames(
    frames: List[Tuple[float, str]],
    is_dense_mode: bool = False,
) -> List[Tuple[float, str]]:
    """Filter out near-duplicate consecutive frames using downscaled pixel difference.

    In dense visual mode (speech_status none/skipped/error):
      Only virtually identical frames (diff < 0.0002) are dropped, ensuring subtle text edits,
      character typing, and meme punchlines are preserved.
    In conversational mode (speech present):
      Threshold is 0.015 to suppress static talking head frames.
    """
    if not frames or not PIL_AVAILABLE or len(frames) <= 1:
        return frames

    threshold = 0.0002 if is_dense_mode else 0.015
    kept = [frames[0]]
    prev_sig = compute_frame_signature(frames[0][1])

    for pts, fpath in frames[1:]:
        curr_sig = compute_frame_signature(fpath)
        if prev_sig is None or curr_sig is None:
            kept.append((pts, fpath))
            prev_sig = curr_sig
            continue

        diff = calculate_frame_difference(prev_sig, curr_sig)
        if diff >= threshold:
            kept.append((pts, fpath))
            prev_sig = curr_sig
        else:
            try:
                os.remove(fpath)
            except OSError:
                pass

    return kept


def sanitize_stderr(stderr_text: str) -> str:
    """Sanitize stderr to strip potential cookie paths or sensitive tokens."""
    cleaned = re.sub(r'cookies(?:\.txt)?', '[COOKIES_REDACTED]', stderr_text, flags=re.IGNORECASE)
    cleaned = re.sub(r'--cookies\s+[^\s]+', '--cookies [REDACTED]', cleaned)
    return cleaned.strip()[-300:]


def extract_keyframes(
    video_path: str,
    output_dir: str,
    max_frames: Optional[int] = None,
    has_speech: bool = True,
) -> Tuple[List[Tuple[float, str]], str, str, str]:
    """Extract scene keyframes using hybrid scene detection + adaptive cadence floor.
    
    Returns: (keyframes_list, timestamp_type, error_code, error_message)
    timestamp_type is 'exact' or 'approximate'
    """
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return [], "exact", "FFMPEG_MISSING", "ffmpeg binary is not found."

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

    # Compatibility: -fps_mode in FFmpeg >= 5.1, -vsync in older
    major, minor = get_ffmpeg_version()
    vfr_args = ["-fps_mode", "vfr"] if (major, minor) >= (5, 1) else ["-vsync", "vfr"]

    filter_expr = f"select='isnan(prev_selected_t)+gt(scene,0.3)+gte(t-prev_selected_t,{step:.2f})',{scale_filter},showinfo"

    cmd = [
        ffmpeg,
        "-y",
        "-i", video_path,
        "-vf", filter_expr,
    ] + vfr_args + [
        "-pix_fmt", "yuvj420p",
        "-q:v", "3",
        raw_pattern,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            err_tail = sanitize_stderr(proc.stderr)
            return [], "exact", "FRAME_EXTRACTION_FAILED", f"FFmpeg error ({proc.returncode}): {err_tail}"
    except subprocess.TimeoutExpired:
        return [], "exact", "FRAME_EXTRACTION_FAILED", "Keyframe extraction timed out after 60s."
    except Exception as e:
        return [], "exact", "FRAME_EXTRACTION_FAILED", f"Extraction error: {str(e)}"

    # Parse PTS time from showinfo lines in stderr
    pts_map = {}
    for line in proc.stderr.splitlines():
        m = re.search(r"n:\s*(\d+)\s+pts:\s*\d+\s+pts_time:([0-9.]+)", line)
        if m:
            pts_map[int(m.group(1))] = float(m.group(2))

    raw_files = sorted(glob.glob(os.path.join(frames_dir, "raw_frame_*.jpg")))
    if not raw_files:
        err_tail = sanitize_stderr(proc.stderr)
        return [], "exact", "FRAME_EXTRACTION_FAILED", f"No keyframes were generated. FFmpeg output: {err_tail}"

    frames_with_pts: List[Tuple[float, str]] = []
    all_pts_exact = True
    for idx, fpath in enumerate(raw_files):
        if idx in pts_map:
            pts = pts_map[idx]
        else:
            all_pts_exact = False
            pts = round(idx * (duration / max(1, len(raw_files))), 2)
        frames_with_pts.append((pts, fpath))

    timestamp_type = "exact" if all_pts_exact else "approximate"

    # Deduplicate near-identical frames using downscaled pixel difference
    filtered = deduplicate_frames(frames_with_pts, is_dense_mode=(not has_speech))

    # Cap to effective_max if needed
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

    return final_results, timestamp_type, "", ""


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
