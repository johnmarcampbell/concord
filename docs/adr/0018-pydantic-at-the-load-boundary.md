# 0018 — Pydantic validation at the load boundary

**Status**: Proposed, 2026-05-29.

## Context

The pipeline already has Pydantic models for the mutable entities — `Bill`, `Cosponsor`, `BillAction`, `BillSubject`, `BillTitle`, `BillSummary` in `src/concord/models/bills.py`; `Member`, `Term` in `src/concord/models/members.py`; `Vote`, `VotePosition`, `ParsedVoteDetail`, `ParsedVotePosition` in `src/concord/models/votes.py`. (The `Parsed*` names predate the endpoint-naming rule introduced below; they are renamed as part of this ADR — see Rule 4.) Each carries (or is acquiring) a `from_congress_api(payload)` classmethod that parses the upstream contract into a validated instance.

What the codebase doesn't have is a consistent rule for *where* validation runs, what those models are *named*, or how the pieces relate across pipeline stages. Three concrete inconsistencies motivate this ADR:

1. **Validation is partially at the load boundary, partially nowhere.** `pipeline/load_bills.py` and `pipeline/load_members.py` read JSONL envelopes as raw `dict[str, Any]`, hand-extract `payload`, then pass it to `from_congress_api`. The envelope itself (`{fetched_at, key, payload}` per [ADR 0006](./0006-snapshot-on-fetch-for-mutable-entities.md)) is never validated, even though `BillSnapshot` and `MemberSnapshot` exist as Pydantic types in the same modules. Vote loaders are the exception — they use `VoteSnapshot` end-to-end. The web read path never validates: `sqlite3.Row` becomes `dict[str, Any]` in `web/search.py` and flows straight into Jinja templates.

2. **Scrapers do trace-amounts of pre-validation.** `scraper/members.py:82-86` skips a record when `bioguideId` is missing, with a comment that says "skip rather than write an un-loadable line." The actual blocker is that an envelope `key` (`{bioguide_id, congress}`) cannot be constructed without an ID — a legitimate concern — but the framing leaks loader logic into the scraper. The skip is also silent: a routine upstream dropout produces no log signal.

3. **`Bill` the class is doing double duty.** It parses the wire shape of `/v3/bill/{c}/{t}/{n}` (identity + sponsor + `latestAction` + `policyArea`) and sits in the same module as `Cosponsor`, `BillAction`, etc. — wire shapes of *other* endpoints describing the same underlying Bill ([ADR 0009](./0009-multi-endpoint-entities-split-jsonl.md)). No name distinguishes "the formal record for the `bills` table" from "the conceptual Bill that all six endpoints together describe." Member and Vote don't suffer the same overload — they each have effectively one primary wire shape — but the rule for naming wire shapes needs to be settled before another multi-endpoint entity (Committees, Amendments) lands.

The earlier ADRs that constrain this space:

- [ADR 0002](./0002-jsonl-as-canonical-raw-store.md) — JSONL is the source of truth; SQLite is rebuildable.
- [ADR 0006](./0006-snapshot-on-fetch-for-mutable-entities.md) — the envelope `{fetched_at, key, payload}` for mutable entities.
- [ADR 0007](./0007-parallel-pipelines-per-entity.md) — stages are modules, not classes; no base-class hierarchies across entities.
- [ADR 0009](./0009-multi-endpoint-entities-split-jsonl.md) — multi-endpoint entities split into one JSONL per sub-endpoint, identically keyed.

The rule for *what a model is, where it lives, what its constructor looks like, and how scrapers behave around it* belongs in an ADR rather than being re-derived per entity.

## Decision

Four rules and a vocabulary.

### Rule 1 — Validation lives at the JSONL read boundary

Every loader call site that reads a JSONL line validates the full envelope (not just the payload) through a Pydantic model. The API client (`concord.api.Client`) continues to return `dict[str, Any]`, and the scraper writes raw payloads to JSONL unchanged. The scraper does not Pydantic-validate the payload.

This places exactly one validation step per record per pipeline run: at load time, against the raw payload on disk. A model fix is a re-load away, not a re-scrape away — preserving the recovery story [ADR 0002](./0002-jsonl-as-canonical-raw-store.md) was built around.

### Rule 2 — The factory is a `@classmethod` returning `Self`

