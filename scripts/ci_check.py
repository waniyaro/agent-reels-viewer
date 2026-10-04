#!/usr/bin/env python3
"""CI verification helper for live canary checks.

Validates output JSON from agent-reels-viewer inspect commands.
"""

import json
import re
import sys
from typing import Any, Dict, Optional, Tuple


def parse_json_from_output(raw_output: str) -> Optional[Dict[str, Any]]:
    """Extract and parse first valid JSON object from CLI output."""
    raw = raw_output.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Search for embedded JSON object {...}
        m = re.search(r"(\{.*\})", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    return None


def validate_canary_result(platform: str, data: Dict[str, Any]) -> Tuple[bool, str]:
    """Validate canary execution result according to platform policy.

    Policy:
    - YouTube and TikTok: must return status='success'.
    - Instagram: status='success' or error_code='NEEDS_COOKIES'.
    - Speech check: status='success' and speech_status='ok'.
    - PRIVATE_VIDEO: failure on all platforms (signals canary video was deleted/privated).
    - Any extractor/download breakage: failure.
    """
    status = data.get("status")
    error_code = data.get("error_code", "")
    message = data.get("message", "")

    if status == "success":
        if platform == "speech":
            speech_status = data.get("speech_status")
            if speech_status == "ok":
                return True, "Speech dialogue correctly detected and transcribed (speech_status=ok)."
            return False, f"Speech canary failed: expected speech_status='ok', got '{speech_status}'."
        return True, f"Canary passed for {platform} (status=success)."

    if error_code == "PRIVATE_VIDEO":
        return False, f"Canary failed: video was deleted or made private (PRIVATE_VIDEO). Update canary URL."

    if platform == "instagram" and error_code == "NEEDS_COOKIES":
        return True, "Instagram canary passed with expected login-wall checkpoint (NEEDS_COOKIES)."

    return False, f"Canary failed for {platform}: [{error_code}] {message}"


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python scripts/ci_check.py <platform> <raw_output_or_json>", file=sys.stderr)
        return 1

    platform = sys.argv[1].lower().strip()
    raw_output = sys.argv[2]

    data = parse_json_from_output(raw_output)
    if data is None:
        print(f"Error: Could not parse JSON from output: {raw_output[:200]}", file=sys.stderr)
        return 1

    ok, explanation = validate_canary_result(platform, data)
    print(f"[{'PASS' if ok else 'FAIL'}] {explanation}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
