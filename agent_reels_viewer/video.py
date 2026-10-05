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
        candidates = [
            os.path.expanduser("~/.local/bin/ffmpeg"),
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/usr/bin/ffmpeg",
            "C:\\ProgramData\\chocolatey\\bin\\ffmpeg.exe",
            "C:\\ProgramData\\chocolatey\\lib\\ffmpeg\\tools\\ffmpeg\\bin\\ffmpeg.exe",
            "C:\\ffmpeg\\bin\\ffmpeg.exe",
            "C:\\Program Files\\ffmpeg\\bin\\ffmpeg.exe",
        ]
        for c in candidates:
            if os.path.exists(c):
                path = c
                break
    return path


def get_ffprobe_path() -> Optional[str]:
    """Find ffprobe binary."""
    path = shutil.which("ffprobe")
    if not path:
        candidates = [
            os.path.expanduser("~/.local/bin/ffprobe"),
            "/opt/homebrew/bin/ffprobe",
            "/usr/local/bin/ffprobe",
            "/usr/bin/ffprobe",
            "C:\\ProgramData\\chocolatey\\bin\\ffprobe.exe",
            "C:\\ProgramData\\chocolatey\\lib\\ffmpeg\\tools\\ffmpeg\\bin\\ffprobe.exe",
            "C:\\ffmpeg\\bin\\ffprobe.exe",
            "C:\\Program Files\\ffmpeg\\bin\\ffprobe.exe",
        ]
        for c in candidates:
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


def compute_frame_signature(
    image_path: str,
    target_width: int = 256,
    grid: Tuple[int, int] = (16, 16),
) -> Optional[Tuple[List[bytes], Tuple[int, int]]]:
    """Load image as downscaled grayscale signature (target_width=256 preserving aspect ratio) divided into a 16x16 grid of tiles."""
    if not PIL_AVAILABLE:
        return None
    try:
        with Image.open(image_path) as img:
            w, h = img.size
            if w <= 0 or h <= 0:
                return None
            target_height = max(grid[1], int(h * target_width / w))
            resized = img.convert("L").resize((target_width, target_height), Image.Resampling.BILINEAR)
            pixels = bytes(resized.tobytes())

            cols, rows = grid
            tile_w = target_width // cols
            tile_h = target_height // rows
            tiles = []
            for r in range(rows):
                for c in range(cols):
                    t_bytes = bytearray()
                    for y in range(r * tile_h, (r + 1) * tile_h):
                        row_start = y * target_width
                        t_bytes.extend(pixels[row_start + c * tile_w : row_start + (c + 1) * tile_w])
                    tiles.append(bytes(t_bytes))
            return tiles, (target_width, target_height)
    except Exception:
        return None


def calculate_frame_difference(
    sig1: Tuple[List[bytes], Tuple[int, int]],
    sig2: Tuple[List[bytes], Tuple[int, int]],
    pixel_threshold: int = 16,
) -> float:
    """Calculate the maximum fraction of differing pixels across any tile (max tile difference).
    A pixel differs if abs(p1 - p2) > pixel_threshold (default 16). Returns float in range [0.0, 1.0]."""
    tiles1, size1 = sig1
    tiles2, size2 = sig2
    if size1 != size2 or len(tiles1) != len(tiles2) or len(tiles1) == 0:
        return 1.0

    max_diff = 0.0
    for t1, t2 in zip(tiles1, tiles2):
        if not t1:
            continue
        diff_count = sum(1 for p1, p2 in zip(t1, t2) if abs(p1 - p2) > pixel_threshold)
        ratio = diff_count / len(t1)
        if ratio > max_diff:
            max_diff = ratio
    return max_diff


