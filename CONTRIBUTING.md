# Contributing to Agent Reels Viewer

Thank you for your interest in improving **Agent Reels Viewer**! We welcome contributions from developers, researchers, and agent creators.

---

## Code of Conduct & Ground Rules

1. **Local-First & Privacy First**: We do not introduce mandatory cloud API dependencies or telemetry. Data must remain local on the user's machine.
2. **Deterministic Tests**: All unit tests must run offline, without downloading real media from social networks. Real network tests are strictly isolated to canary scripts.
3. **Cross-Platform Compatibility**: Code must run seamlessly across **Linux (Ubuntu)**, **macOS**, and **Windows** on Python 3.10 through 3.13.

---

## Development Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/waniyaro/agent-reels-viewer.git
   cd agent-reels-viewer
   ```

2. **Create a virtual environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -e ".[speech]"
   ```

4. **Verify your local environment**:
   ```bash
   agent-reels-viewer doctor
   ```

5. **Run test suite**:
   ```bash
   python -m unittest discover -s tests -p "test_*.py"
   ```

---

## Submitting Pull Requests

- Keep PRs focused on a single bug fix, performance enhancement, or feature.
- Ensure all 57+ unit tests pass.
- Write unit tests for any new options, flags, or edge cases.
- Follow conventional commit style: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`.
