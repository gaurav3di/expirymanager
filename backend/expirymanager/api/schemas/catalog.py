"""Request and response models for the catalog surface, API.md sections 3, 4 and 5.

Three route modules share these, which is why they live here rather than beside any one of them:
`underlyings.py` returns an `UnderlyingOut` that `expiries.py` validates its path id against, and
both the expiry list and the contract list carry the same coverage vocabulary.

Two conversions are done here once rather than at twenty call sites.

**Timestamps leave as strings.** DuckDB hands back naive `datetime` values that are already IST
wall clock, because that is how the ingest path stores them. Serialising them as `datetime` would
let pydantic attach a `Z` or a `+00:00` that is not true, and a chart that then shifted by five
and a half hours would look plausible and be wrong. `iso_text` formats them verbatim instead. The
one exception is `/contracts/{id}/bounds`, which is UTC **seconds** by design so the chart adapter
can clamp its window with no conversion at all.

**Decimals leave as floats.** `strike`, `tick_size` and the strike geometry are DECIMAL in the
store. JSON has no decimal, and a `Decimal` rendered through pydantic's default becomes a string,
which every arithmetic consumer in the frontend would then have to parse. Prices that must stay
exact are never carried by these models: candle prices go out through the bars route.

Nothing here hardcodes a market open, a market close or a session length. The catalog does not
know about sessions, and the moment it appears to it will be wrong: the NSE derivatives close
moved from 15:30 to 15:40 on 2026-08-03 and special sessions run on weekends.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from expirymanager.api.schemas.common import ApiModel, RequestModel

__all__ = [
    "DEFAULT_RESOLUTIONS",
    "MAX_QUERY_LENGTH",
    "iso_text",
    "as_float",
    "as_int",
    "UnderlyingOut",
    "ResolveRequest",
    "ResolveCandidate",
    "ResolveRejection",
    "ResolveProbe",
    "ResolveResponse",
    "CreateUnderlyingRequest",
    "UpdateUnderlyingRequest",
    "ExpiryCoverage",
    "ExpiryOut",
    "ExpiryPage",
    "DiscoverRequest",
    "DiscoverWindow",
    "DiscoverAccepted",
    "ContractResolution",
    "ContractOut",
    "ContractPage",
    "ContractCoverage",
    "ContractDetail",
    "BoundsResolution",
    "ContractBoundsOut",
]

# What a new underlying gets when the caller does not say. The same four codes the four builtin
# registry rows carry, so a user-added underlying behaves like a seeded one.
DEFAULT_RESOLUTIONS: tuple[str, ...] = ("1", "5", "15", "60")

MAX_QUERY_LENGTH = 64

# A life in days is bounded so a typo cannot turn one contract into a ten thousand request plan.
MIN_LIFE_DAYS = 1
MAX_LIFE_DAYS = 3650


def iso_text(value: Any) -> str | None:
    """A date or timestamp as the text the API documents, or None.

    No timezone is attached and none is stripped. The value in the store is IST wall clock and
    stating anything else about it here would be a claim this layer cannot back.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    return int(value)


# ---------------------------------------------------------------------------
# Section 3, underlyings
# ---------------------------------------------------------------------------


class UnderlyingOut(ApiModel):
    """One registered underlying: what the user declared, plus what the store actually holds.

    The declared half comes from `sqlite.underlying_registry`, which is authoritative for what
    was asked for. The held half comes from `dim_underlying`, its DuckDB mirror, which every
    catalog and chart join reads.

    `mirrored` exists because those two have been out of step before. On a fresh install the four
    seeded registry rows had no mirror row and nine separate joins answered empty with no error
    anywhere. A boolean on the row the user is looking at is cheaper than finding that out from a
    blank chart.
    """

    underlying_id: int
    fyers_symbol: str
    root: str
    exchange: str
    segment: str
    instrument_kind: str
    display_name: str
    data_from: str
    default_resolutions: list[str] = Field(default_factory=list)
    include_oi: bool = True
    option_life_days: int = 200
    future_life_days: int = 400
    spot_contract_id: int
    resolved_root_echo: str | None = None
    resolved_at: str | None = None
    is_builtin: bool = False
    is_active: bool = True

    mirrored: bool = False
    expiry_count: int = 0
    contract_count: int = 0
    first_expiry: str | None = None
    last_expiry: str | None = None
    spot_bars: int = 0
    spot_last_ts: str | None = None


def _clean_resolutions(value: list[str]) -> list[str]:
    seen: list[str] = []
    for item in value:
        code = str(item).strip().upper()
        if not code:
            raise ValueError("a resolution code cannot be blank")
        if code not in seen:
            seen.append(code)
    if not seen:
        raise ValueError("at least one resolution is needed")
    return seen


