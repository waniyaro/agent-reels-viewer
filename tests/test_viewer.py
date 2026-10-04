"""Unit tests for agent-reels-viewer."""

import os
import unittest
from scripts.lib.downloader import detect_platform, validate_url
from scripts.lib.timeline import format_timestamp, assemble_timeline


class TestDownloader(unittest.TestCase):
    def test_detect_platform(self):
        self.assertEqual(detect_platform("https://www.instagram.com/reel/C312345/"), "instagram")
        self.assertEqual(detect_platform("https://instagr.am/p/C312345/"), "instagram")
        self.assertEqual(detect_platform("https://www.tiktok.com/@user/video/123456789"), "tiktok")
        self.assertEqual(detect_platform("https://youtube.com/shorts/abc123xyz"), "youtube")
        self.assertEqual(detect_platform("https://youtu.be/abc123xyz"), "youtube")

    def test_validate_url(self):
        valid, _ = validate_url("https://www.instagram.com/reel/C312345/")
        self.assertTrue(valid)

        valid, _ = validate_url("https://www.tiktok.com/@user/video/123456789")
        self.assertTrue(valid)

        # Invalid schemes
        invalid, err = validate_url("ftp://instagram.com/reel/123")
        self.assertFalse(invalid)
        self.assertIn("HTTP(S)", err)

        # Unsupported domain
        unsupported, err = validate_url("https://example.com/video.mp4")
        self.assertFalse(unsupported)
        self.assertIn("not supported", err)


class TestTimeline(unittest.TestCase):
    def test_format_timestamp(self):
        self.assertEqual(format_timestamp(0), "00:00")
        self.assertEqual(format_timestamp(65), "01:05")
        self.assertEqual(format_timestamp(125.4), "02:05")

    def test_assemble_timeline(self):
        tmp_dir = os.path.abspath("tests_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        try:
            meta = {
                "title": "Test Reel",
                "uploader": "test_creator",
                "webpage_url": "https://instagram.com/reel/test",
                "duration": 30,
            }
            keyframes = [(0.0, os.path.join(tmp_dir, "frame_01.jpg"))]
            speech = [{"start": 1.0, "end": 4.0, "text": "Hello world"}]

            path = assemble_timeline(
                tmp_dir,
                meta=meta,
                keyframes=keyframes,
                speech_segments=speech,
                has_speech=True,
                transcription_status="ok",
            )
            self.assertTrue(os.path.exists(path))
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
                self.assertIn("Security Boundary", content)
                self.assertIn("@test_creator", content)
                self.assertIn("Hello world", content)
        finally:
            if os.path.exists(tmp_dir):
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
