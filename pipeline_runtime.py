"""Shared runtime primitives for pipeline clients and concurrent workers.

The helpers in this module deliberately do not know about a specific model
provider.  HTTP and MCP adapters supply their own retryability classification,
while sharing the same delay policy.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, TypeVar


ResultT = TypeVar("ResultT")
ExceptionClassifier = Callable[[Exception], bool]


@dataclass(frozen=True)
class RetryPolicy:
    """Delay settings for :func:`retry_with_backoff`.

    ``attempts`` includes the initial call.  Delays are linear in the number
    of the failed attempt and capped independently for ordinary and
    rate-limited failures.  Jitter is only added for rate-limited failures.
    """

    attempts: int = 4
    base_delay: float = 2.0
    max_delay: float = 12.0
    rate_limit_base_delay: float = 5.0
    rate_limit_max_delay: float = 45.0
    rate_limit_jitter: float = 3.0

    def __post_init__(self) -> None:
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int):
            raise TypeError("attempts must be an integer")
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")
        for name in (
            "base_delay",
            "max_delay",
            "rate_limit_base_delay",
            "rate_limit_max_delay",
            "rate_limit_jitter",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")

    def delay_for(
        self,
        failed_attempt: int,
        *,
        rate_limited: bool,
        random_source: Callable[[], float] = random.random,
    ) -> float:
        """Return the delay after a failed, non-final attempt."""

        if failed_attempt < 1:
            raise ValueError("failed_attempt must be at least 1")
        if rate_limited:
            return min(
                self.rate_limit_max_delay,
                self.rate_limit_base_delay * failed_attempt,
            ) + self.rate_limit_jitter * random_source()
        return min(self.max_delay, self.base_delay * failed_attempt)


def _default_is_rate_limited(exc: Exception) -> bool:
    """Recognize common HTTP and message-based rate-limit errors."""

    for attribute in ("code", "status", "status_code"):
        if getattr(exc, attribute, None) == 429:
            return True
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True
    message = str(exc).lower()
    return "429" in message or "rate limit" in message or "rate-limit" in message


def retry_with_backoff(
    operation: Callable[[], ResultT],
    *,
    policy: RetryPolicy | None = None,
    should_retry: ExceptionClassifier | None = None,
    is_rate_limited: ExceptionClassifier | None = None,
    sleep: Callable[[float], None] = time.sleep,
    random_source: Callable[[], float] = random.random,
) -> ResultT:
    """Run ``operation`` until it succeeds or its retry policy is exhausted.

    Provider adapters decide which failures are retryable through
    ``should_retry``.  By default all ordinary ``Exception`` instances are
    retryable.  ``is_rate_limited`` can override the built-in recognition of
    HTTP 429 and common MCP rate-limit messages.

    The original exception is raised unchanged for a non-retryable failure or
    after the final attempt, allowing each client to add provider-specific
    context at its own boundary.
    """

    effective_policy = policy or RetryPolicy()
    retry_classifier = should_retry or (lambda _exc: True)
    rate_limit_classifier = is_rate_limited or _default_is_rate_limited

    for attempt in range(1, effective_policy.attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if not retry_classifier(exc) or attempt == effective_policy.attempts:
                raise
            delay = effective_policy.delay_for(
                attempt,
                rate_limited=rate_limit_classifier(exc),
                random_source=random_source,
            )
            sleep(delay)

    raise AssertionError("retry loop exited without returning or raising")


class StartRateLimiter:
    """Enforce a minimum interval between request start permissions.

    A limiter instance is intended to be shared by all workers using one
    provider profile.  The lock is held through the wait so that another
    worker cannot reserve the same start time.
    """

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not math.isfinite(interval) or interval < 0:
            raise ValueError("interval must be a finite non-negative number")
        self.interval = float(interval)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_start_at = 0.0

    def wait(self) -> float:
        """Wait for a start slot and return the requested sleep duration."""

        with self._lock:
            wait_for = max(0.0, self._next_start_at - self._clock())
            if wait_for:
                self._sleep(wait_for)
            self._next_start_at = self._clock() + self.interval
            return wait_for
