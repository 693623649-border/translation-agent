"""Shared runtime primitives for pipeline clients and concurrent workers.

The helpers in this module deliberately do not know about a specific model
provider.  HTTP and MCP adapters supply their own retryability classification,
while sharing the same delay policy.
"""

from __future__ import annotations

import math
import json
import os
import random
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

try:  # POSIX workers coordinate through flock; other platforms fall back locally.
    import fcntl
except ImportError:  # pragma: no cover - Windows compatibility path.
    fcntl = None


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


class SharedAdaptiveRateLimiter:
    """Coordinate request starts across threads and pipeline processes.

    The state file is keyed by a one-way credential fingerprint, so concurrent
    books using the same provider quota share one request-start schedule.  A
    rate-limit response raises the interval; sustained successes reduce it in
    small steps.  No credential or request content is persisted.
    """

    def __init__(
        self,
        interval: float,
        *,
        identity: str,
        min_interval: float = 5.0,
        max_interval: float = 60.0,
        success_window: int = 8,
        decrease_factor: float = 0.9,
        increase_factor: float = 1.5,
        state_dir: Path | str | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        for name, value in (
            ("interval", interval),
            ("min_interval", min_interval),
            ("max_interval", max_interval),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if min_interval > max_interval:
            raise ValueError("min_interval cannot exceed max_interval")
        if success_window < 1:
            raise ValueError("success_window must be at least 1")
        if not 0 < decrease_factor <= 1:
            raise ValueError("decrease_factor must be in (0, 1]")
        if increase_factor < 1:
            raise ValueError("increase_factor must be at least 1")
        self.initial_interval = min(max(float(interval), min_interval), max_interval)
        self.min_interval = float(min_interval)
        self.max_interval = float(max_interval)
        self.success_window = int(success_window)
        self.decrease_factor = float(decrease_factor)
        self.increase_factor = float(increase_factor)
        self._clock = clock
        self._sleep = sleep
        self._local = StartRateLimiter(
            self.initial_interval,
            clock=clock,
            sleep=sleep,
        )
        self._local_state = self._default_state(self._clock())
        self._thread_lock = threading.Lock()
        safe_identity = "".join(
            character for character in identity.lower() if character.isalnum()
        )[:64]
        root = Path(
            state_dir
            or os.getenv("TRANSLATION_AGENT_RATE_LIMIT_DIR")
            or Path(tempfile.gettempdir()) / "translation-agent-rate-limits"
        )
        self.state_path = (
            root / f"{safe_identity or 'default'}.json"
            if fcntl is not None
            else None
        )

    def _default_state(self, now: float) -> dict[str, float | int]:
        return {
            "interval": self.initial_interval,
            "next_start_at": 0.0,
            "success_streak": 0,
            "updated_at": now,
        }

    def _read_state(self, handle: object, now: float) -> dict[str, float | int]:
        handle.seek(0)
        raw = handle.read()
        try:
            value = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            value = {}
        if not isinstance(value, dict):
            value = {}
        try:
            updated_at = float(value.get("updated_at", 0.0))
        except (TypeError, ValueError):
            updated_at = 0.0
        # Do not inherit an old throttled interval from a long-finished job.
        if now - updated_at > 3600:
            return self._default_state(now)
        try:
            interval = float(value.get("interval", self.initial_interval))
            next_start_at = float(value.get("next_start_at", 0.0))
            success_streak = int(value.get("success_streak", 0))
        except (TypeError, ValueError):
            return self._default_state(now)
        return {
            "interval": min(max(interval, self.min_interval), self.max_interval),
            "next_start_at": max(0.0, next_start_at),
            "success_streak": max(0, success_streak),
            "updated_at": updated_at,
        }

    @staticmethod
    def _write_state(handle: object, state: dict[str, float | int]) -> None:
        handle.seek(0)
        handle.truncate()
        json.dump(state, handle, ensure_ascii=True, separators=(",", ":"))
        handle.flush()

    def _update(self, operation: Callable[[dict[str, float | int], float], float]) -> float:
        if self.state_path is None:
            with self._thread_lock:
                now = self._clock()
                result = operation(self._local_state, now)
                self._local_state["updated_at"] = now
                self._local.interval = float(self._local_state["interval"])
                return result
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, self.state_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                now = self._clock()
                state = self._read_state(handle, now)
                result = operation(state, now)
                state["updated_at"] = now
                self._write_state(handle, state)
                return result
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def wait(self) -> float:
        if self.state_path is None:
            return self._local.wait()
        total_wait = 0.0
        while True:
            def reserve(state: dict[str, float | int], now: float) -> float:
                wait_for = max(0.0, float(state["next_start_at"]) - now)
                if wait_for == 0:
                    state["next_start_at"] = now + float(state["interval"])
                return wait_for

            wait_for = self._update(reserve)
            if wait_for <= 0:
                return total_wait
            self._sleep(wait_for)
            total_wait += wait_for

    def report_success(self) -> tuple[float, bool]:
        def reward(state: dict[str, float | int], _now: float) -> float:
            streak = int(state["success_streak"]) + 1
            interval = float(state["interval"])
            if streak >= self.success_window:
                interval = max(self.min_interval, interval * self.decrease_factor)
                streak = 0
            state["success_streak"] = streak
            state["interval"] = interval
            return interval

        before = self.current_interval()
        after = self._update(reward)
        return after, not math.isclose(before, after)

    def report_rate_limit(self) -> tuple[float, bool]:
        def penalize(state: dict[str, float | int], now: float) -> float:
            interval = min(
                self.max_interval,
                max(self.initial_interval, float(state["interval"]) * self.increase_factor),
            )
            state["interval"] = interval
            state["success_streak"] = 0
            state["next_start_at"] = max(
                float(state["next_start_at"]),
                now + interval,
            )
            return interval

        before = self.current_interval()
        after = self._update(penalize)
        return after, not math.isclose(before, after)

    def current_interval(self) -> float:
        return self._update(
            lambda state, _now: float(state["interval"])
        )
