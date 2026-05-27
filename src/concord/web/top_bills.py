"""Hand-curated list of notable Bills to highlight on ``/bills``.

The entries point at real ``(congress, bill_type, bill_number)`` tuples
that may or may not have been scraped yet — the renderer skips any that
aren't in the local SQLite store, so the section degrades gracefully on
a partial dataset.
"""

from typing import NamedTuple


class CuratedBill(NamedTuple):
    """One curated highlight entry."""

    congress: int
    bill_type: str
    bill_number: int
    label: str
    blurb: str


#: Order is editorial — most-impactful / most-recognizable first.
CURATED_TOP_BILLS: tuple[CuratedBill, ...] = (
    CuratedBill(
        congress=119,
        bill_type="hr",
        bill_number=1,
        label="One Big Beautiful Bill Act",
        blurb=(
            "A 2025 reconciliation law making broad changes across tax, "
            "spending, health, energy, defense, and immigration policy."
        ),
    ),
    CuratedBill(
        congress=119,
        bill_type="s",
        bill_number=146,
        label="TAKE IT DOWN Act",
        blurb=(
            "A 2025 tech-safety law criminalizing nonconsensual intimate "
            "imagery, including AI-generated deepfakes, and requiring "
            "platforms to remove flagged content."
        ),
    ),
    CuratedBill(
        congress=118,
        bill_type="hr",
        bill_number=3746,
        label="Fiscal Responsibility Act of 2023",
        blurb=(
            "Suspended the debt ceiling while setting spending caps after "
            "the 2023 debt-limit standoff."
        ),
    ),
    CuratedBill(
        congress=118,
        bill_type="hr",
        bill_number=3935,
        label="FAA Reauthorization Act of 2024",
        blurb=(
            "Reauthorized the Federal Aviation Administration through "
            "fiscal year 2028 and updated aviation safety and civil "
            "aviation programs."
        ),
    ),
    CuratedBill(
        congress=117,
        bill_type="hr",
        bill_number=5376,
        label="Inflation Reduction Act",
        blurb=(
            "A major climate, energy, tax, and health-care law, including "
            "clean-energy incentives and Medicare drug-pricing provisions."
        ),
    ),
    CuratedBill(
        congress=117,
        bill_type="hr",
        bill_number=4346,
        label="CHIPS and Science Act",
        blurb=(
            "A semiconductor and science-R&D law intended to boost "
            "domestic chip manufacturing and strengthen U.S. technological "
            "competitiveness."
        ),
    ),
    CuratedBill(
        congress=117,
        bill_type="hr",
        bill_number=3684,
        label="Infrastructure Investment and Jobs Act",
        blurb=(
            "A $1.2 trillion infrastructure package funding transportation, "
            "broadband, water systems, energy, and other public works."
        ),
    ),
    CuratedBill(
        congress=117,
        bill_type="s",
        bill_number=3373,
        label="PACT Act",
        blurb=(
            "Expanded VA health care and benefits for veterans exposed to "
            "burn pits, Agent Orange, and other toxic substances."
        ),
    ),
    CuratedBill(
        congress=117,
        bill_type="s",
        bill_number=2938,
        label="Bipartisan Safer Communities Act",
        blurb=(
            "A gun-safety and mental-health law that added enhanced "
            "background checks for buyers under 21 and funded "
            "school/community safety programs."
        ),
    ),
    CuratedBill(
        congress=117,
        bill_type="hr",
        bill_number=1319,
        label="American Rescue Plan Act",
        blurb=(
            "A $1.9 trillion COVID-era relief law aimed at public health, "
            "direct economic aid, schools, state/local governments, and "
            "pandemic recovery."
        ),
    ),
)
