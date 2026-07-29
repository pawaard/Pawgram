import unittest

from app.rate_limit import InMemoryRateLimiter


class RateLimiterTests(unittest.TestCase):
    def test_limit_is_enforced_per_key_and_resettable(self):
        limiter = InMemoryRateLimiter(limit=2, window_seconds=60)

        self.assertTrue(limiter.allow("first"))
        self.assertTrue(limiter.allow("first"))
        self.assertFalse(limiter.allow("first"))
        self.assertTrue(limiter.allow("second"))
        limiter.reset("first")
        self.assertTrue(limiter.allow("first"))


if __name__ == "__main__":
    unittest.main()
