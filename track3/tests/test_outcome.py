from datetime import datetime, timedelta, timezone

import pytest

from outcome import label, load_intent, score_outcome

FIX = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
COVERAGE = load_intent("coverage_first")
THROUGHPUT = load_intent("throughput_first")


def samples(dropped: list, rate: float = 1.2, start: int = 0) -> list:
    return [{"minute": FIX + timedelta(minutes=start + i), "phones_dropped": d, "median_rate": rate} for i, d in enumerate(dropped)]


def test_restored_within_window_passes():
    r = score_outcome(samples([3, 0, 0, 0, 0]), FIX, FIX + timedelta(minutes=4), COVERAGE)
    assert r["restored"] is True
    assert r["restored_at"] == (FIX + timedelta(minutes=1)).isoformat()
    assert r["problem_closed"] is True
    assert r["verdict"] == "pass"
    assert r["score"] == 1.0


def test_never_restored_fails():
    r = score_outcome(samples([2] * 10), FIX, None, COVERAGE)
    assert r["restored"] is False
    assert r["verdict"] == "fail"


def test_restored_too_late_is_not_restored():
    r = score_outcome(samples([2] * 7 + [0] * 5), FIX, None, COVERAGE)
    assert r["restored"] is False
    assert r["verdict"] == "fail"


def test_rate_below_floor_is_regression():
    r = score_outcome(samples([0] * 5, rate=0.5), FIX, FIX + timedelta(minutes=3), COVERAGE)
    assert r["restored"] is True
    assert r["no_regression"] is False
    assert r["verdict"] == "fail"


def test_short_data_is_inconclusive():
    r = score_outcome(samples([0, 0]), FIX, None, COVERAGE)
    assert r["verdict"] == "inconclusive"


def test_no_data_is_inconclusive():
    r = score_outcome([], FIX, None, COVERAGE)
    assert r["verdict"] == "inconclusive"
    assert r["median_rate_after"] is None


def test_samples_before_the_fix_are_ignored():
    before = samples([0, 0, 0], start=-5)
    r = score_outcome(before + samples([2] * 10), FIX, None, COVERAGE)
    assert r["restored"] is False


def test_throughput_intent_weights_rate():
    data = samples([2] * 10, rate=3.5)
    assert score_outcome(data, FIX, None, THROUGHPUT)["verdict"] == "pass"
    assert score_outcome(data, FIX, None, COVERAGE)["verdict"] == "fail"


@pytest.mark.parametrize("judge,outcome,expected", [
    (True, True, "confirmed_good"), (True, False, "sounded_right_failed"),
    (False, True, "judge_miscalibrated"), (False, False, "confirmed_bad"),
])
def test_label_table(judge, outcome, expected):
    assert label(judge, outcome) == expected
