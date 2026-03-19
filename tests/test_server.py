from unittest.mock import patch
from server import RateLimiter

def test_rate_limiter():
    limiter = RateLimiter(max_requests=2, window_seconds=10)

    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is False

def test_rate_limiter_window():
    with patch('time.time') as mock_time:
        mock_time.return_value = 100
        limiter = RateLimiter(max_requests=1, window_seconds=10)

        assert limiter.allow() is True
        assert limiter.allow() is False

        # Move forward in time past window
        mock_time.return_value = 111
        assert limiter.allow() is True
