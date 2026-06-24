"""Unit tests for :func:`concord.congress.current_congress`.

The interesting cases are the January-3 boundaries of odd years, where the
new Congress convenes; everything else is interior to a two-year span.
"""

from datetime import date

import pytest

from concord.congress import current_congress


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 6, 23), 119),  # interior of an even year
        (date(2025, 6, 1), 119),  # interior of the odd start year
        (date(2025, 1, 3), 119),  # the day the 119th convenes
        (date(2025, 1, 1), 118),  # odd year, before Jan 3 → 118th still sitting
        (date(2027, 1, 2), 119),  # day before the 120th convenes
        (date(2027, 1, 3), 120),  # the day the 120th convenes
    ],
)
def test_current_congress(today: date, expected: int) -> None:
    assert current_congress(today) == expected
