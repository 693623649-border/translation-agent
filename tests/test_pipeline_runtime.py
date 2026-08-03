import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pipeline_runtime import (
    RetryPolicy,
    SharedAdaptiveRateLimiter,
    StartRateLimiter,
    retry_with_backoff,
)


class RetryWithBackoffTests(unittest.TestCase):
    def test_non_retryable_failure_stops_immediately(self) -> None:
        failure = ValueError("invalid request")
        calls = 0
        sleeps: list[float] = []

        def operation() -> None:
            nonlocal calls
            calls += 1
            raise failure

        with self.assertRaises(ValueError) as raised:
            retry_with_backoff(
                operation,
                policy=RetryPolicy(attempts=5),
                should_retry=lambda _exc: False,
                sleep=sleeps.append,
            )

        self.assertIs(raised.exception, failure)
        self.assertEqual(calls, 1)
        self.assertEqual(sleeps, [])

    def test_http_429_uses_rate_limit_backoff_and_injected_jitter(self) -> None:
        class TooManyRequests(RuntimeError):
            code = 429

        calls = 0
        sleeps: list[float] = []
        random_values = iter((0.25, 0.5))

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise TooManyRequests("busy")
            return "ok"

        result = retry_with_backoff(
            operation,
            policy=RetryPolicy(
                attempts=3,
                rate_limit_base_delay=5.0,
                rate_limit_max_delay=45.0,
                rate_limit_jitter=3.0,
            ),
            should_retry=lambda _exc: True,
            sleep=sleeps.append,
            random_source=lambda: next(random_values),
        )

        self.assertEqual(result, "ok")
        self.assertEqual(calls, 3)
        self.assertEqual(sleeps, [5.75, 11.5])


class StartRateLimiterTests(unittest.TestCase):
    def test_wait_serializes_concurrent_workers(self) -> None:
        workers = 8
        barrier = threading.Barrier(workers)
        fake_time = [0.0]
        sleep_calls: list[float] = []

        def clock() -> float:
            return fake_time[0]

        def sleep(delay: float) -> None:
            sleep_calls.append(delay)
            fake_time[0] += delay

        limiter = StartRateLimiter(0.5, clock=clock, sleep=sleep)

        def worker(_index: int) -> float:
            barrier.wait()
            return limiter.wait()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            waits = list(executor.map(worker, range(workers)))

        self.assertEqual(sorted(waits), [0.0] + [0.5] * (workers - 1))
        self.assertEqual(sleep_calls, [0.5] * (workers - 1))
        self.assertEqual(fake_time[0], 0.5 * (workers - 1))


class SharedAdaptiveRateLimiterTests(unittest.TestCase):
    def test_instances_with_same_identity_share_request_schedule(self) -> None:
        now = [100.0]
        sleeps: list[float] = []

        def clock() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        with tempfile.TemporaryDirectory() as directory:
            first = SharedAdaptiveRateLimiter(
                10,
                identity="same-key",
                state_dir=Path(directory),
                clock=clock,
                sleep=sleep,
            )
            second = SharedAdaptiveRateLimiter(
                10,
                identity="same-key",
                state_dir=Path(directory),
                clock=clock,
                sleep=sleep,
            )
            self.assertEqual(first.wait(), 0)
            self.assertEqual(second.wait(), 10)
        self.assertEqual(sleeps, [10])

    def test_successes_speed_up_and_429_slows_down(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            limiter = SharedAdaptiveRateLimiter(
                10,
                identity="adaptive-key",
                min_interval=5,
                max_interval=30,
                success_window=2,
                decrease_factor=0.5,
                increase_factor=2,
                state_dir=Path(directory),
            )
            self.assertEqual(limiter.report_success(), (10, False))
            self.assertEqual(limiter.report_success(), (5, True))
            self.assertEqual(limiter.report_rate_limit(), (10, True))


if __name__ == "__main__":
    unittest.main()
