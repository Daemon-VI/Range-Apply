"""Small-sample-safe statistics (``SMOOTHING_METHOD``).

* **Shrinkage**: a group's rate is the beta-binomial posterior mean with a
  prior centred on the tenant's own baseline —
  ``(positives + baseline * k) / (n + k)`` with ``k = prior_strength``
  pseudo-observations. One interview out of one application is pulled
  almost all the way back to the baseline; a hundred barely move.
* **Bounds**: the Wilson score interval (95 %) on the raw rate, so a sparse
  group shows a wide interval instead of a confident number.
* **Confidence label**: from the sample count against ``min_samples``:
  NONE (0), LOW (< min), MEDIUM (< 4 × min), HIGH.

Everything is pure and deterministic; the same counts give the same numbers.
"""

import math
from statistics import median as _median
from typing import Iterable, Optional

from app.learning.models import LearningConfidence, Metric, RateEstimate

Z_95 = 1.959964


def smoothed_rate(positives: int, n: int, prior_rate: float, prior_strength: float) -> float:
    if n < 0 or positives < 0 or positives > n:
        raise ValueError("positives must be within 0..n")
    prior_rate = min(1.0, max(0.0, prior_rate))
    denominator = n + prior_strength
    if denominator <= 0:
        return prior_rate
    return (positives + prior_rate * prior_strength) / denominator


def wilson_interval(positives: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """95 % Wilson score interval for a binomial proportion; ``(0, 1)`` when empty."""
    if n <= 0:
        return 0.0, 1.0
    p = positives / n
    z2 = z * z
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def confidence_for(n: int, min_samples: int) -> LearningConfidence:
    if n <= 0:
        return LearningConfidence.NONE
    if n < min_samples:
        return LearningConfidence.LOW
    if n < 4 * min_samples:
        return LearningConfidence.MEDIUM
    return LearningConfidence.HIGH


def estimate(metric: Metric, positives: int, n: int, baseline_rate: float, prior_strength: float, min_samples: int) -> RateEstimate:
    low, high = wilson_interval(positives, n)
    return RateEstimate(
        metric=metric,
        n=n,
        positives=positives,
        negatives=n - positives,
        raw_rate=(positives / n) if n else None,
        smoothed_rate=round(smoothed_rate(positives, n, baseline_rate, prior_strength), 6),
        ci_low=round(low, 6),
        ci_high=round(high, 6),
        confidence=confidence_for(n, min_samples),
        baseline_rate=round(baseline_rate, 6),
        prior_strength=prior_strength,
    )


def median(values: Iterable[Optional[float]]) -> Optional[float]:
    present = [float(v) for v in values if v is not None]
    return round(_median(present), 3) if present else None


__all__ = ["Z_95", "confidence_for", "estimate", "median", "smoothed_rate", "wilson_interval"]
