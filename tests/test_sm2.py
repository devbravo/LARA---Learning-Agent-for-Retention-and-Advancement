import pytest
from src.core.sm2 import calculate_next_review


def test_quality_2_hard_resets_interval_and_repetitions():
    """Quality 2 (Hard): interval resets to 1, repetitions reset to 0."""
    ef, interval, reps = calculate_next_review(
        quality=2, easiness_factor=2.5, interval_days=6, repetitions=3
    )
    assert interval == 1
    assert reps == 0


def test_quality_3_ok_first_session():
    """Quality 3 first session (repetitions=0): interval = 1, repetitions = 1."""
    ef, interval, reps = calculate_next_review(
        quality=3, easiness_factor=2.5, interval_days=1, repetitions=0
    )
    assert interval == 1
    assert reps == 1


def test_quality_3_ok_second_session():
    """Quality 3 second session (repetitions=1): interval = 6, repetitions = 2."""
    ef, interval, reps = calculate_next_review(
        quality=3, easiness_factor=2.5, interval_days=1, repetitions=1
    )
    assert interval == 6
    assert reps == 2


def test_quality_5_easy_third_session():
    """Quality 5 third session (repetitions=2): interval = round(6 * ef), repetitions = 3."""
    starting_ef = 2.5
    ef, interval, reps = calculate_next_review(
        quality=5, easiness_factor=starting_ef, interval_days=6, repetitions=2
    )
    assert interval == round(6 * starting_ef)
    assert reps == 3
    assert ef > starting_ef  # EF should increase on easy


def test_ef_never_drops_below_1_3():
    """EF floor is 1.3 no matter how many hard sessions."""
    ef = 1.4
    interval = 1
    reps = 0
    for _ in range(20):
        ef, interval, reps = calculate_next_review(
            quality=2, easiness_factor=ef, interval_days=interval, repetitions=reps
        )
    assert ef >= 1.3


def test_quality_2_decreases_ef():
    """Quality 2 reduces easiness_factor (above the floor)."""
    ef, _, _ = calculate_next_review(
        quality=2, easiness_factor=2.5, interval_days=1, repetitions=0
    )
    assert ef < 2.5


def test_quality_5_increases_ef():
    """Quality 5 increases easiness_factor."""
    ef, _, _ = calculate_next_review(
        quality=5, easiness_factor=2.5, interval_days=1, repetitions=0
    )
    assert ef > 2.5


def test_interval_grows_beyond_second_session():
    """After repetitions > 1, interval = round(interval * ef)."""
    ef, interval, reps = calculate_next_review(
        quality=5, easiness_factor=2.5, interval_days=6, repetitions=2
    )
    assert interval == round(6 * 2.5)  # 15
