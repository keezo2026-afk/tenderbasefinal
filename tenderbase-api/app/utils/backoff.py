"""Shared backoff arithmetic for anything that retries.

Both the HTTP fetcher and the ARQ workers need "wait a bit longer each time, but
not forever, and not in lockstep with everyone else". Centralising it here keeps
one definition of *attempt 0* and one jitter model, and makes both call sites
testable without a network or a broker.

Full jitter (a uniform draw in ``[0, cap]`` of the exponential value) is chosen
over a fixed delay on purpose: when a source comes back after an outage, a
thousand workers that all slept exactly ``2 ** n`` seconds would stampede it at
the same instant. Spreading the wake-ups is the difference between a recovery and
a self-inflicted second outage.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Final

#: Hard ceiling on a single sleep. A retry policy that can sleep for hours stops
#: looking like a retry and starts looking like a stuck job.
DEFAULT_MAX_SECONDS: Final[float] = 30.0


def exponential_backoff_seconds(
    attempt: int,
    *,
    base_seconds: float,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    jitter: bool = True,
    rng: Callable[[], float] | None = None,
) -> float:
    """Delay before ``attempt`` (0-based) is retried.

    :param attempt: how many tries have already happened; ``0`` means "the first
        failure was just observed, schedule the second try".
    :param base_seconds: multiplier for the exponential curve.
    :param max_seconds: ceiling applied *before* jitter, so the drawn value can
        never exceed it.
    :param jitter: when false the curve is deterministic (useful for tests and
        for operators who want to reason about worst-case latency).
    :param rng: injectable uniform source, returning ``[0.0, 1.0)``.
    """
    if attempt < 0:
        raise ValueError("attempt must be zero or greater")
    if base_seconds < 0 or max_seconds < 0:
        raise ValueError("backoff delays cannot be negative")

    capped = min(max_seconds, base_seconds * (2**attempt))
    if not jitter or capped <= 0:
        return capped
    draw = (rng or random.random)()
    return capped * min(1.0, max(0.0, draw))