def deduplicate_frames(
    frames: List[Tuple[float, str]],
    is_dense_mode: bool = False,
    threshold: Optional[float] = None,
    return_stats: bool = False,
):
    """Filter out near-duplicate consecutive frames using tiled block difference (256px width preserving aspect ratio, 16x16 grid, max fraction of differing pixels > 16 across tiles).

    In dense visual mode (speech_status none/skipped/error):
      Threshold is 0.012 (1.2% max tile difference). This drops identical duplicates
      and compression artifacts (< 0.005) while preserving fine single-character edits (>= 0.027)
      and subtitle changes.
    In conversational mode (speech present):
      Threshold is 0.080 (8.0% max tile difference) to suppress motionless talking heads
      while retaining subtitle changes (>= 0.180) and gesture/scene shifts.
    """
    if not frames or not PIL_AVAILABLE or len(frames) <= 1:
        return (frames, []) if return_stats else frames

    if threshold is None:
        threshold = 0.012 if is_dense_mode else 0.080

    kept = [frames[0]]
    prev_sig = compute_frame_signature(frames[0][1])
    diff_records = []

    for pts, fpath in frames[1:]:
        curr_sig = compute_frame_signature(fpath)
        if prev_sig is None or curr_sig is None:
            kept.append((pts, fpath))
            prev_sig = curr_sig
            diff_records.append({
                "from_sec": kept[-2][0],
                "to_sec": pts,
                "diff": 1.0,
                "threshold": threshold,
                "decision": "kept",
            })
            continue

        diff = calculate_frame_difference(prev_sig, curr_sig)
        if diff >= threshold:
            diff_records.append({
                "from_sec": kept[-1][0],
                "to_sec": pts,
                "diff": round(diff, 4),
                "threshold": threshold,
                "decision": "kept",
            })
            kept.append((pts, fpath))
            prev_sig = curr_sig
        else:
            diff_records.append({
                "from_sec": kept[-1][0],
                "to_sec": pts,
                "diff": round(diff, 4),
                "threshold": threshold,
                "decision": "dropped",
            })
            try:
                os.remove(fpath)
            except OSError:
                pass

    return (kept, diff_records) if return_stats else kept


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
    mode: str = "standard",
    return_stats: bool = False,
):
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

    is_deep = (mode == "deep")
    if not has_speech:
        step = 1.0 if is_deep else 1.5
        default_cap = 30 if is_deep else 20
        dedup_threshold = 0.008 if is_deep else 0.012
    else:
        step = max(2.0, duration / 20.0) if is_deep else max(3.0, duration / 12.0)
        default_cap = 20 if is_deep else 12
        dedup_threshold = 0.050 if is_deep else 0.080

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
    filtered, pairwise_diffs = deduplicate_frames(
        frames_with_pts,
        is_dense_mode=(not has_speech),
        threshold=dedup_threshold,
        return_stats=True,
    )

    raw_candidates_count = len(frames_with_pts)
    kept_after_dedup = len(filtered)
    dropped_by_dedup = raw_candidates_count - kept_after_dedup
    dropped_by_cap = 0

    # Cap to effective_max if needed, choosing frames with highest visual transition diff
    if len(filtered) > effective_max:
        dropped_by_cap = len(filtered) - effective_max
        if effective_max == 1:
            to_keep_indices = {0}
        elif effective_max == 2:
            to_keep_indices = {0, len(filtered) - 1}
        else:
            # Score interior candidates by difference from previous frame
            interior_scores = []
            for i in range(1, len(filtered) - 1):
                prev_sig = compute_frame_signature(filtered[i - 1][1])
                curr_sig = compute_frame_signature(filtered[i][1])
                diff_val = calculate_frame_difference(prev_sig, curr_sig) if (prev_sig and curr_sig) else 1.0
                interior_scores.append((diff_val, i))

            # Pick top (effective_max - 2) interior candidates with highest difference
            interior_scores.sort(key=lambda x: (-x[0], x[1]))
            selected_interior = [idx for _, idx in interior_scores[:(effective_max - 2)]]
            to_keep_indices = {0, len(filtered) - 1}.union(selected_interior)

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

    stats = {
        "raw_candidates": raw_candidates_count,
        "kept_after_dedup": kept_after_dedup,
        "dropped_by_dedup": dropped_by_dedup,
        "dropped_by_cap": dropped_by_cap,
        "final_frames": len(final_results),
        "mode": mode,
        "has_speech": has_speech,
        "step_sec": round(step, 2),
        "threshold": dedup_threshold,
        "pairwise_diffs": pairwise_diffs,
    }
    if return_stats:
        return final_results, timestamp_type, "", "", stats
    return final_results, timestamp_type, "", ""


def extract_range_frames(
    video_path: str,
    start_sec: float,
    end_sec: float,
    output_dir: str,
    count: int = 6,
    hires: bool = False,
) -> List[Tuple[float, str]]:
    """Second-pass on-demand inspection: extract frames in a specific time range.
    
    Each range request is isolated in its own subdirectory to prevent cross-contamination
    across multiple calls. Timestamps are extracted from FFmpeg showinfo pts_time.
    """
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg or not os.path.exists(video_path):
        return []

    duration = max(0.1, end_sec - start_sec)
    step = duration / max(1, count)

    range_slug = f"{start_sec:.1f}-{end_sec:.1f}_c{count}_{'hires' if hires else 'std'}"
    range_dir = os.path.join(output_dir, "zoom_frames", range_slug)
    os.makedirs(range_dir, exist_ok=True)

    pattern = os.path.join(range_dir, "raw_zoom_%03d.jpg")
    for stale in glob.glob(os.path.join(range_dir, "raw_zoom_*.jpg")):
        try:
            os.remove(stale)
        except OSError:
            pass

    scale_filter = "scale=1080:-2" if hires else "scale='if(gt(iw,ih),min(768,iw),-2)':'if(gt(iw,ih),-2,min(768,ih))'"
    major, minor = get_ffmpeg_version()
    vfr_args = ["-fps_mode", "vfr"] if (major, minor) >= (5, 1) else ["-vsync", "vfr"]
    filter_expr = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{step:.3f})',{scale_filter},showinfo"

    cmd = [
        ffmpeg,
        "-y",
        "-ss", str(start_sec),
        "-t", str(duration),
        "-i", video_path,
        "-vf", filter_expr,
    ] + vfr_args + [
        "-q:v", "2" if hires else "4",
        pattern,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception:
        return []

    pts_map = {}
    if proc.stderr:
        for line in proc.stderr.splitlines():
            m = re.search(r"n:\s*(\d+)\s+pts:\s*\d+\s+pts_time:([0-9.]+)", line)
            if m:
                pts_map[int(m.group(1))] = float(m.group(2))

    raw = sorted(glob.glob(os.path.join(range_dir, "raw_zoom_*.jpg")))
    if len(raw) > count:
        for extra in raw[count:]:
            try:
                os.remove(extra)
            except OSError:
                pass
        raw = raw[:count]
    results = []
    for idx, p in enumerate(raw):
        if idx in pts_map:
            actual_pts = round(start_sec + pts_map[idx], 2)
        else:
            actual_pts = round(start_sec + (idx * duration / max(1, len(raw))), 2)

        mins = int(actual_pts // 60)
        secs = int(actual_pts % 60)
        final_path = os.path.join(range_dir, f"zoom_{idx+1:02d}_{mins:02d}-{secs:02d}s.jpg")
        os.replace(p, final_path)
        results.append((actual_pts, final_path))

    return results
