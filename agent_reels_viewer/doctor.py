"""Environment diagnostic utility (doctor)."""

import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Tuple

from agent_reels_viewer.video import get_ffmpeg_version


def check_binary(name: str) -> Tuple[bool, str]:
    """Check if a system CLI binary exists and return version."""
    path = shutil.which(name)
    if not path:
        candidates = [
            os.path.expanduser(f"~/.local/bin/{name}"),
            f"/opt/homebrew/bin/{name}",
            f"/usr/local/bin/{name}",
            f"/usr/bin/{name}",
            f"C:\\ProgramData\\chocolatey\\bin\\{name}.exe",
            f"C:\\ProgramData\\chocolatey\\lib\\ffmpeg\\tools\\ffmpeg\\bin\\{name}.exe",
            f"C:\\ffmpeg\\bin\\{name}.exe",
            f"C:\\Program Files\\ffmpeg\\bin\\{name}.exe",
        ]
        for c in candidates:
            if os.path.exists(c):
                path = c
                break

    if not path:
        return False, "Not found in PATH"

    try:
        res = subprocess.run([path, "-version" if name.startswith("ff") else "--version"], capture_output=True, text=True, timeout=5)
        first_line = (res.stdout or res.stderr).splitlines()[0][:60]
        return True, f"{path} ({first_line})"
    except Exception as e:
        return True, f"{path} (found, but version check error: {str(e)})"


def check_python_package(pkg_name: str) -> Tuple[bool, str]:
    """Check if a python package can be imported."""
    try:
        mod = __import__(pkg_name)
        ver = getattr(mod, "__version__", "installed")
        return True, f"v{ver}"
    except ImportError:
        return False, "Not installed"
    except Exception as e:
        return False, f"Import error: {str(e)}"


def check_ffmpeg_av1_support() -> Tuple[bool, str]:
    """Check if system ffmpeg supports AV1 decoding (libdav1d, libaom-av1, or native av1)."""
    ffmpeg = shutil.which("ffmpeg") or os.path.expanduser("~/.local/bin/ffmpeg")
    try:
        r = subprocess.run([ffmpeg, "-decoders"], capture_output=True, text=True, timeout=3)
        dec = r.stdout.lower()
        if "libdav1d" in dec:
            return True, "libdav1d (fast)"
        elif "libaom-av1" in dec or "libaom" in dec:
            return True, "libaom (standard)"
        elif "av1" in dec:
            return True, "av1 (generic)"
        else:
            return False, "Not available (AV1 videos may fail to decode)"
    except Exception as e:
        return False, f"Could not query decoders ({e})"


def run_doctor(download_model: bool = False, model_name: str = "base") -> Dict[str, Any]:
    """Perform health checks on all dependencies and print structured diagnostic."""
    print("=" * 60)
    print("Agent Reels Viewer — Environment Diagnostics (Doctor)")
    print("=" * 60)

    # 1. System Binaries
    ffmpeg_ok, ffmpeg_info = check_binary("ffmpeg")
    ffprobe_ok, ffprobe_info = check_binary("ffprobe")
    ytdlp_ok, ytdlp_info = check_binary("yt-dlp")

    # Fallback check python -m yt_dlp
    if not ytdlp_ok:
        try:
            r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                ytdlp_ok = True
                ytdlp_info = f"python3 -m yt_dlp v{r.stdout.strip()}"
        except Exception:
            pass

    print("\n[System Binaries]")
    print(f"  [{'PASS' if ffmpeg_ok else 'FAIL'}] ffmpeg:  {ffmpeg_info}")
    if ffmpeg_ok:
        major, minor = get_ffmpeg_version()
        if (major, minor) < (5, 1):
            print(f"         [WARN] FFmpeg version {major}.{minor} is older than 5.1. Legacy '-vsync' mode will be used.")
        av1_ok, av1_desc = check_ffmpeg_av1_support()
        if av1_ok:
            print(f"         [INFO] FFmpeg AV1 decoder: {av1_desc}")
        else:
            print(f"         [WARN] FFmpeg AV1 decoder: {av1_desc}. Install ffmpeg with libdav1d/libaom.")
    print(f"  [{'PASS' if ffprobe_ok else 'WARN'}] ffprobe: {ffprobe_info}")
    print(f"  [{'PASS' if ytdlp_ok else 'FAIL'}] yt-dlp:  {ytdlp_info}")

    # 2. Python Libraries
    whisper_ok, whisper_info = check_python_package("faster_whisper")
    pillow_ok, pillow_info = check_python_package("PIL")

    print("\n[Python Libraries]")
    print(f"  [{'PASS' if whisper_ok else 'WARN'}] faster-whisper: {whisper_info} (speech engine: {'READY' if whisper_ok else 'NOT READY'})")
    print(f"  [{'PASS' if pillow_ok else 'FAIL'}] Pillow (PIL):    {pillow_info}")

    # 3. Model Pre-download (optional warm cache)
    if download_model:
        if whisper_ok:
            print(f"\n[Model Download]")
            print(f"  Downloading / warming up Whisper model ({model_name})...")
            try:
                from faster_whisper import download_model as hf_download_model
                cached_path = hf_download_model(model_name)
                print(f"  [PASS] Whisper '{model_name}' model successfully cached at: {cached_path}")
            except Exception as e:
                print(f"  [FAIL] Failed to pre-download model: {e}")
        else:
            print("\n[Model Download]")
            print("  [SKIP] faster-whisper is not installed. Run: pip install '.[speech]'")

    # 4. Cookies detection
    cookie_paths = [
        os.path.expanduser("~/.config/agent-reels-viewer/cookies.txt"),
        os.path.abspath("./cookies.txt"),
    ]
    cookies_found = [p for p in cookie_paths if os.path.exists(p)]
    print("\n[Authentication & Cookies]")
    if cookies_found:
        print(f"  [INFO] Found cookies file at: {cookies_found[0]}")
    else:
        print("  [INFO] No cookies.txt found in default locations.")
        print("         (Instagram Reels may require cookies if platform rate-limits or asks for login).")

    # 5. Summary & Advice
    print("\n" + "-" * 60)
    core_ready = ffmpeg_ok and ytdlp_ok and pillow_ok
    full_ready = core_ready and whisper_ok

    if full_ready:
        print("Status: FULL SYSTEM READY!")
        print("Video downloading, visual keyframes, and speech transcription are fully operational.")
    elif core_ready:
        print("Status: PARTIAL (visual only - speech transcription disabled)")
        print("Video download and visual keyframe extraction are ready.")
        print("To enable speech transcription: pip install '.[speech]'")
    else:
        print("Status: ACTION REQUIRED!")
        if not ffmpeg_ok:
            print("  - Install ffmpeg: brew install ffmpeg (macOS) or apt install ffmpeg (Ubuntu)")
        if not ytdlp_ok:
            print("  - Install yt-dlp: brew install yt-dlp or pip install -U yt-dlp")
        if not pillow_ok:
            print("  - Install Pillow: pip install Pillow")

    print("=" * 60)

    return {
        "ffmpeg": ffmpeg_ok,
        "ffprobe": ffprobe_ok,
        "yt_dlp": ytdlp_ok,
        "faster_whisper": whisper_ok,
        "pillow": pillow_ok,
        "core_ready": core_ready,
        "full_ready": full_ready,
    }
