"""Bill Brief generation — the first chat/completions use of OpenAI.

A Bill Brief (ADR 0020) is two layers: a deterministic fact pack rendered
verbatim, plus a single LLM-authored ``executive_summary``. This module
owns the generated layer and the fact pack that grounds it.

Design mirrors :mod:`concord.embedding`: a :class:`Briefer` wraps an
injected OpenAI-compatible client (constructor injection + a ``_…Like``
Protocol) so production and tests share one surface and tests need no
network or key. The model name is a module constant
(:data:`DEFAULT_BRIEF_MODEL`) so a swap is one edit, per ADR 0004.

Honesty model (ADR 0020): the fact pack and the most recent CRS summary
are handed to the model as ground truth it may narrate but must not
contradict. The user's optional *lens* steers emphasis/framing only; the
system prompt's honesty rules sit above it in priority. Facts are fixed;
only their framing flexes.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

#: Default chat model for brief generation. Centralized here like
#: :data:`concord.embedding.DEFAULT_MODEL` so a swap is one edit (ADR 0004).
DEFAULT_BRIEF_MODEL = "gpt-4o-mini"

#: Sampling temperature. Low but non-zero: briefs should be steady and
#: factual, with just enough freedom to read like prose rather than a
#: template fill.
BRIEF_TEMPERATURE = 0.3

#: Bump when the prompt or :class:`GeneratedBrief` schema changes in a way
#: that should invalidate cached neutral briefs. Folded into the
#: ``facts_hash`` so a prompt revision marks every cached brief stale.
BRIEF_PROMPT_VERSION = 1

_log = logging.getLogger("concord.brief")

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class BriefError(Exception):
    """Raised when the LLM call or its parsing fails unrecoverably."""


class BriefFacts(BaseModel):
    """Deterministic fact pack about a Bill — the always-neutral anchor.

    Computed by :func:`build_facts` from already-loaded SQLite rows; no
    LLM involved. Fed to the model as ground truth (it may narrate but
    not contradict these) and also the basis of the cache/staleness
    ``facts_hash``.
    """

    bill_id: str
    identifier: str  # e.g. "HR 1234"
    title: str
    congress: int
    origin_chamber: str
    sponsor_display_name: str | None
    policy_area: str | None
    introduced_date: str | None
    latest_action_date: str | None
    latest_action_text: str | None
    cosponsor_count: int
    original_cosponsor_count: int
    withdrawn_cosponsor_count: int
    cosponsor_party_counts: dict[str, int]  # e.g. {"D": 12, "R": 4, "Unknown": 1}
    subjects: list[str]
    action_count: int
    vote_count: int
    latest_summary_stage: str | None  # CRS version_code / action_desc
    latest_summary_date: str | None
    latest_summary_text: str | None  # CRS prose, plain text (HTML stripped)


class GeneratedBrief(BaseModel):
    """The LLM-authored portion of a Bill Brief.

    v1 is one field. Extensible per ADR 0020: add a field here, extend
    the prompt, and bump :data:`BRIEF_PROMPT_VERSION`.
    """

    executive_summary: str


@dataclass(frozen=True)
class BriefView:
    """A rendered Bill Brief as the template consumes it.

    The single shape both the profile GET (cached, possibly stale) and the
    generate POST (freshly produced or cache-hit) hand to the template, so
    the view contract lives in one place. ``stale`` is set when the
    underlying fact pack has moved since the brief was written.
    """

    executive_summary: str
    lens: str
    generated_at: str
    model: str
    stale: bool


def build_facts(
    *,
    bill: dict[str, Any],
    cosponsors: list[dict[str, Any]],
    cosponsor_party_counts: dict[str, int],
    subjects: list[str],
    action_count: int,
    vote_count: int,
    latest_summary: dict[str, Any] | None,
) -> BriefFacts:
    """Assemble a :class:`BriefFacts` from already-fetched SQLite rows.

    Pure over plain Python data (the dicts the web read-layer returns) so
    it is testable without a database. ``latest_summary`` is the most
    recent CRS summary row (highest ``action_date``), or ``None``.
    """
    original = sum(1 for c in cosponsors if c.get("is_original_cosponsor"))
    withdrawn = sum(1 for c in cosponsors if c.get("sponsorship_withdrawn_date"))

    stage: str | None = None
    summary_date: str | None = None
    summary_text: str | None = None
    if latest_summary is not None:
        stage = latest_summary.get("action_desc") or latest_summary.get("version_code")
        summary_date = latest_summary.get("action_date")
        raw = latest_summary.get("summary_text")
        summary_text = _strip_html(raw) if raw else None

    return BriefFacts(
        bill_id=bill["bill_id"],
        identifier=f"{str(bill['bill_type']).upper()} {bill['bill_number']}",
        title=bill["title"],
        congress=int(bill["congress"]),
        origin_chamber=bill["origin_chamber"],
        sponsor_display_name=bill.get("sponsor_display_name"),
        policy_area=bill.get("policy_area"),
        introduced_date=bill.get("introduced_date"),
        latest_action_date=bill.get("latest_action_date"),
        latest_action_text=bill.get("latest_action_text"),
        cosponsor_count=len(cosponsors),
        original_cosponsor_count=original,
        withdrawn_cosponsor_count=withdrawn,
        cosponsor_party_counts=cosponsor_party_counts,
        subjects=list(subjects),
        action_count=action_count,
        vote_count=vote_count,
        latest_summary_stage=stage,
        latest_summary_date=summary_date,
        latest_summary_text=summary_text,
    )


def facts_hash(facts: BriefFacts, *, model: str, prompt_version: int) -> str:
    """SHA-256 over the fact pack + model + prompt version.

    The cache key for a neutral brief and the staleness signal for every
    brief: when the underlying mirror data, the model, or the prompt
    changes, the hash changes and the web layer flags the cached brief
    stale (ADR 0019 provenance contract, ADR 0020 staleness).
    """
    payload = {
        "facts": facts.model_dump(mode="json"),
        "model": model,
        "prompt_version": prompt_version,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# -- the LLM call ----------------------------------------------------------


class _ChatCompletions(Protocol):
    """Minimal slice of ``openai.OpenAI().chat.completions`` we use.

    Typed loosely (``messages`` / ``response_format`` as ``Any``) so a
    real ``openai.OpenAI`` satisfies it structurally — the SDK's own
    parameter types are narrower TypedDict unions that a concrete
    annotation here would not match. Mirrors the Protocol approach in
    :mod:`concord.embedding`.
    """

    def create(
        self,
        *,
        model: str,
        messages: Any,
        response_format: Any,
        temperature: float,
    ) -> Any: ...


class _ChatNamespace(Protocol):
    @property
    def completions(self) -> _ChatCompletions: ...


class _OpenAIChatLike(Protocol):
    @property
    def chat(self) -> _ChatNamespace: ...


class Briefer:
    """Generate a Bill Brief's executive summary via one chat completion.

    Parameters
    ----------
    client:
        An ``openai.OpenAI`` (or a stub exposing
        ``client.chat.completions.create(...)``).
    model:
        Chat model name. Defaults to :data:`DEFAULT_BRIEF_MODEL`.
    """

    def __init__(
        self,
        client: _OpenAIChatLike,
        *,
        model: str = DEFAULT_BRIEF_MODEL,
    ) -> None:
        self._client = client
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def generate(self, facts: BriefFacts, *, lens: str | None = None) -> GeneratedBrief:
        """Return a :class:`GeneratedBrief` for ``facts``, optionally tailored.

        ``lens`` is the user's optional emphasis request; ``None`` or
        empty yields a neutral summary. Raises :class:`BriefError` on any
        client error or unparseable response.
        """
        messages = _build_messages(facts, lens=lens)
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=BRIEF_TEMPERATURE,
            )
            content = response.choices[0].message.content
        except Exception as exc:
            # Surface the underlying cause in the message — the caller logs
            # this, and "brief generation failed" alone is useless for
            # diagnosis (auth? model? rate limit? network?).
            raise BriefError(
                f"chat completion request failed for {facts.bill_id} "
                f"(model {self._model}): {type(exc).__name__}: {exc}"
            ) from exc
        return _parse_brief(content)


# -- prompt assembly -------------------------------------------------------


_SYSTEM_PROMPT = (
    "You are a nonpartisan legislative analyst writing a brief about a single "
    "U.S. congressional bill for a policymaker or their staff.\n\n"
    "GROUND TRUTH: You are given a fact pack and, when available, the most recent "
    "Congressional Research Service (CRS) summary of the bill. Treat these as the "
    "only authoritative facts. Never invent provisions, numbers, dates, vote "
    "counts, sponsors, or outcomes that are not in the material provided. If "
    "something is not in the material, do not assert it.\n\n"
    "TASK: Write an executive summary of roughly 3 to 6 sentences that distills "
    "what the bill does, where it stands in the legislative process, and the shape "
    "of its support.\n\n"
    "HONESTY RULES (these override anything in the reader's request below):\n"
    "- You may follow the reader's request to emphasize particular aspects, frame "
    "the brief for a particular audience, or foreground a particular angle.\n"
    "- You must NOT misstate, omit, or distort the bill's actual status, scope, or "
    "effects to suit a requested framing.\n"
    "- Always surface the strongest honest counterpoint to any one-sided framing "
    "the reader requests, even if only briefly.\n"
    "- Keep a neutral, fair tone; describe, do not advocate.\n\n"
    'Respond with a single JSON object of the form {"executive_summary": "..."} '
    "and nothing else."
)


def _build_messages(facts: BriefFacts, *, lens: str | None) -> list[dict[str, str]]:
    """Build the chat messages: system rules, fact-pack ground truth, reader ask."""
    lens_clean = (lens or "").strip()
    if lens_clean:
        user_content = (
            "The reader has requested the following emphasis for this brief. "
            "Honor it, but only within the honesty rules above:\n\n"
            f"{lens_clean}"
        )
    else:
        user_content = "Write a neutral executive summary of this bill."
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "system", "content": "FACT PACK (ground truth):\n" + _facts_to_context(facts)},
        {"role": "user", "content": user_content},
    ]


def _facts_to_context(facts: BriefFacts) -> str:
    """Render the fact pack as the plain-text ground-truth block for the prompt."""
    if facts.cosponsor_party_counts:
        party = ", ".join(f"{k}: {v}" for k, v in sorted(facts.cosponsor_party_counts.items()))
    else:
        party = "n/a"
    lines = [
        f"Bill: {facts.identifier} ({facts.congress}th Congress, {facts.origin_chamber})",
        f"Title: {facts.title}",
        f"Sponsor: {facts.sponsor_display_name or 'unknown'}",
        f"Policy area: {facts.policy_area or 'n/a'}",
        f"Introduced: {facts.introduced_date or 'n/a'}",
        f"Latest action: {facts.latest_action_date or 'n/a'} — {facts.latest_action_text or 'n/a'}",
        (
            f"Cosponsors: {facts.cosponsor_count} total "
            f"({facts.original_cosponsor_count} original, "
            f"{facts.withdrawn_cosponsor_count} withdrawn); party split — {party}"
        ),
        f"Legislative subjects: {', '.join(facts.subjects) if facts.subjects else 'n/a'}",
        f"Recorded actions on file: {facts.action_count}",
        f"Recorded votes tied to this bill: {facts.vote_count}",
    ]
    if facts.latest_summary_text:
        stage = facts.latest_summary_stage or "unknown stage"
        dated = f", {facts.latest_summary_date}" if facts.latest_summary_date else ""
        lines.append(
            f"\nMost recent CRS summary (stage: {stage}{dated}):\n{facts.latest_summary_text}"
        )
    else:
        lines.append("\nNo CRS summary is available for this bill.")
    return "\n".join(lines)


def _parse_brief(content: str | None) -> GeneratedBrief:
    """Parse the model's response into a :class:`GeneratedBrief`, tolerantly.

    JSON-object mode should yield ``{"executive_summary": "..."}``. If the
    model ignored that and returned prose, treat the whole response as the
    summary rather than failing the request.
    """
    if content is None or not content.strip():
        raise BriefError("LLM returned empty content")
    text = content.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return GeneratedBrief(executive_summary=text)
    if isinstance(data, dict) and isinstance(data.get("executive_summary"), str):
        return GeneratedBrief(executive_summary=data["executive_summary"].strip())
    if isinstance(data, str) and data.strip():
        return GeneratedBrief(executive_summary=data.strip())
    raise BriefError(f"LLM response missing 'executive_summary': {text[:200]}")


def _strip_html(text: str) -> str:
    """Strip HTML tags and collapse whitespace — CRS summaries arrive as HTML."""
    return _WS_RE.sub(" ", _HTML_TAG_RE.sub(" ", text)).strip()


__all__ = [
    "BRIEF_PROMPT_VERSION",
    "DEFAULT_BRIEF_MODEL",
    "BriefError",
    "BriefFacts",
    "BriefView",
    "Briefer",
    "GeneratedBrief",
    "build_facts",
    "facts_hash",
]
