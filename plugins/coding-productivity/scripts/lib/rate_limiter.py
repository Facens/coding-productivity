"""
Thread-safe token-bucket rate limiter with platform-aware header tracking.

Reads rate-limit headers from GitHub and GitLab API responses and sleeps
when the remaining quota drops below a configurable threshold.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Optional


class RateLimiter:
    """Token-bucket rate limiter with HTTP header awareness."""

    def __init__(
        self,
        *,
        fixed_delay: float = 0.0,
        platform: str = "github",
        threshold: int = 100,
    ):
        """
        Parameters
        ----------
        fixed_delay:
            Minimum seconds between consecutive requests.
        platform:
            ``"github"`` or ``"gitlab"`` -- determines which response headers
            to inspect for remaining/reset values.
        threshold:
            When ``remaining`` drops below this number, the limiter sleeps
            until the reset epoch.
        """
        self._fixed_delay = fixed_delay
        self._platform = platform.lower()
        self._threshold = threshold

        self._remaining: Optional[int] = None
        self._reset_epoch: Optional[float] = None
        self._last_request: float = 0.0
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    def wait(self) -> None:
        """Block until it is safe to issue the next request."""
        with self._lock:
            # Honour the fixed inter-request delay.
            elapsed = time.time() - self._last_request
            if elapsed < self._fixed_delay:
                time.sleep(self._fixed_delay - elapsed)

            # If we know the remaining budget is low, sleep until reset.
            if (
                self._remaining is not None
                and self._remaining < self._threshold
                and self._reset_epoch is not None
            ):
                wait = max(0, self._reset_epoch - time.time()) + 5
                self._print_countdown(wait)
                time.sleep(wait)

            self._last_request = time.time()

    def update_from_response(self, response) -> None:
        """Extract rate-limit metadata from an HTTP response.

        Parameters
        ----------
        response:
            A ``requests.Response`` object.
        """
        with self._lock:
            if self._platform == "github":
                remaining_hdr = response.headers.get("X-RateLimit-Remaining")
                reset_hdr = response.headers.get("X-RateLimit-Reset")
            else:  # gitlab
                remaining_hdr = response.headers.get("RateLimit-Remaining")
                reset_hdr = response.headers.get("RateLimit-Reset")

            if remaining_hdr is not None:
                try:
                    self._remaining = int(remaining_hdr)
                except (ValueError, TypeError):
                    pass

            if reset_hdr is not None:
                try:
                    self._reset_epoch = float(reset_hdr)
                except (ValueError, TypeError):
                    pass

    def handle_429(self, response) -> float:
        """Handle an HTTP 429 response.

        Reads the ``Retry-After`` header and returns the number of seconds
        to sleep.  The caller is responsible for actually sleeping.
        """
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return float(retry_after)
            except (ValueError, TypeError):
                pass
        return 60.0  # sensible default

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _print_countdown(seconds: float) -> None:
        total = int(seconds)
        print(
            f"  Rate limit low -- waiting {total}s for reset...",
            flush=True,
        )
        for remaining in range(total, 0, -1):
            mins, secs = divmod(remaining, 60)
            print(
                f"\r  Countdown: {mins:02d}:{secs:02d} ",
                end="",
                flush=True,
            )
            time.sleep(1)
        print("\r  Rate limit wait complete.       ", flush=True)

    def __repr__(self) -> str:
        return (
            f"<RateLimiter platform={self._platform!r} "
            f"remaining={self._remaining} threshold={self._threshold}>"
        )
