"""Environment diagnostic utility (doctor)."""

import os
import shutil
import subprocess
import sys
from typing import Dict, List, Tuple


def check_binary(name: str) -> Tuple[bool, str]:
    """Check if a system CLI binary exists and return version."""
    path = shutil.which(name)
    if not path:
        # Check ~/.local/bin or brew paths
        candidates = [
            f"/Users/waniyaro/.local/bin/{name}",
            f"/opt/homebrew/bin/{name}",
            f"/usr/local/bin/{name}",
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


def run_doctor() -> Dict[str, Any]:
    """Perform health checks on all dependencies and print structured diagnostic."""
    print("=" * 60)
    print("🩺 Agent Reels Viewer — Environment Diagnostics (Doctor)")
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
    print(f"  {'✅' if ffmpeg_ok else '❌'} ffmpeg:  {ffmpeg_info}")
    print(f"  {'✅' if ffprobe_ok else '❌'} ffprobe: {ffprobe_info}")
    print(f"  {'✅' if ytdlp_ok else '❌'} yt-dlp:  {ytdlp_info}")

    # 2. Python Libraries
    whisper_ok, whisper_info = check_python_package("faster_whisper")
    pillow_ok, pillow_info = check_python_package("PIL")
    imghash_ok, imghash_info = check_python_package("imagehash")

    print("\n[Python Libraries]")
    print(f"  {'✅' if whisper_ok else '⚠️ '} faster-whisper: {whisper_info} (optional, for speech audio)")
    print(f"  {'✅' if pillow_ok else '⚠️ '} Pillow (PIL):    {pillow_info}")
    print(f"  {'✅' if imghash_ok else '⚠️ '} imagehash:       {imghash_info} (optional, for keyframe deduplication)")

    # 3. Cookies detection
    cookie_paths = [
        os.path.expanduser("~/.config/agent-reels-viewer/cookies.txt"),
        os.path.abspath("./cookies.txt"),
    ]
    cookies_found = [p for p in cookie_paths if os.path.exists(p)]
    print("\n[Authentication & Cookies]")
    if cookies_found:
        print(f"  ✅ Found cookies file at: {cookies_found[0]}")
    else:
        print("  ℹ️  No cookies.txt found in default locations.")
        print("     (Instagram Reels may require cookies if platform rate-limits or asks for login).")

    # 4. Summary & Advice
    print("\n" + "-" * 60)
    ready = ffmpeg_ok and ytdlp_ok
    if ready:
        print("✨ Status: CORE ENGINE READY!")
        print("   Video download and visual keyframe extraction are fully operational.")
    else:
        print("⚠️ Status: ACTION REQUIRED!")
        if not ffmpeg_ok:
            print("   → Install ffmpeg: brew install ffmpeg (macOS) or apt install ffmpeg (Ubuntu)")
        if not ytdlp_ok:
            print("   → Install yt-dlp: brew install yt-dlp or pip install -U yt-dlp")

    if not whisper_ok:
        print("   → To enable speech transcription: pip install faster-whisper")

    print("=" * 60)

    return {
        "ffmpeg": ffmpeg_ok,
        "ffprobe": ffprobe_ok,
        "yt_dlp": ytdlp_ok,
        "faster_whisper": whisper_ok,
        "pillow": pillow_ok,
        "imagehash": imghash_ok,
        "core_ready": ready,
    }