The sole sanctioned constructor for a wire-shape model is:

```python
@classmethod
def from_congress_api(cls, payload: dict[str, Any]) -> Self:
    ...
```

Non-Congress sources use `from_<source>` (e.g. `SenateVoteDetail.from_senate_xml`). Module-level parser functions, builder classes, and `@model_validator(mode='before')` hooks are not substitutes. Collocating the constructor with the model in one class body is the discoverability we are paying for; the `Self` return type (PEP 673) carries subclass identity through mypy strict without manual annotation. Existing factories that return `"ClassName"` (forward-ref string) migrate to `Self`.

### Rule 3 — Wire-shape and domain models

A **wire-shape model** is a Pydantic model whose structure mirrors a single response from a source the project does not control. Field names, optionality, and nesting follow the upstream contract verbatim, with two cosmetic adaptations allowed:

- camelCase → snake_case via `alias_generator` (configuration, not logic).
- Pydantic's built-in type parsing for JSON-native types (`str → date`, `int → int`) (validation, not coercion as business logic).

Custom `@field_validator` semantic shims — empty-string-to-None, two encodings collapsed to one, defaults invented for fields the API omits — do **not** belong on a wire-shape model. That work is canonicalization, and it lives downstream.

A **domain model** is the Pydantic model the rest of the codebase operates on after any normalization. Two cases:

- **Wire shape already fits.** The wire-shape model *is* the domain model — one class plays both roles. This is the common case: `BillDetail`, `Member`, House `Vote`. No symmetry-for-its-own-sake duplicate class.
- **Wire shape is awkward.** A separate domain model is defined, and the wire-shape model is projected into it via a plain function (not a Pydantic validator). The Senate vote path is the worked example: `SenateVoteDetail` (XML wire shape) projects to `Vote` (domain model) via `_vote_from_parsed_detail` in `pipeline/load_votes.py` (function name to be updated alongside the rename).

The same rule applies to the per-Member sub-rows: `SenateVotePosition` (XML wire shape) projects to `VotePosition` (domain model) once Bioguide IDs are resolved.

### Rule 4 — Naming follows the endpoint, not the concept

Wire-shape models are named after the endpoint that produced them. Two renames fall out of this:

- The existing `Bill` class — which parses `/v3/bill/{c}/{t}/{n}` — is renamed to **`BillDetail`**.
- The existing `Cosponsor` class — which parses one item from `/v3/bill/{c}/{t}/{n}/cosponsors` — is renamed to **`BillCosponsor`** for parallel with its peers `BillAction`, `BillSubject`, `BillTitle`, `BillSummary`. The bare `Cosponsor` carried enough domain weight to read naturally on its own, but the parallel to the other four sub-resource models is the stronger pull: a reader scanning `models/bills.py` should see a uniform `Bill*` family, and the `Bill*` prefix makes the source endpoint unambiguous at the import site.

The unqualified noun "Bill" describes the aggregate domain concept (all six endpoints together) and is not bound to any class. The unqualified noun "Cosponsor" stays in CONTEXT.md as a domain term; code refers to `BillCosponsor`.

`Member` and `Vote` do not need renames at the domain layer. Member has effectively one primary wire shape (the list endpoint returns the full payload); `Member` is unambiguous. Vote already disambiguates `Vote` (domain) and `VotePosition` (per-Member position).

The Senate vote wire shapes do need renames. The current `ParsedVoteDetail` and `ParsedVotePosition` use a `Parsed*` prefix that names the implementation (these are the post-XML-parse shapes), not the source. Under the endpoint-naming rule they become **`SenateVoteDetail`** (the per-roll vote XML at `…/roll_call_votes/vote_{c}_{s}_{roll5}.xml`) and **`SenateVotePosition`** (one per-Member position item inside it). The `Senate` prefix does the disambiguation-from-House work the `Parsed` prefix was doing, without leaking implementation into the type name. House votes have no parallel `HouseVoteDetail` because the wire shape and domain shape coincide — `Vote.from_congress_api(payload)` parses House JSON directly.

### Vocabulary — the envelope: `Snapshot[T]`

The ADR 0006 envelope is implemented as a Pydantic generic:

```python
class Snapshot(BaseModel, Generic[T]):
    fetched_at: datetime
    key: dict[str, Any]
    payload: T
```

