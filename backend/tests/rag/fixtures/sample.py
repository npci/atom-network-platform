# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Rate limiter module — test fixture."""

COUNTER_LIMIT = 100


class RateLimiter:
    """Token bucket rate limiter."""

    def __init__(self, limit):
        self.limit = limit
        self.count = 0

    def acquire(self):
        self.count += 1
        return self.count <= self.limit


def log_event(event):
    """Write event to log."""
    print(f"event: {event}")
