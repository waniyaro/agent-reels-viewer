"""Comprehensive unit and integration test suite for agent-reels-viewer."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from agent_reels_viewer.audio import extract_audio, transcribe_audio
from agent_reels_viewer.cli import auto_clean_old_sessions, cmd_inspect
from agent_reels_viewer.downloader import (
    MAX_DURATION_SECONDS,
    detect_platform,
    fetch_metadata,
    validate_url,
)
from agent_reels_viewer.timeline import assemble_timeline, format_timestamp
from agent_reels_viewer.video import (
    extract_keyframes,
    extract_range_frames,
    get_ffmpeg_path,
    get_ffmpeg_version,
    has_audio_stream,
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


class TestErrorCodesAndPreflights(unittest.TestCase):
    """Test structured error codes and preflights."""

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

    @patch("agent_reels_viewer.cli.get_ffmpeg_path")
    def test_code_ffmpeg_missing(self, mock_ffmpeg):
        mock_ffmpeg.return_value = None
        import argparse

        args = argparse.Namespace(
            url="https://www.youtube.com/shorts/test12345",
            json=True,
            cookies=None,
            output=None,
            mode="standard",
            model="base",
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


class TestLocalFileInspection(unittest.TestCase):
    """Test inspecting local video files directly without downloading."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.local_mp4 = os.path.join(self.temp_dir, "test_clip.mp4")
        ffmpeg = get_ffmpeg_path()
        if ffmpeg:
            cmd = [
                ffmpeg, "-y",
                "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                self.local_mp4,
            ]
            subprocess.run(cmd, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_inspect_local_file(self):
        if not os.path.exists(self.local_mp4):
            self.skipTest("FFmpeg not available to generate local fixture")

        import argparse
        out_session = os.path.join(self.temp_dir, "session_out")
        args = argparse.Namespace(
            url=self.local_mp4,
            json=True,
            cookies=None,
            output=out_session,
            mode="standard",
            model="base",
            no_speech=True,
            max_frames=5,
            no_video=False,
            whisper_timeout=None,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            exit_code = cmd_inspect(args)
            stdout_str = mock_out.getvalue()

        self.assertEqual(exit_code, 0)
        data = json.loads(stdout_str)
        self.assertEqual(data.get("status"), "success")
        self.assertEqual(data.get("platform"), "local")
        self.assertEqual(data.get("video_path"), self.local_mp4)
        self.assertTrue(os.path.exists(data.get("timeline_path")))


class TestSafeAutoClean(unittest.TestCase):
    """Test that auto-cleaning ONLY cleans the base cache root, never user custom output dirs."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.foreign_session = os.path.join(self.temp_dir, "session_user_data")
        os.makedirs(self.foreign_session, exist_ok=True)
        # Create a mock file inside foreign_session
        with open(os.path.join(self.foreign_session, "notes.txt"), "w") as f:
            f.write("important notes")

        # Fake old timestamp (48 hours ago)
        old_time = time.time() - (48 * 3600)
        os.utime(self.foreign_session, (old_time, old_time))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_auto_clean_only_cache_root(self):
        # Attempt to auto-clean pointing to user's temp directory
        auto_clean_old_sessions(self.temp_dir, max_age_hours=24)
        # Verify the user directory session was NOT deleted
        self.assertTrue(
            os.path.exists(self.foreign_session),
            "auto_clean_old_sessions unsafely deleted a directory outside get_base_cache_dir()!"
        )


class TestAudioAndSilentHandling(unittest.TestCase):
    """Test audio extraction: distinguishing silent videos from extraction errors."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.silent_video = os.path.join(self.temp_dir, "silent.mp4")
        ffmpeg = get_ffmpeg_path()
        if ffmpeg:
            cmd = [
                ffmpeg, "-y",
                "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                self.silent_video,
            ]
            subprocess.run(cmd, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_silent_video_reports_no_audio_stream(self):
        if not os.path.exists(self.silent_video):
            self.skipTest("FFmpeg not available")

        self.assertFalse(has_audio_stream(self.silent_video))
        audio_path, err_code, err_msg = extract_audio(self.silent_video, self.temp_dir)
        self.assertIsNone(audio_path)
        self.assertEqual(err_code, "NO_AUDIO_STREAM")

    @patch("agent_reels_viewer.audio.has_audio_stream")
    @patch("subprocess.run")
    def test_audio_extraction_failure_reports_error(self, mock_run, mock_has_stream):
        mock_has_stream.return_value = True
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_run.return_value = mock_proc

        audio_path, err_code, err_msg = extract_audio("/dummy/video.mp4", self.temp_dir)
        self.assertIsNone(audio_path)
        self.assertEqual(err_code, "AUDIO_EXTRACTION_FAILED")


class TestWhisperTimeout(unittest.TestCase):
    """Test process-isolated Whisper timeout handling without test hooks in production code."""

    def test_whisper_timeout_real_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create a mock worker script that intentionally sleeps longer than timeout
            mock_worker = os.path.join(tmp_dir, "sleeping_worker.py")
            with open(mock_worker, "w") as f:
                f.write("import time, sys\ntime.sleep(4)\nsys.exit(0)\n")

            tmp_audio = os.path.join(tmp_dir, "test.mp3")
            with open(tmp_audio, "wb") as f:
                f.write(b"dummy")

            start_t = time.time()
            has_speech, segments, status, msg = transcribe_audio(
                tmp_audio,
                timeout_sec=1,
                worker_script=mock_worker,
            )
            elapsed = time.time() - start_t
            self.assertLess(elapsed, 2.5, "Subprocess was not killed within timeout limit")
            self.assertIsNone(has_speech)
            self.assertEqual(segments, [])
            self.assertEqual(status, "timeout")
            self.assertIn("WHISPER_TIMEOUT", msg)
            self.assertIn("--model tiny", msg)


class TestTimelineAssembly(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_format_timestamp(self):
        self.assertEqual(format_timestamp(0), "00:00")
        self.assertEqual(format_timestamp(9), "00:09")
        self.assertEqual(format_timestamp(65), "01:05")
        self.assertEqual(format_timestamp(125.4), "02:05")

    def test_timeline_speech_status_ok(self):
        meta = {"title": "Speech Video", "uploader": "speaker", "webpage_url": "https://youtube.com/shorts/1", "duration": 30}
        path = assemble_timeline(self.temp_dir, meta, [], [{"start": 1.0, "end": 3.0, "text": "Hello"}], True, "ok", speech_status="ok")
        with open(path, "r") as f:
            c = f.read()
            self.assertIn("Spoken dialogue present", c)
            self.assertIn("Hello", c)

    def test_timeline_speech_status_none(self):
        meta = {"title": "Music Video", "uploader": "dj", "webpage_url": "https://tiktok.com/@dj/1", "duration": 20}
        path = assemble_timeline(self.temp_dir, meta, [], [], False, "no speech", speech_status="none")
        with open(path, "r") as f:
            c = f.read()
            self.assertIn("No spoken speech detected (music/visual only)", c)
            self.assertIn("No spoken transcript available", c)

    def test_timeline_speech_status_skipped(self):
        meta = {"title": "Skipped Video", "uploader": "user", "webpage_url": "https://instagram.com/reel/1", "duration": 15}
        path = assemble_timeline(self.temp_dir, meta, [], [], None, "skipped", speech_status="skipped")
        with open(path, "r") as f:
            c = f.read()
            self.assertIn("Speech analysis skipped (--no-speech)", c)
            self.assertIn("Speech analysis skipped by user flag", c)

    def test_timeline_speech_status_timeout(self):
        meta = {"title": "Timeout Video", "uploader": "user", "webpage_url": "https://youtube.com/shorts/2", "duration": 60}
        path = assemble_timeline(self.temp_dir, meta, [], [], None, "timeout", speech_status="timeout")
        with open(path, "r") as f:
            c = f.read()
            self.assertIn("Speech analysis timed out", c)

    def test_timeline_speech_status_error(self):
        meta = {"title": "Error Video", "uploader": "user", "webpage_url": "https://youtube.com/shorts/3", "duration": 15}
        path = assemble_timeline(self.temp_dir, meta, [], [], None, "module not found", speech_status="error")
        with open(path, "r") as f:
            c = f.read()
            self.assertIn("Speech analysis unavailable", c)
            self.assertIn("install faster-whisper", c)
            self.assertNotIn("No spoken speech detected", c)


class TestRealKeyframeCutsAndPTS(unittest.TestCase):
    """Test actual scene change detection and PTS timestamps against synthetic video cuts."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.video_path = os.path.join(self.temp_dir, "cuts_fixture.mp4")

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
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_known_scene_cuts_match_pts(self):
        if not os.path.exists(self.video_path):
            self.skipTest("FFmpeg not available to generate cuts fixture")

        frames, ts_type, err_code, err_msg = extract_keyframes(self.video_path, self.temp_dir, max_frames=10, has_speech=False)
        self.assertEqual(err_code, "")
        self.assertEqual(ts_type, "exact")
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
        frames, ts_type, err_code, err_msg = extract_keyframes(self.video_path, self.temp_dir, max_frames=1)
        self.assertEqual(len(frames), 1)
        self.assertEqual(err_code, "")

    @patch("subprocess.run")
    def test_frame_extraction_failure_reports_code(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "Error initializing complex filter"
        mock_run.return_value = mock_proc

        frames, ts_type, err_code, err_msg = extract_keyframes(self.video_path, self.temp_dir)
        self.assertEqual(frames, [])
        self.assertEqual(err_code, "FRAME_EXTRACTION_FAILED")
        self.assertIn("FFmpeg error", err_msg)


class TestKeyframeAdaptiveDensity(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.video_path = os.path.join(self.temp_dir, "fixture.mp4")

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
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_adaptive_sampling_density(self):
        if not os.path.exists(self.video_path):
            self.skipTest("FFmpeg not available for fixture generation")

        # Silent mode: step 1.5s -> extracts more frames
        dir_silent = os.path.join(self.temp_dir, "silent")
        frames_silent, _, _, _ = extract_keyframes(self.video_path, dir_silent, has_speech=False)
        self.assertGreaterEqual(len(frames_silent), 3)

        # Speech mode: step 3.0s -> extracts fewer frames
        dir_speech = os.path.join(self.temp_dir, "speech")
        frames_speech, _, _, _ = extract_keyframes(self.video_path, dir_speech, has_speech=True)
        self.assertLess(len(frames_speech), len(frames_silent))


class TestLimitsAndConstants(unittest.TestCase):
    def test_max_duration(self):
        self.assertLessEqual(MAX_DURATION_SECONDS, 600)
        self.assertGreaterEqual(MAX_DURATION_SECONDS, 60)

    def test_ffmpeg_version_detection(self):
        major, minor = get_ffmpeg_version()
        self.assertGreaterEqual(major, 1)


if __name__ == "__main__":
    unittest.main()
