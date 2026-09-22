"""Small-sample statistics: empty, one, sparse, normal, large; smoothing,
intervals, confidence labels, baseline reference."""

import pytest

from app.learning.models import LearningConfidence, Metric
from app.learning.stats import confidence_for, estimate, median, smoothed_rate, wilson_interval


def test_empty_sample():
    e = estimate(Metric.RESPONSE_RATE, 0, 0, baseline_rate=0.3, prior_strength=10, min_samples=5)
    assert e.n == 0 and e.raw_rate is None and e.smoothed_rate == 0.3 and (e.ci_low, e.ci_high) == (0.0, 1.0) and e.confidence is LearningConfidence.NONE


def test_one_sample_is_pulled_to_the_baseline_and_wide():
    e = estimate(Metric.INTERVIEW_RATE, 1, 1, baseline_rate=0.1, prior_strength=10, min_samples=5)
    assert e.raw_rate == 1.0 and e.smoothed_rate == pytest.approx((1 + 0.1 * 10) / 11) and e.smoothed_rate < 0.2
    assert e.ci_low < 0.25 and e.ci_high == 1.0 and e.confidence is LearningConfidence.LOW


def test_sparse_normal_and_large_samples():
    sparse = estimate(Metric.RESPONSE_RATE, 3, 4, 0.3, 10, 5)
    normal = estimate(Metric.RESPONSE_RATE, 15, 20, 0.3, 10, 5)
    large = estimate(Metric.RESPONSE_RATE, 750, 1000, 0.3, 10, 5)
    assert sparse.confidence is LearningConfidence.LOW and normal.confidence is LearningConfidence.HIGH and large.confidence is LearningConfidence.HIGH
    assert sparse.smoothed_rate < normal.smoothed_rate < large.smoothed_rate <= 0.75
    assert large.smoothed_rate == pytest.approx(0.75, abs=0.005) and (large.ci_high - large.ci_low) < 0.06
    assert (sparse.ci_high - sparse.ci_low) > 0.5, "four samples give a wide interval"
    assert estimate(Metric.RESPONSE_RATE, 6, 12, 0.3, 10, 5).confidence is LearningConfidence.MEDIUM


def test_smoothing_is_monotone_in_n_and_neutral_with_zero_prior():
    assert smoothed_rate(0, 0, 0.4, 10) == 0.4
    assert smoothed_rate(5, 5, 0.4, 0) == 1.0
    rates = [smoothed_rate(n, n, 0.2, 10) for n in (1, 5, 20, 100)]
    assert rates == sorted(rates) and rates[0] < 0.4 and rates[-1] > 0.9
    with pytest.raises(ValueError):
        smoothed_rate(3, 2, 0.5, 10)


def test_wilson_interval_bounds():
    assert wilson_interval(0, 0) == (0.0, 1.0)
    low, high = wilson_interval(0, 10)
    assert low == 0.0 and 0.2 < high < 0.35
    low, high = wilson_interval(50, 100)
    assert 0.40 < low < 0.41 and 0.59 < high < 0.60


def test_confidence_labels_and_median():
    assert [confidence_for(n, 5) for n in (0, 1, 4, 5, 19, 20)] == [LearningConfidence.NONE, LearningConfidence.LOW, LearningConfidence.LOW, LearningConfidence.MEDIUM, LearningConfidence.MEDIUM, LearningConfidence.HIGH]
    assert median([]) is None and median([None, 3.0, 1.0, None]) == 2.0 and median([1, 2, 3]) == 2