def _clean_symbol(value: str) -> str:
    symbol = str(value).strip().upper()
    if ":" not in symbol:
        raise ValueError("a Fyers symbol is written as EXCHANGE:TICKER, for example NSE:SBIN-EQ")
    exchange, _, body = symbol.partition(":")
    if not exchange or not body:
        raise ValueError("a Fyers symbol is written as EXCHANGE:TICKER, for example NSE:SBIN-EQ")
    return symbol


class ResolveRequest(RequestModel):
    """A cash ticker fragment, a derivative root, or a full Fyers symbol."""

    query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_LENGTH)]


class ResolveCandidate(ApiModel):
    """One derivative root resolved to the cash ticker the expired endpoints accept.

    Nothing in a derivative symbol names its cash ticker: BANKNIFTY is quoted as
    NSE:NIFTYBANK-INDEX. `under_fytoken` is the join that knows that, and it is returned so the
    caller can see the resolution was data and not a guess.
    """

    fyers_symbol: str
    root: str
    exchange: str
    segment: str
    instrument_kind: str
    display_name: str
    under_fytoken: str
    fo_contract_count: int = 0
    source: str = "symbol_master"
    already_registered: bool = False


class ResolveRejection(ApiModel):
    """A root that matched the query and cannot be registered, with the reason.

    Returned rather than dropped. The measured case is the 360ONE class: a root that starts with
    a digit, which neither the root registry nor the symbol parser accepts today. Hiding it would
    let a user add an underlying that fails much later, inside the pipeline, with a parse error
    nobody can trace back to this screen.
    """

    root: str
    exchange: str
    reason: str
    fo_contract_count: int = 0


class ResolveProbe(ApiModel):
    """The one governed expiry-dates request, and what it echoed back.

    `root_echo` is `data.symbol` from the response and is the authoritative root. It is the only
    confirmation that the cash ticker the symbol master pointed at is the underlying the F and O
    contracts are actually filed under.
    """

    attempted: bool = False
    symbol: str | None = None
    root_echo: str | None = None
    expiry_count: int | None = None
    range_from: str | None = None
    range_to: str | None = None
    reason: str | None = None


class ResolveResponse(ApiModel):
    candidates: list[ResolveCandidate] = Field(default_factory=list)
    rejected: list[ResolveRejection] = Field(default_factory=list)
    probe: ResolveProbe = Field(default_factory=ResolveProbe)


