"""Calendar arithmetic for the **Current Congress** (CONTEXT.md → Orchestration).

A Congress spans two years, beginning January 3 of the odd year that follows
each federal election; the 119th Congress runs 2025-2026. The 1st Congress
convened in 1789. This module is a pure, dependency-free helper so it can be
imported anywhere (including non-CLI code) and unit-tested at the boundaries.
"""

from datetime import date


def current_congress(today: date) -> int:
    """Return the number of the Congress in session on ``today``.

    ``current_congress(date(2026, 6, 23)) == 119``. The boundary is January 3
    of an odd year: on/after Jan 3 of an odd year the new Congress has
    convened; before it, the previous Congress is still sitting.
    """
    year = today.year
    if year % 2 == 0:  # even year → the Congress began the previous (odd) year
        start_year = year - 1
    elif today >= date(year, 1, 3):  # odd year, on/after Jan 3 → this year's Congress
        start_year = year
    else:  # odd year, before Jan 3 → the previous Congress is still sitting
        start_year = year - 2
    return (start_year - 1789) // 2 + 1