Loaders read JSONL via `Snapshot[BillDetail]`, `Snapshot[Cosponsor]`, `Snapshot[Member]`, etc. The existing `MemberSnapshot`, `BillSnapshot`, `VoteSnapshot`, and `VotePositionsSnapshot` classes are deleted, not aliased — `Snapshot[BillDetail]` carries its payload type at the import site, where an alias would silently bind one of six possible sub-endpoint snapshots to a single ambiguous name (`BillSnapshot`). The word "envelope" stays in prose to describe the on-disk shape; the class is `Snapshot[T]`.

### Scraper skip rule

Scrapers may skip a record **only** when they cannot construct the envelope's `key`: no Bioguide ID for a Member, no roll number for a Vote, no bill number for a Bill. They do not schema-validate the payload, do not skip records whose payload looks wrong, and do not drop fields they don't recognize. The rule has a sharp boundary: anything beyond "I literally cannot make the key" is the loader's call.

Every skip emits a `WARN`-level log record naming the source endpoint, the missing field, and a small identifying payload fragment (not the whole payload — Bill payloads run kilobytes):

```python
logger.warning(
    "scraper.skip.missing_key",
    extra={
        "source": "congress.gov/v3/member/congress",
        "missing": "bioguideId",
        "fragment": {"name": payload.get("name"), "state": payload.get("state")},
    },
)
```

WARN, not INFO: the cause is an upstream contract violation, and routine occurrence is signal a maintainer should notice.

## Consequences

**Trade-offs accepted:**

- **Renaming `Bill → BillDetail` touches several modules.** `models/bills.py`, `pipeline/load_bills.py`, `storage/sqlite.py`, `web/`, tests. Mechanical but visible in diff. Paired with the broader refactor in one PR so the disruption is one review.
- **Re-loading SQLite on a model fix is the assumed recovery path.** When a `ValidationError` surfaces in a `Snapshot[BillDetail]` parse, the right move is to harden the model and re-run Stage 1 — not to soften the model or patch the JSONL. The contract is that JSONL stores what the API sent, full stop.
- **Domain-model projection adds a step when wire and domain shapes differ.** Today only Senate votes need it. If a future entity does, the loader grows a projection helper — a plain function, per [ADR 0007](./0007-parallel-pipelines-per-entity.md)'s no-base-class stance.
- **`Snapshot[T]` parses the full envelope including payload on every load.** Today the Bill loader treats the envelope as a dict and only parses the payload when it has decided to use it. Validating the whole envelope eagerly adds Pydantic cost per line. At v1 scale (≤50K lines per file) the cost is well under a second per loader run; acceptable.

**Things this buys:**

- **Validation gaps close.** The web layer's "row-dict-into-template" pattern is still permitted at the web boundary, but the *parse* into a domain model is now guaranteed to have happened upstream. A renamed column or a missing field surfaces at Stage 1, not at page render.
- **The naming question is settled.** `BillDetail` is what `/v3/bill/{c}/{t}/{n}` returns; `Cosponsor` is what `/cosponsors` returns. New multi-endpoint entities (Committees, Amendments) inherit the rule with no further discussion.
- **Scraper failures are observable.** The current `members.py` skip is silent; under the rule, an upstream Bioguide ID dropout would log at WARN every time it fires. A monitoring story for "is our raw store dropping records?" becomes possible.
- **The Senate vote pattern generalizes.** `ParsedVoteDetail → Vote` is no longer an idiosyncratic special case but the canonical example of "wire shape projects to domain model when the upstream shape is awkward."
- **`Snapshot[T]` collapses three near-duplicate classes into one generic.** `MemberSnapshot`, `BillSnapshot`, `VoteSnapshot`, `VotePositionsSnapshot` all carried the same `{fetched_at, key, payload}` shape with different payload types. One generic carries them all and makes the payload type explicit at the import site.

**What stays open:**

