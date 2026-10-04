"""Comprehensive unit and integration test suite for agent-reels-viewer."""

import io
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

from scripts.lib.audio import extract_audio, transcribe_audio
from scripts.lib.downloader import (
    MAX_DURATION_SECONDS,
    detect_platform,
    fetch_metadata,
    validate_url,
)
from scripts.lib.timeline import assemble_timeline, format_timestamp
from scripts.lib.video import (
    extract_keyframes,
    extract_range_frames,
    get_ffmpeg_path,
)


class TestDownloaderValidation(unittest.TestCase):
    """Test URL schema, domain whitelisting, and SSRF/bypass protections."""

    def test_detect_platform_all(self):
        self.assertEqual(detect_platform("https://www.instagram.com/reel/C312345/"), "instagram")
        self.assertEqual(detect_platform("https://instagr.am/p/C312345/"), "instagram")
        self.assertEqual(detect_platform("https://www.tiktok.com/@user/video/123456789"), "tiktok")
        self.assertEqual(detect_platform("https://vm.tiktok.com/ZMxxxx/"), "tiktok")
        self.assertEqual(detect_platform("https://youtube.com/shorts/abc123xyz"), "youtube")
        self.assertEqual(detect_platform("https://youtu.be/abc123xyz"), "youtube")
        self.assertEqual(detect_platform("https://m.youtube.com/shorts/abc123xyz"), "youtube")

    def test_validate_url_positive_whitelist(self):
        valid_urls = [
            "https://www.instagram.com/reel/C312345/",
            "https://instagr.am/p/C312345/",
            "https://www.tiktok.com/@user/video/123456789",
            "https://vm.tiktok.com/ZMxxxx/",
            "https://youtube.com/shorts/abc123xyz",
            "https://youtu.be/abc123xyz",
            "https://m.youtube.com/shorts/abc123xyz",
        ]
        for u in valid_urls:
            valid, code, err = validate_url(u)
            self.assertTrue(valid, f"Failed for valid URL: {u}")
            self.assertEqual(code, "")
            self.assertEqual(err, "")

    def test_validate_url_negative_schemes(self):
        invalid_schemes = [
            "ftp://www.instagram.com/reel/123",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html,<html></html>",
            "www.instagram.com/reel/123",  # Missing scheme
        ]
        for u in invalid_schemes:
            valid, code, err = validate_url(u)
            self.assertFalse(valid, f"Should reject scheme: {u}")
            self.assertEqual(code, "INVALID_URL")
            self.assertTrue("HTTP(S)" in err or "Unsupported" in err)

    def test_validate_url_whitelist_bypass_attacks(self):
        bypasses = [
            "https://instagram.com.evil.com/reel/123",
            "https://evil-instagram.com/reel/123",
            "https://tiktok.com@evil.com/video/123",
            "https://not-youtube.com/shorts/123",
            "http://169.254.169.254/latest/meta-data/",
        ]
        for u in bypasses:
            valid, code, err = validate_url(u)
            self.assertFalse(valid, f"Bypass succeeded for {u}")
            self.assertEqual(code, "INVALID_URL")


