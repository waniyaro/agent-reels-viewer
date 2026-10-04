"""Unit tests for CI canary verification helper (scripts/ci_check.py)."""

import json
import unittest

from scripts.ci_check import parse_json_from_output, validate_canary_result


class TestCiCheck(unittest.TestCase):
    def test_youtube_success(self):
        data = {"status": "success", "session_id": "session_123"}
        ok, msg = validate_canary_result("youtube", data)
        self.assertTrue(ok)
        self.assertIn("Canary passed", msg)

    def test_tiktok_success(self):
        data = {"status": "success", "session_id": "session_456"}
        ok, msg = validate_canary_result("tiktok", data)
        self.assertTrue(ok)

    def test_instagram_success(self):
        data = {"status": "success", "session_id": "session_789"}
        ok, msg = validate_canary_result("instagram", data)
        self.assertTrue(ok)

    def test_instagram_needs_cookies_is_accepted(self):
        data = {"status": "error", "error_code": "NEEDS_COOKIES", "message": "Login required"}
        ok, msg = validate_canary_result("instagram", data)
        self.assertTrue(ok)
        self.assertIn("NEEDS_COOKIES", msg)

    def test_youtube_needs_cookies_is_rejected(self):
        data = {"status": "error", "error_code": "NEEDS_COOKIES", "message": "Login required"}
        ok, msg = validate_canary_result("youtube", data)
        self.assertFalse(ok)

    def test_private_video_fails_canary(self):
        """PRIVATE_VIDEO signals that a canary clip was deleted and must be replaced."""
        data = {"status": "error", "error_code": "PRIVATE_VIDEO", "message": "This video is private"}
        ok, msg = validate_canary_result("youtube", data)
        self.assertFalse(ok)
        self.assertIn("PRIVATE_VIDEO", msg)

        ok_insta, msg_insta = validate_canary_result("instagram", data)
        self.assertFalse(ok_insta)
        self.assertIn("PRIVATE_VIDEO", msg_insta)

    def test_extractor_broken_fails_canary(self):
        data = {"status": "error", "error_code": "EXTRACTOR_BROKEN", "message": "yt-dlp layout error"}
        ok, msg = validate_canary_result("tiktok", data)
        self.assertFalse(ok)
        self.assertIn("EXTRACTOR_BROKEN", msg)

    def test_speech_canary_checks_speech_status_ok(self):
        # Speech ok -> pass
        ok, msg = validate_canary_result("speech", {"status": "success", "speech_status": "ok"})
        self.assertTrue(ok)

        # Speech none or error -> fail
        ok_none, _ = validate_canary_result("speech", {"status": "success", "speech_status": "none"})
        self.assertFalse(ok_none)

        ok_err, _ = validate_canary_result("speech", {"status": "error", "error_code": "AUDIO_EXTRACTION_FAILED"})
        self.assertFalse(ok_err)

    def test_parse_json_from_noisy_cli_output(self):
        raw = "WARNING: [youtube] Some warning text\n{\"status\": \"success\", \"platform\": \"youtube\"}\nCleaning session...\n"
        data = parse_json_from_output(raw)
        self.assertIsNotNone(data)
        self.assertEqual(data.get("status"), "success")


if __name__ == "__main__":
    unittest.main()