- **Scraper logging configuration.** The rule mandates WARN-level log records but doesn't specify a sink. Today the project uses `logging` via the standard library; whether to add structured JSON output or a dedicated drops file is a follow-up.
- **The `_fetch_and_write_members` best-effort pattern in `scraper/votes.py:286-299`.** It silently swallows a positions-fetch failure under the principle "re-run picks it up." This ADR doesn't decide whether that stays as-is or upgrades to a WARN log. Likely worth a follow-up.
- **Compaction interaction with `Snapshot[T]`.** [ADR 0006](./0006-snapshot-on-fetch-for-mutable-entities.md) leaves compaction open. Reading a compacted file through `Snapshot[T]` works unchanged; nothing in this ADR forecloses any compaction shape.
- **Whether `from_congress_api` factories take additional context kwargs.** Today `Term.from_congress_api(payload, *, congress=...)` and `Vote.from_congress_api(payload, *, chamber=...)` take extra context that isn't on the payload itself. The rule allows this — context that lives in the envelope's `key`, not the payload, has to come in from somewhere — but the project may want a tighter convention for which kwargs are permitted.

## Rejected — Pydantic at the API client boundary

An earlier draft put the factory on `concord.api.Client` itself: `client.get_bill_detail(...) -> BillDetail`. Rejected because:

1. **It contradicts [ADR 0002](./0002-jsonl-as-canonical-raw-store.md).** JSONL is the source of truth, and the contract is "every fetch appends a line." A model fix would require re-fetching every previously-scraped record from the live API instead of re-reading the JSONL on disk. The api.data.gov rate limit (5,000 req/hr) would make routine model evolution expensive in ways re-loading SQLite is not.
2. **It pushes validation before persistence.** A `ValidationError` at the API client would either drop the record or require a fallback raw write — either way, the JSONL would no longer faithfully mirror what the API returned. That mirror is what makes the recovery story work.

Pydantic at the load boundary keeps validation strict without coupling it to the API call.

## Rejected — per-entity snapshot subclasses (or aliases) of `Snapshot[T]`

Keeping `MemberSnapshot`, `BillSnapshot`, `VoteSnapshot` as named subclasses or aliases of `Snapshot[T]` would preserve existing import paths. Rejected because:

1. **`BillSnapshot` is ambiguous post-rename.** A Bill has six sub-endpoints ([ADR 0009](./0009-multi-endpoint-entities-split-jsonl.md)), each with its own envelope on disk. A single `BillSnapshot` alias silently binds the name to one of them — the detail endpoint — without disambiguation at the use site. `Snapshot[BillDetail]` makes the payload type explicit.
2. **Aliases drift.** Two years on, a method added to one alias-but-not-the-generic produces subtle non-equivalence. The fewer parallel type names the codebase carries, the smaller that surface.
3. **Migration cost is one PR.** No transition period justifies a long-lived alias.

## Rejected — base + per-stage subclass hierarchy

A `MemberBase` with `MemberFromAPI`, `MemberDB`, `MemberView` subclasses was considered as a way to make the wire/domain/view split visible per entity. Rejected because:

1. **Stage-specific fields don't cleanly partition.** ~95 % of the fields are shared; the differences cleave by feature (joins, computed fields) not by pipeline stage. Subclassing by stage produces near-identical classes with near-identical fields.
2. **It contradicts [ADR 0007](./0007-parallel-pipelines-per-entity.md).** That ADR explicitly rejects base-class hierarchies as a way to "DRY up" parallel entity pipelines. The same reasoning applies here: shared shape is not a license to introduce shared parents.
3. **Wire-shape / domain / view is the actual distinction we want.** Those three roles can be played by one class or by separate classes per entity, decided per case. A blanket three-class hierarchy is more structure than the project needs.

## Rejected — renaming "canonical raw store" to free up "canonical"

The word "canonical" appears in CONTEXT.md in four distinct senses already ("canonical actor entity", "Canonical form is lowercase", "Canonical raw store", and "canonical output record"). An earlier draft of this ADR used "canonical model" for what is now "domain model" and proposed renaming "canonical raw store" → "raw store" to free up the word. Rejected because:

1. **"Canonical raw store" is baked into [ADR 0002](./0002-jsonl-as-canonical-raw-store.md), its filename, and three places in CONTEXT.md.** Renaming it has larger blast radius than renaming a still-uncoined term.
2. **"Domain model" is industry-standard (DDD, EAI patterns).** CONTEXT.md is already framed as a "domain glossary" per `CLAUDE.md`; the word "domain" is loaded with the right meaning in this project.
3. **The wire/domain pairing reads cleanly.** "Wire-shape model vs domain model" maps directly to "external contract vs internal shape," which is exactly the distinction.