class TestErrorCodes(unittest.TestCase):
    """Test every structured error code individually."""

    def test_code_invalid_url(self):
        valid, code, err = validate_url("https://unsupported-site.com/video")
        self.assertFalse(valid)
        self.assertEqual(code, "INVALID_URL")
        self.assertIn("not supported", err)

    @patch("subprocess.run")
    def test_code_needs_cookies(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "ERROR: [Instagram] Instagram sent an empty media response. Check if this post is accessible in your browser without being logged-in"
        mock_run.return_value = mock_proc

        meta, code, msg = fetch_metadata("https://www.instagram.com/reel/test/")
        self.assertIsNone(meta)
        self.assertEqual(code, "NEEDS_COOKIES")
        self.assertIn("cookies", msg)

    @patch("subprocess.run")
    def test_code_private_video(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "ERROR: [youtube] Video is private or not available"
        mock_run.return_value = mock_proc

        meta, code, msg = fetch_metadata("https://www.youtube.com/shorts/test")
        self.assertIsNone(meta)
        self.assertEqual(code, "PRIVATE_VIDEO")

    @patch("subprocess.run")
    def test_code_video_too_long(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"title": "Long Video", "duration": 500, "uploader": "creator"}'
        mock_run.return_value = mock_proc

        meta, code, msg = fetch_metadata("https://www.youtube.com/shorts/test")
        self.assertIsNone(meta)
        self.assertEqual(code, "VIDEO_TOO_LONG")
        self.assertIn("exceeds short-form limit", msg)

    @patch("subprocess.run")
    def test_code_extractor_broken(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "ERROR: Unable to extract video JSON data on current layout"
        mock_run.return_value = mock_proc

        meta, code, msg = fetch_metadata("https://www.tiktok.com/@user/video/1")
        self.assertIsNone(meta)
        self.assertEqual(code, "EXTRACTOR_BROKEN")
        self.assertIn("yt-dlp", msg)

    @patch("scripts.viewer.get_ffmpeg_path")
    def test_code_ffmpeg_missing(self, mock_ffmpeg):
        mock_ffmpeg.return_value = None
        from scripts.viewer import cmd_inspect
        import argparse

        args = argparse.Namespace(
            url="https://www.youtube.com/shorts/test12345",
            json=True,
            cookies=None,
            output=None,
            mode="standard",
            no_speech=False,
            max_frames=None,
            no_video=False,
            whisper_timeout=None,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = cmd_inspect(args)
            output = mock_out.getvalue()

        self.assertEqual(code, 1)
        data = json.loads(output)
        self.assertEqual(data.get("status"), "error")
        self.assertEqual(data.get("error_code"), "FFMPEG_MISSING")


class TestWhisperTimeout(unittest.TestCase):
    """Test process-isolated Whisper timeout handling."""

    def test_whisper_timeout_real_subprocess(self):
        tmp_audio = os.path.abspath("test_dummy_audio.mp3")
        with open(tmp_audio, "wb") as f:
            f.write(b"dummy")

        os.environ["AGENT_REELS_TEST_WORKER_SLEEP"] = "3"
        start_t = time.time()
        try:
            has_speech, segments, status, msg = transcribe_audio(tmp_audio, timeout_sec=1)
            elapsed = time.time() - start_t
            self.assertLess(elapsed, 2.5, "Subprocess was not killed within timeout limit")
            self.assertIsNone(has_speech)
            self.assertEqual(segments, [])
            self.assertEqual(status, "timeout")
            self.assertIn("WHISPER_TIMEOUT", msg)

            # Verify timeline formatting on timeout
            tmp_dir = os.path.abspath("test_timeout_timeline")
            os.makedirs(tmp_dir, exist_ok=True)
            try:
                t_path = assemble_timeline(tmp_dir, {"title": "Timeout Video"}, [], [], has_speech, msg, speech_status="timeout")
                with open(t_path, "r") as f:
                    c = f.read()
                    self.assertIn("Speech analysis timed out", c)
            finally:
                if os.path.exists(tmp_dir):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
        finally:
            os.environ.pop("AGENT_REELS_TEST_WORKER_SLEEP", None)
            if os.path.exists(tmp_audio):
                os.remove(tmp_audio)


class TestTimelineAssembly(unittest.TestCase):
    def setUp(self):
        self.test_dir = os.path.abspath("test_timeline_tmp")
        os.makedirs(self.test_dir, exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_format_timestamp(self):
        self.assertEqual(format_timestamp(0), "00:00")
        self.assertEqual(format_timestamp(9), "00:09")
        self.assertEqual(format_timestamp(65), "01:05")
        self.assertEqual(format_timestamp(125.4), "02:05")

    def test_timeline_speech_status_ok(self):
        meta = {"title": "Speech Video", "uploader": "speaker", "webpage_url": "https://youtube.com/shorts/1", "duration": 30}
        path = assemble_timeline(self.test_dir, meta, [], [{"start": 1.0, "end": 3.0, "text": "Hello"}], True, "ok", speech_status="ok")
        with open(path, "r") as f:
            c = f.read()
            self.assertIn("Spoken dialogue present", c)
            self.assertIn("Hello", c)

    def test_timeline_speech_status_none(self):
        meta = {"title": "Music Video", "uploader": "dj", "webpage_url": "https://tiktok.com/@dj/1", "duration": 20}
        path = assemble_timeline(self.test_dir, meta, [], [], False, "no speech", speech_status="none")
        with open(path, "r") as f:
            c = f.read()
            self.assertIn("No spoken speech detected (music/visual only)", c)
            self.assertIn("No spoken transcript available", c)

    def test_timeline_speech_status_skipped(self):
        meta = {"title": "Skipped Video", "uploader": "user", "webpage_url": "https://instagram.com/reel/1", "duration": 15}
        path = assemble_timeline(self.test_dir, meta, [], [], None, "skipped", speech_status="skipped")
        with open(path, "r") as f:
            c = f.read()
            self.assertIn("Speech analysis skipped (--no-speech)", c)
            self.assertIn("Speech analysis skipped by user flag", c)

    def test_timeline_speech_status_timeout(self):
        meta = {"title": "Timeout Video", "uploader": "user", "webpage_url": "https://youtube.com/shorts/2", "duration": 60}
        path = assemble_timeline(self.test_dir, meta, [], [], None, "timeout", speech_status="timeout")
        with open(path, "r") as f:
            c = f.read()
            self.assertIn("Speech analysis timed out", c)

    def test_timeline_speech_status_error(self):
        meta = {"title": "Error Video", "uploader": "user", "webpage_url": "https://youtube.com/shorts/3", "duration": 15}
        path = assemble_timeline(self.test_dir, meta, [], [], None, "module not found", speech_status="error")
        with open(path, "r") as f:
            c = f.read()
            self.assertIn("Speech analysis unavailable", c)
            self.assertIn("install faster-whisper", c)
            self.assertNotIn("No spoken speech detected", c)


class TestRealKeyframeCutsAndPTS(unittest.TestCase):
    """Test actual scene change detection and PTS timestamps against synthetic video cuts."""

    def setUp(self):
        self.test_dir = os.path.abspath("test_cuts_tmp")
        os.makedirs(self.test_dir, exist_ok=True)
        self.video_path = os.path.join(self.test_dir, "cuts_fixture.mp4")

        ffmpeg = get_ffmpeg_path()
        if ffmpeg:
            # 3 distinct color cuts: Black (0-2s), White (2-4.5s), Black (4.5-6.5s)
            cmd = [
                ffmpeg, "-y",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:d=2.0",
                "-f", "lavfi", "-i", "color=c=white:s=320x240:d=2.5",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:d=2.0",
                "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[outv]",
                "-map", "[outv]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                self.video_path,
            ]
            subprocess.run(cmd, capture_output=True)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_known_scene_cuts_match_pts(self):
        if not os.path.exists(self.video_path):
            self.skipTest("FFmpeg not available to generate cuts fixture")

        frames = extract_keyframes(self.video_path, self.test_dir, max_frames=10, has_speech=False)
        pts_list = [ts for ts, f in frames]

        # Verify initial frame at 0.0s
        self.assertAlmostEqual(pts_list[0], 0.0, delta=0.2)

        # Check cut around 2.0s (within 0.3s tolerance)
        cut1_found = any(abs(ts - 2.0) <= 0.3 for ts in pts_list)
        self.assertTrue(cut1_found, f"Did not find cut near 2.0s in PTS list: {pts_list}")

        # Check cut around 4.5s (within 0.3s tolerance)
        cut2_found = any(abs(ts - 4.5) <= 0.3 for ts in pts_list)
        self.assertTrue(cut2_found, f"Did not find cut near 4.5s in PTS list: {pts_list}")

    def test_max_frames_one(self):
        if not os.path.exists(self.video_path):
            self.skipTest("FFmpeg fixture missing")
        frames = extract_keyframes(self.video_path, self.test_dir, max_frames=1)
        self.assertEqual(len(frames), 1)


class TestKeyframeAdaptiveDensity(unittest.TestCase):
    def setUp(self):
        self.test_dir = os.path.abspath("test_keyframes_tmp")
        os.makedirs(self.test_dir, exist_ok=True)
        self.video_path = os.path.join(self.test_dir, "fixture.mp4")

        ffmpeg = get_ffmpeg_path()
        if ffmpeg:
            cmd = [
                ffmpeg, "-y",
                "-f", "lavfi", "-i", "testsrc=duration=6:size=360x640:rate=15",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                self.video_path,
            ]
            subprocess.run(cmd, capture_output=True)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_adaptive_sampling_density(self):
        if not os.path.exists(self.video_path):
            self.skipTest("FFmpeg not available for fixture generation")

        # Silent mode: step 1.5s -> extracts more frames
        dir_silent = os.path.join(self.test_dir, "silent")
        frames_silent = extract_keyframes(self.video_path, dir_silent, has_speech=False)
        self.assertGreaterEqual(len(frames_silent), 3)

        # Speech mode: step 3.0s -> extracts fewer frames
        dir_speech = os.path.join(self.test_dir, "speech")
        frames_speech = extract_keyframes(self.video_path, dir_speech, has_speech=True)
        self.assertLess(len(frames_speech), len(frames_silent))


class TestLimitsAndConstants(unittest.TestCase):
    def test_max_duration(self):
        self.assertLessEqual(MAX_DURATION_SECONDS, 600)
        self.assertGreaterEqual(MAX_DURATION_SECONDS, 60)


if __name__ == "__main__":
    unittest.main()
