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
from agent_reels_viewer.cli import auto_clean_old_sessions, cmd_frames, cmd_inspect
from agent_reels_viewer.downloader import (
    MAX_DURATION_SECONDS,
    detect_platform,
    fetch_metadata,
    validate_url,
)
from agent_reels_viewer.timeline import assemble_timeline, format_timestamp
from agent_reels_viewer.video import (
    deduplicate_frames,
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

        # Check meta.json contains source_path
        meta_file = os.path.join(out_session, "meta.json")
        self.assertTrue(os.path.exists(meta_file))
        with open(meta_file, "r", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta.get("source_path"), self.local_mp4)

    def test_local_file_not_found_returns_error_code(self):
        import argparse
        args = argparse.Namespace(
            url=os.path.join(self.temp_dir, "non_existent_clip.mp4"),
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
            exit_code = cmd_inspect(args)
            stdout_str = mock_out.getvalue()

        self.assertEqual(exit_code, 1)
        data = json.loads(stdout_str)
        self.assertEqual(data.get("status"), "error")
        self.assertEqual(data.get("error_code"), "LOCAL_FILE_NOT_FOUND")


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
                f.write("import time, sys\ntime.sleep(30)\nsys.exit(0)\n")

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
            self.assertLess(elapsed, 10.0, "Subprocess was not killed within timeout limit")
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

    def test_timeline_cross_drive_windows_mount(self):
        """Verify assemble_timeline handles cross-drive mounts on Windows without ValueError."""
        with patch("os.path.relpath", side_effect=ValueError("path is on mount 'D:', start on mount 'C:'")):
            meta = {"uploader": "test", "duration": 5.0}
            keyframes = [(1.0, "D:\\workspace\\frame_01.jpg")]
            t_path = assemble_timeline(
                output_dir=self.temp_dir,
                meta=meta,
                keyframes=keyframes,
                speech_segments=[],
                has_speech=False,
                transcription_status="None",
                speech_status="none",
            )
            with open(t_path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("D:/workspace/frame_01.jpg", content)

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


class TestPixelDifferenceDeduplication(unittest.TestCase):
    """Test downscaled grayscale tiled block deduplication behavior."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_deduplicate_fixture_identical_vs_text_change(self):
        """Fixture test: fine 14px text edit preserved; identical frames with JPEG noise dropped."""
        from PIL import Image, ImageDraw

        f1_path = os.path.join(self.temp_dir, "frame_01.jpg")
        im_base = Image.new("RGB", (768, 432), color=(30, 30, 30))
        draw_base = ImageDraw.Draw(im_base)
        draw_base.text((40, 40), "def my_function():", fill=(220, 220, 220))
        draw_base.text((60, 65), "val = compute_result(", fill=(220, 220, 220))
        im_base.save(f1_path, "JPEG", quality=85)

        # f2 is an identical frame saved with different JPEG compression (quality 60)
        f2_path = os.path.join(self.temp_dir, "frame_02.jpg")
        im_base.save(f2_path, "JPEG", quality=60)

        # f3 has a single small character 'x' (~14px) added at the cursor
        f3_path = os.path.join(self.temp_dir, "frame_03.jpg")
        im_edit = im_base.copy()
        draw_edit = ImageDraw.Draw(im_edit)
        draw_edit.text((220, 65), "x", fill=(255, 255, 255))
        im_edit.save(f3_path, "JPEG", quality=85)

        # 1. Identical frames with JPEG noise: duplicate is dropped
        kept_identical = deduplicate_frames([(0.0, f1_path), (0.5, f2_path)], is_dense_mode=True)
        self.assertEqual(len(kept_identical), 1)
        self.assertEqual(kept_identical[0][1], f1_path)

        # 2. Single 14px small-font character edit: both frames are preserved
        kept_edit = deduplicate_frames([(1.0, f1_path), (1.5, f3_path)], is_dense_mode=True)
        self.assertEqual(len(kept_edit), 2)
        self.assertEqual(kept_edit[0][1], f1_path)
        self.assertEqual(kept_edit[1][1], f3_path)

    def test_speech_mode_preserves_subtitles(self):
        """In conversational mode (speech present), subtitle changes at bottom of screen must not be dropped."""
        from PIL import Image, ImageDraw

        f1_path = os.path.join(self.temp_dir, "sub_01.jpg")
        im1 = Image.new("RGB", (768, 432), color=(20, 20, 20))
        draw1 = ImageDraw.Draw(im1)
        draw1.text((200, 380), "First spoken subtitle sentence.", fill=(255, 255, 255))
        im1.save(f1_path, "JPEG")

        f2_path = os.path.join(self.temp_dir, "sub_02.jpg")
        im2 = Image.new("RGB", (768, 432), color=(20, 20, 20))
        draw2 = ImageDraw.Draw(im2)
        draw2.text((200, 380), "Second spoken subtitle sentence.", fill=(255, 255, 255))
        im2.save(f2_path, "JPEG")

        kept = deduplicate_frames([(0.0, f1_path), (3.5, f2_path)], is_dense_mode=False)
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[0][1], f1_path)
        self.assertEqual(kept[1][1], f2_path)


class TestCmdFrames(unittest.TestCase):
    """Test cmd_frames for downloaded sessions and local file sessions."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.video_path = os.path.join(self.temp_dir, "test_clip.mp4")
        ffmpeg = get_ffmpeg_path()
        if ffmpeg:
            cmd = [
                ffmpeg, "-y",
                "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=10",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                self.video_path,
            ]
            subprocess.run(cmd, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_frames_downloaded_session(self):
        """Case 1: video.mp4 is directly inside the session folder."""
        if not os.path.exists(self.video_path):
            self.skipTest("FFmpeg fixture missing")

        session_dir = os.path.join(self.temp_dir, "session_downloaded")
        os.makedirs(session_dir, exist_ok=True)
        shutil.copy(self.video_path, os.path.join(session_dir, "video.mp4"))

        import argparse
        args = argparse.Namespace(
            target=session_dir,
            from_sec=1.0,
            to_sec=3.0,
            count=3,
            hires=False,
            output=None,
            json=True,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = cmd_frames(args)
            output = mock_out.getvalue()

        self.assertEqual(code, 0)
        data = json.loads(output)
        self.assertEqual(data.get("status"), "success")
        self.assertEqual(data.get("frames_extracted"), 3)

    def test_frames_local_file_session(self):
        """Case 2: video is external; session folder only contains meta.json with source_path."""
        if not os.path.exists(self.video_path):
            self.skipTest("FFmpeg fixture missing")

        session_dir = os.path.join(self.temp_dir, "session_local")
        os.makedirs(session_dir, exist_ok=True)
        meta = {
            "id": "local_abc",
            "title": "test_clip.mp4",
            "source_path": self.video_path,
            "duration": 4.0,
        }
        with open(os.path.join(session_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)

        import argparse
        args = argparse.Namespace(
            target=session_dir,
            from_sec=1.0,
            to_sec=3.0,
            count=3,
            hires=False,
            output=None,
            json=True,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = cmd_frames(args)
            output = mock_out.getvalue()

        self.assertEqual(code, 0)
        data = json.loads(output)
        self.assertEqual(data.get("status"), "success")
        self.assertEqual(data.get("frames_extracted"), 3)

    def test_frames_video_not_found(self):
        """Case 3: Session folder has no video and no valid source_path -> VIDEO_NOT_FOUND."""
        session_dir = os.path.join(self.temp_dir, "empty_session")
        os.makedirs(session_dir, exist_ok=True)

        import argparse
        args = argparse.Namespace(
            target=session_dir,
            from_sec=1.0,
            to_sec=3.0,
            count=3,
            hires=False,
            output=None,
            json=True,
        )
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            code = cmd_frames(args)

        self.assertEqual(code, 1)

    def test_frames_consecutive_calls_no_contamination(self):
        """Two consecutive calls on same session with different ranges return only their own frames."""
        if not os.path.exists(self.video_path):
            self.skipTest("FFmpeg fixture missing")

        session_dir = os.path.join(self.temp_dir, "session_consecutive")
        os.makedirs(session_dir, exist_ok=True)
        shutil.copy(self.video_path, os.path.join(session_dir, "video.mp4"))

        import argparse
        # First call: 0.5s to 1.5s (count 2)
        args1 = argparse.Namespace(
            target=session_dir,
            from_sec=0.5,
            to_sec=1.5,
            count=2,
            hires=False,
            output=None,
            json=True,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out1:
            code1 = cmd_frames(args1)
            data1 = json.loads(mock_out1.getvalue())

        self.assertEqual(code1, 0)
        self.assertEqual(data1.get("frames_extracted"), 2)
        frames1 = data1.get("frames", [])
        self.assertTrue(all("0.5-1.5" in f for f in frames1))

        # Second call: 2.0s to 3.0s (count 2)
        args2 = argparse.Namespace(
            target=session_dir,
            from_sec=2.0,
            to_sec=3.0,
            count=2,
            hires=False,
            output=None,
            json=True,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out2:
            code2 = cmd_frames(args2)
            data2 = json.loads(mock_out2.getvalue())

        self.assertEqual(code2, 0)
        self.assertEqual(data2.get("frames_extracted"), 2)
        frames2 = data2.get("frames", [])
        self.assertTrue(all("2.0-3.0" in f for f in frames2))
        # Ensure no overlap/contamination from call 1
        self.assertFalse(any(f in frames1 for f in frames2))

        # Repeat call on same range: does not crash
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out3:
            code3 = cmd_frames(args2)
            data3 = json.loads(mock_out3.getvalue())
        self.assertEqual(code3, 0)
        self.assertEqual(data3.get("frames_extracted"), 2)

    def test_frames_invalid_range_errors(self):
        """Verify INVALID_RANGE error code for invalid or out-of-bounds time intervals."""
        if not os.path.exists(self.video_path):
            self.skipTest("FFmpeg fixture missing")

        import argparse
        # Case A: from_sec >= to_sec
        args_inverted = argparse.Namespace(
            target=self.video_path,
            from_sec=3.0,
            to_sec=1.0,
            count=2,
            hires=False,
            output=None,
            json=True,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = cmd_frames(args_inverted)
            data = json.loads(mock_out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(data.get("error_code"), "INVALID_RANGE")

        # Case B: from_sec exceeds video duration (4.0s)
        args_oob = argparse.Namespace(
            target=self.video_path,
            from_sec=10.0,
            to_sec=12.0,
            count=2,
            hires=False,
            output=None,
            json=True,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = cmd_frames(args_oob)
            data = json.loads(mock_out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(data.get("error_code"), "INVALID_RANGE")


class TestLimitsAndConstants(unittest.TestCase):
    def test_max_duration(self):
        self.assertLessEqual(MAX_DURATION_SECONDS, 600)
        self.assertGreaterEqual(MAX_DURATION_SECONDS, 60)

    def test_ffmpeg_version_detection(self):
        major, minor = get_ffmpeg_version()
        self.assertGreaterEqual(major, 1)



class TestHardeningRoundTwelve(unittest.TestCase):
    """Tests for file cleanup, TTL by file mtime, token estimation, timeline deduplication, and modes."""

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp(prefix="test_round12_")
        cls.fixture_video = os.path.join(cls.test_dir, "test_fixture.mp4")
        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            raise unittest.SkipTest("ffmpeg binary not available in environment")
        cmd = [
            ffmpeg, "-y", "-f", "lavfi", "-i",
            "testsrc=duration=5:size=320x240:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=5",
            "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p",
            cls.fixture_video
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def test_audio_mp3_deleted_in_finally(self):
        """Verify audio.mp3 is deleted immediately after transcription even if transcribe fails or succeeds."""
        session_dir = tempfile.mkdtemp(prefix="session_audio_del_")
        try:
            import argparse
            args = argparse.Namespace(
                url=self.fixture_video,
                json=True,
                output=session_dir,
                no_speech=False,
                mode="standard",
                model="tiny",
                whisper_timeout=10,
                cookies=None,
                max_frames=4,
                no_video=False,
                debug_frames=False,
            )
            # Run cmd_inspect with mock transcribe
            with patch("agent_reels_viewer.cli.transcribe_audio") as mock_trans:
                mock_trans.return_value = (True, [{"start": 0.0, "end": 1.0, "text": "hello"}], "ok", "transcribed")
                with patch("sys.stdout", new_callable=io.StringIO):
                    cmd_inspect(args)

            audio_path = os.path.join(session_dir, "audio.mp3")
            self.assertFalse(os.path.exists(audio_path), "audio.mp3 must be deleted in finally block")

            # Also verify deletion when transcribe raises an exception
            with patch("agent_reels_viewer.cli.transcribe_audio", side_effect=RuntimeError("transcribe crash")):
                with patch("sys.stdout", new_callable=io.StringIO):
                    try:
                        cmd_inspect(args)
                    except RuntimeError:
                        pass
            self.assertFalse(os.path.exists(audio_path), "audio.mp3 must be deleted even if transcribe raises an exception")
        finally:
            shutil.rmtree(session_dir, ignore_errors=True)

    def test_session_cleanup_by_file_mtime_and_custom_ttl(self):
        """Verify TTL cleanup uses newest file mtime and respects AGENT_REELS_TTL_HOURS and clean --days 0."""
        from agent_reels_viewer.cli import get_session_latest_mtime, auto_clean_old_sessions, cmd_clean
        cache_dir = tempfile.mkdtemp(prefix="test_cache_ttl_")
        try:
            s_old = os.path.join(cache_dir, "session_old")
            s_fresh = os.path.join(cache_dir, "session_fresh")
            os.makedirs(s_old)
            os.makedirs(s_fresh)

            f_old = os.path.join(s_old, "video.mp4")
            with open(f_old, "w") as f:
                f.write("old data")

            f_fresh = os.path.join(s_fresh, "video.mp4")
            with open(f_fresh, "w") as f:
                f.write("fresh data")

            now = time.time()
            old_time = now - (30 * 3600)  # 30 hours ago
            fresh_time = now - (2 * 3600)  # 2 hours ago

            os.utime(f_old, (old_time, old_time))
            os.utime(f_fresh, (fresh_time, fresh_time))

            # Check get_session_latest_mtime inspects files inside
            self.assertAlmostEqual(get_session_latest_mtime(s_old), old_time, delta=2.0)
            self.assertAlmostEqual(get_session_latest_mtime(s_fresh), fresh_time, delta=2.0)

            # auto_clean_old_sessions with TTL=24 hours should prune s_old and keep s_fresh
            with patch("agent_reels_viewer.cli.get_base_cache_dir", return_value=cache_dir):
                with patch.dict(os.environ, {"AGENT_REELS_TTL_HOURS": "24"}):
                    auto_clean_old_sessions(cache_dir)
            self.assertFalse(os.path.exists(s_old), "Session with files older than 24h must be cleaned")
            self.assertTrue(os.path.exists(s_fresh), "Session with recent files must be preserved")

            # cmd_clean with days=0 should remove s_fresh immediately
            import argparse
            args_clean = argparse.Namespace(days=0)
            with patch("agent_reels_viewer.cli.get_base_cache_dir", return_value=cache_dir):
                with patch("sys.stdout", new_callable=io.StringIO):
                    cmd_clean(args_clean)
            self.assertFalse(os.path.exists(s_fresh), "clean --days 0 must remove all sessions")
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)

    def test_estimated_image_tokens_in_json_and_meta(self):
        """Verify estimated_image_tokens and frames_total are present in JSON and meta.json."""
        session_dir = tempfile.mkdtemp(prefix="session_tokens_")
        try:
            import argparse
            args = argparse.Namespace(
                url=self.fixture_video,
                json=True,
                output=session_dir,
                no_speech=True,
                mode="standard",
                model="tiny",
                whisper_timeout=10,
                cookies=None,
                max_frames=3,
                no_video=False,
                debug_frames=False,
            )
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                cmd_inspect(args)
                data = json.loads(mock_out.getvalue())

            self.assertIn("estimated_image_tokens", data)
            self.assertIn("frames_total", data)
            self.assertGreater(data["estimated_image_tokens"], 0)
            self.assertEqual(data["frames_total"], data["frames_extracted"])

            meta_file = os.path.join(session_dir, "meta.json")
            self.assertTrue(os.path.exists(meta_file))
            with open(meta_file, encoding="utf-8") as f:
                meta_data = json.load(f)
            self.assertIn("estimated_image_tokens", meta_data)
            self.assertIn("frames_total", meta_data)
            self.assertEqual(meta_data["estimated_image_tokens"], data["estimated_image_tokens"])
        finally:
            shutil.rmtree(session_dir, ignore_errors=True)

    def test_timeline_no_duplicate_transcript(self):
        """Verify speech transcript is in timeline table, and ## Full Transcript section is removed."""
        t_path = os.path.join(self.test_dir, "test_timeline.md")
        segments = [
            {"start": 1.0, "end": 2.5, "text": "Testing speech transcript line"}
        ]
        meta = {"uploader": "test_creator", "duration": 5.0}
        keyframes = [(1.5, os.path.join(self.test_dir, "frame_01.jpg"))]
        t_path = assemble_timeline(
            output_dir=self.test_dir,
            meta=meta,
            keyframes=keyframes,
            speech_segments=segments,
            has_speech=True,
            transcription_status="Transcribed",
            speech_status="ok",
        )
        with open(t_path, encoding="utf-8") as f:
            content = f.read()

        self.assertIn("Testing speech transcript line", content)
        self.assertNotIn("## Full Transcript", content, "Redundant Full Transcript section must be removed to save tokens")

    def test_modes_standard_vs_deep(self):
        """Verify standard mode vs deep mode cadence and candidate generation."""
        frames_std, ts_std, _, _, stats_std = extract_keyframes(
            self.fixture_video,
            os.path.join(self.test_dir, "out_std"),
            has_speech=False,
            mode="standard",
            return_stats=True,
        )
        frames_deep, ts_deep, _, _, stats_deep = extract_keyframes(
            self.fixture_video,
            os.path.join(self.test_dir, "out_deep"),
            has_speech=False,
            mode="deep",
            return_stats=True,
        )
        self.assertEqual(stats_std["mode"], "standard")
        self.assertEqual(stats_deep["mode"], "deep")
        self.assertLess(stats_deep["step_sec"], stats_std["step_sec"])
        self.assertLessEqual(stats_deep["threshold"], stats_std["threshold"])

    def test_cap_preserves_most_significant_transitions(self):
        """Verify that when 17 synthetic candidates exceed cap, significant transitions are kept,
        close pairs are deduplicated/spaced, and temporal intervals are covered."""
        from agent_reels_viewer.video import select_capped_frames
        from PIL import Image

        fixture_dir = tempfile.mkdtemp(prefix="test_cap_17_")
        try:
            # 17 synthetic frames across 100 seconds:
            # - Significant transitions:
            #   t=25.0 (white -> black)
            #   t=45.0 (black -> red)
            #   t=60.0 (red -> blue)
            # - Pair of close frames:
            #   t=75.0 and t=75.13 (difference 0.13s, both blue)
            # - Uniform long intervals: [0..25], [26..45], [46..60], [61..75], [75.13..85], [85..100]
            timestamps = [
                0.0, 5.0, 10.0,
                25.0, 26.0,
                45.0, 46.0,
                60.0, 61.0,
                75.0, 75.13,
                85.0, 90.0, 92.0, 95.0, 98.0, 100.0
            ]
            frames_with_pts = []
            for i, ts in enumerate(timestamps):
                fpath = os.path.join(fixture_dir, f"frame_{i:02d}_{ts:.2f}s.jpg")
                if ts < 25.0:
                    color = (255, 255, 255)
                elif ts < 45.0:
                    color = (0, 0, 0)
                elif ts < 60.0:
                    color = (255, 0, 0)
                elif i == 10:
                    color = (0, 0, 250)
                else:
                    color = (0, 0, 255)
                img = Image.new("RGB", (100, 100), color)
                img.save(fpath, quality=90)
                frames_with_pts.append((ts, fpath))

            # Cap 17 frames down to 8 frames across 100.0s duration
            cap = 8
            duration = 100.0
            selected = select_capped_frames(frames_with_pts, effective_max=cap, duration=duration)
            self.assertEqual(len(selected), cap)

            selected_pts = [round(ts, 2) for ts, _ in selected]

            # 1. First and last frames must always be preserved
            self.assertIn(0.0, selected_pts)
            self.assertIn(100.0, selected_pts)

            # 2. Significant visual transitions must be kept
            self.assertIn(25.0, selected_pts, f"Significant shift at 25.0s missing from {selected_pts}")
            self.assertIn(45.0, selected_pts, f"Significant shift at 45.0s missing from {selected_pts}")
            self.assertIn(60.0, selected_pts, f"Significant shift at 60.0s missing from {selected_pts}")

            # 3. From the close pair (75.0s and 75.13s, diff 0.13s), exactly ONE must be kept
            has_75_0 = 75.0 in selected_pts
            has_75_13 = 75.13 in selected_pts
            self.assertTrue(
                (has_75_0 and not has_75_13) or (has_75_13 and not has_75_0),
                f"Close pair 75.0 and 75.13 must not both be kept: {selected_pts}"
            )

            # 4. Temporal intervals must be covered: no huge gap across the 100s timeline
            gaps = [selected_pts[i+1] - selected_pts[i] for i in range(len(selected_pts) - 1)]
            for gap in gaps:
                self.assertLessEqual(gap, 30.0, f"Temporal gap {gap}s too large in {selected_pts}")
        finally:
            shutil.rmtree(fixture_dir, ignore_errors=True)

    def test_fresh_download_mtime_not_pruned_by_ttl(self):
        """Verify freshly downloaded file with current mtime is not pruned by auto_clean_old_sessions."""
        from agent_reels_viewer.cli import auto_clean_old_sessions, get_session_latest_mtime
        cache_dir = tempfile.mkdtemp(prefix="test_fresh_ttl_")
        try:
            session_dir = os.path.join(cache_dir, "session_fresh_download")
            os.makedirs(session_dir)
            video_file = os.path.join(session_dir, "video.mp4")
            with open(video_file, "w") as f:
                f.write("fresh video data")

            # mtime is current time
            now = time.time()
            os.utime(video_file, (now, now))
            self.assertAlmostEqual(get_session_latest_mtime(session_dir), now, delta=3.0)

            # Auto clean with 24 hours TTL
            with patch("agent_reels_viewer.cli.get_base_cache_dir", return_value=cache_dir):
                with patch.dict(os.environ, {"AGENT_REELS_TTL_HOURS": "24"}):
                    auto_clean_old_sessions(cache_dir)

            self.assertTrue(os.path.exists(session_dir), "Freshly downloaded session must NOT be pruned by TTL")
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
