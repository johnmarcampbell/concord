"""Cross-cutting helpers shared across the package.

Thin home for small utilities that don't belong to any one entity or
layer and would otherwise force a layering inversion. Keeping them here
lets a core module (e.g. :mod:`concord.brief`) and the web layer
(:mod:`concord.web.app`) share one implementation without the web
package importing core or vice versa.
"""

#: Last digit → ordinal suffix for the common case. Any digit not in this
#: map (0 and 4-9) falls through to ``"th"``.
_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}

#: Inclusive range (on ``n % 100``) of numbers that take ``"th"`` despite
#: their last digit — eleventh, twelfth, thirteenth and their multiples of
#: 100 (111th, 212th, …).
_TEENS_LO = 11
_TEENS_HI = 13


def ordinal(n: int) -> str:
    """Return ``n`` with its English ordinal suffix, e.g. ``121`` -> ``"121st"``.

    The suffix follows the standard rule: numbers whose last two digits
    are 11-13 always take ``th`` (eleventh through thirteenth); otherwise
    the last digit picks ``st`` (1), ``nd`` (2), ``rd`` (3), or ``th``.
    Used to render Congress numbers (``119th``, ``121st``, ``123rd``)
    consistently in templates and the Bill Brief fact pack. Defined for
    positive integers, which is all Congress numbers ever are.
    """
    if _TEENS_LO <= (n % 100) <= _TEENS_HI:
        return f"{n}th"
    return f"{n}{_ORDINAL_SUFFIXES.get(n % 10, 'th')}"
