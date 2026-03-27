#!/usr/bin/env python3
"""Detect existing CLI authentication tokens for GitHub or GitLab.

Usage:
    python detect_token.py github
    python detect_token.py gitlab [--url https://gitlab.example.com]

Prints the token to stdout if found, or exits with code 1 if not.
"""

import shutil
import subprocess
import sys


def detect_github_token() -> str | None:
    """Try gh CLI first, then GITHUB_TOKEN env var."""
    gh = shutil.which("gh")
    if gh:
        try:
            result = subprocess.run(
                [gh, "auth", "token"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            pass

    import os
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token

    return None


def detect_gitlab_token(gitlab_url: str = "https://gitlab.com") -> str | None:
    """Try glab CLI first, then GITLAB_TOKEN env var."""
    glab = shutil.which("glab")
    if glab:
        try:
            result = subprocess.run(
                [glab, "config", "get", "token", "-h", gitlab_url.replace("https://", "").replace("http://", "")],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            pass

    import os
    token = os.environ.get("GITLAB_TOKEN", "").strip()
    if token:
        return token

    return None


def main():
    if len(sys.argv) < 2:
        print("Usage: detect_token.py github|gitlab [--url URL]", file=sys.stderr)
        sys.exit(1)

    platform = sys.argv[1].lower()

    if platform == "github":
        token = detect_github_token()
    elif platform == "gitlab":
        url = "https://gitlab.com"
        if "--url" in sys.argv:
            idx = sys.argv.index("--url")
            if idx + 1 < len(sys.argv):
                url = sys.argv[idx + 1]
        token = detect_gitlab_token(url)
    else:
        print(f"Unknown platform: {platform}", file=sys.stderr)
        sys.exit(1)

    if token:
        print(token)
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