class CreateUnderlyingRequest(RequestModel):
    """Register an underlying. `fyers_symbol` is the cash ticker `/resolve` returned."""

    fyers_symbol: str
    display_name: str | None = Field(default=None, max_length=120)
    default_resolutions: list[str] = Field(default_factory=lambda: list(DEFAULT_RESOLUTIONS))
    include_oi: bool = True
    option_life_days: int = Field(default=200, ge=MIN_LIFE_DAYS, le=MAX_LIFE_DAYS)
    future_life_days: int = Field(default=400, ge=MIN_LIFE_DAYS, le=MAX_LIFE_DAYS)

    @field_validator("fyers_symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        return _clean_symbol(value)

    @field_validator("default_resolutions")
    @classmethod
    def _resolutions(cls, value: list[str]) -> list[str]:
        return _clean_resolutions(value)


class UpdateUnderlyingRequest(RequestModel):
    """A partial update. `fyers_symbol`, `root` and `spot_contract_id` are immutable.

    They are immutable because `spot_contract_id` is already the primary key of a row in
    `candles`, and `root` is baked into every contract symbol already parsed under it. Changing
    either would orphan data rather than rename it.
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    default_resolutions: list[str] | None = None
    include_oi: bool | None = None
    option_life_days: int | None = Field(default=None, ge=MIN_LIFE_DAYS, le=MAX_LIFE_DAYS)
    future_life_days: int | None = Field(default=None, ge=MIN_LIFE_DAYS, le=MAX_LIFE_DAYS)
    is_active: bool | None = None

    @field_validator("default_resolutions")
    @classmethod
    def _resolutions(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_resolutions(value)

    @model_validator(mode="after")
    def _not_empty(self) -> "UpdateUnderlyingRequest":
        if not self.model_fields_set:
            raise ValueError("a patch needs at least one field")
        return self


# ---------------------------------------------------------------------------
# Section 4, expiries
# ---------------------------------------------------------------------------


class ExpiryCoverage(ApiModel):
    """What the coverage ledger records for one expiry.

    Every number here is read from `candle_coverage` and `contract_bounds`, which is the same
    ledger the planner subtracts from when it prices a sheet. It is deliberately not recomputed
    from `candles`: two definitions of "already held" is how a download sheet ends up offering to
    re-fetch data the planner will then skip, and a count over `candles` on every visit to this
    screen is a scan of hundreds of millions of rows.

    `chunks_error` is reported rather than the `chunks_missing` API.md sketched. The ledger holds
    one row per fetched window, so it can count windows that came back as errors; it cannot count
    windows nobody ever asked for. `contracts_without_data` is the honest version of absence.
    """

    contracts_with_data: int = 0
    contracts_without_data: int = 0
    contracts_sealed: int = 0
    chunks_ok: int = 0
    chunks_empty: int = 0
    chunks_error: int = 0
    rows: int = 0


class ExpiryOut(ApiModel):
    expiry_id: int
    expiry_date: str
    expiry_dow: int | None = None
    has_futures: bool = False
    has_options: bool = False
    futures_count: int | None = None
    options_count: int | None = None
    contract_count: int | None = None
    expiry_cycle: str | None = None
    expiry_cycle_source: str | None = None
    is_last_of_month: bool | None = None
    discovered_at: str | None = None
    contracts_discovered_at: str | None = None
    contract_id_lo: int | None = None
    contract_id_hi: int | None = None
    min_strike: float | None = None
    max_strike: float | None = None
    strike_step: float | None = None
    coverage: ExpiryCoverage = Field(default_factory=ExpiryCoverage)


class ExpiryPage(ApiModel):
    items: list[ExpiryOut] = Field(default_factory=list)
    next_cursor: str | None = None


class DiscoverRequest(RequestModel):
    """Ask which expiries exist over a range. One request per 366 day window."""

    range_from: date
    range_to: date

    @model_validator(mode="after")
    def _ordered(self) -> "DiscoverRequest":
        if self.range_to < self.range_from:
            raise ValueError("range_to precedes range_from")
        return self


class DiscoverWindow(ApiModel):
    """One expiry-dates request the job will make. One window is one Fyers request."""

    range_from: str
    range_to: str


class DiscoverAccepted(ApiModel):
    """The 202 body. `windows` is what the job will actually ask for after clamping."""

    job_id: str
    kind: str = "expiry_discovery"
    status: str = "queued"
    total_tasks: int = 0
    est_requests: int = 0
    range_from: str
    range_to: str
    clamped: bool = False
    windows: list[DiscoverWindow] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Section 5, contracts
# ---------------------------------------------------------------------------


class ContractResolution(ApiModel):
    """One resolution held for a contract, with its bounds as IST wall clock text."""

    res_id: int
    fyers_code: str | None = None
    chart_interval: str | None = None
    rows: int = 0
    first_ts: str | None = None
    last_ts: str | None = None


class ContractOut(ApiModel):
    contract_id: int
    fyers_symbol: str
    underlying_id: int
    kind: str
    instrument_class: str | None = None
    expiry_date: str | None = None
    strike: float | None = None
    strike_raw: str | None = None
    option_type: str | None = None
    lot_size: int | None = None
    tick_size: float | None = None
    fytoken: str | None = None
    symbol_expiry_encoding: str | None = None
    expiry_cycle: str | None = None
    parse_confidence: str | None = None
    sealed_at: str | None = None
    rows: int = 0
    resolutions: list[ContractResolution] = Field(default_factory=list)


class ContractPage(ApiModel):
    items: list[ContractOut] = Field(default_factory=list)
    next_cursor: str | None = None


class ContractCoverage(ApiModel):
    """The ledger summary for one contract at one resolution."""

    res_id: int
    chunks: int = 0
    chunks_ok: int = 0
    chunks_empty: int = 0
    chunks_error: int = 0
    have_from: str | None = None
    have_to: str | None = None
    rows: int = 0


class ContractDetail(ContractOut):
    """The full contract row plus its bounds and its coverage summary."""

    expiry_id: int | None = None
    root: str | None = None
    exchange: str | None = None
    segment: str | None = None
    underlying_symbol: str | None = None
    underlying_name: str | None = None
    underlying_kind: str | None = None
    exchange_token: int | None = None
    isin: str | None = None
    qty_freeze: int | None = None
    qty_multiplier: float | None = None
    trading_session: str | None = None
    symbol_description: str | None = None
    parsed_expiry_date: str | None = None
    parse_method: str | None = None
    parse_warnings: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    coverage: list[ContractCoverage] = Field(default_factory=list)


class BoundsResolution(ApiModel):
    """First and last bar for one resolution, as UTC seconds.

    Seconds rather than text because this is what the chart adapter clamps its window with, and
    every conversion between here and there is a chance to be five and a half hours wrong. It is
    also the single lookup that stops an expired contract rendering as "No bars": the chart's
    default window is the live present, and an expired contract has nothing there.
    """

    res_id: int
    fyers_code: str | None = None
    chart_interval: str | None = None
    first_ts: int | None = None
    last_ts: int | None = None
    rows: int = 0


class ContractBoundsOut(ApiModel):
    contract_id: int
    fyers_symbol: str | None = None
    resolutions: list[BoundsResolution] = Field(default_factory=list)


# Declared so a route can name the literal in its signature rather than restate the tuple.
SortField = Literal["expiry_date", "strike", "fyers_symbol", "rows"]
SortDirection = Literal["asc", "desc"]
