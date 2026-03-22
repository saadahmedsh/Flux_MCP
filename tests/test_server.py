"""Tests for server.py utility classes."""

from unittest.mock import patch

from server import RateLimiter


def test_rate_limiter_allows_within_limit() -> None:
    """Requests within the configured limit should be allowed."""
    limiter: RateLimiter = RateLimiter(max_requests=2, window_seconds=10)
    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is False


def test_rate_limiter_resets_after_window() -> None:
    """Requests should be allowed again after the window expires."""
    with patch("time.time") as mock_time:
        mock_time.return_value = 100.0
        limiter: RateLimiter = RateLimiter(max_requests=1, window_seconds=10)

        assert limiter.allow() is True
        assert limiter.allow() is False

        mock_time.return_value = 111.0
        assert limiter.allow() is True
