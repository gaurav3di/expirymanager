"""Response models for the chart and option chain routes, API.md section 7.

Two shapes live here and they are deliberately different from each other.

Bars are columnar. `columns` names the fields and `candles` carries positional rows in exactly
the order Fyers returns them, so the browser maps by the `columns` array the same way the ingest
path does and adding open interest, or greeks later, costs neither side a change. A row of seven
numbers is also roughly a fifth of the bytes of seven key/value pairs, and a chart window is tens
of thousands of rows.

The chain is object shaped, because it is read one strike at a time by a human and one strike at
a time by the backtester, and because the interesting fact about a strike is which of its two
legs exist, which a columnar layout hides.

Every timestamp on the way out is UTC epoch seconds, the integer `Bar.time` the chart library
requires. The store keeps naive IST wall clock and the conversion happens in DuckDB, once per
query rather than once per row.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from expirymanager.api.schemas.common import ApiModel

__all__ = [
    "BarsResponse",
    "OpenInterestResponse",
    "ChainLegModel",
    "ChainRowModel",
    "ChainResponse",
    "ChainAtmResponse",
]


class BarsResponse(ApiModel):
    """The columnar bars payload.

    `from_ts` and `to_ts` are the window that was actually served, which is not always the window
    that was asked for: see `clamped`. They are reported so a screen can say what it is showing
    instead of implying that the caller's own window held these bars.
    """

    contract_id: int | None = None
    symbol: str | None = None
    resolution: str
    res_id: int
    columns: list[str] = Field(default_factory=list)
    candles: list[list[Any]] = Field(default_factory=list)
    from_ts: int | None = None
    to_ts: int | None = None
    first_ts: int | None = None
    last_ts: int | None = None
    clamped: bool = False


class OpenInterestResponse(ApiModel):
    """`[[timestamp, open_interest]]` for the Tier 2 indicator.

    Separate from the bars payload because the chart library's `Bar` has no open interest field,
    so the indicator reads its own series. The window is clamped identically to the bars window,
    otherwise the indicator would render empty under a chart that is showing candles.
    """

    contract_id: int | None = None
    resolution: str
    res_id: int
    points: list[list[Any]] = Field(default_factory=list)
    from_ts: int | None = None
    to_ts: int | None = None
    clamped: bool = False


class ChainLegModel(ApiModel):
    """One right of one strike at one timestamp.

    Carries the full bar rather than just the close. The close is what the grid renders, but the
    open, high and low are already in the row the query read, and a backtester that has to make a
    second request for them would run one request per strike per bar.
    """

    contract_id: int
    fyers_symbol: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None
    oi: int | None = None


class ChainRowModel(ApiModel):
    """One strike, both rights, and whether this is the at the money row.

    `is_atm` is an explicit flag rather than something the caller derives by comparing `strike`
    with `atm_strike`. Strikes cross the wire as JSON doubles, so a derived comparison is an
    equality test on a float, and getting it wrong highlights the wrong row silently.
    """

    strike: float
    lot_size: int | None = None
    is_atm: bool = False
    ce: ChainLegModel | None = None
    pe: ChainLegModel | None = None


class ChainResponse(ApiModel):
    """The chain at one instant.

    `ts` is the bar the slice was taken from, which is the requested instant snapped back to the
    most recent bar at or before it. A caller that asked for 11:17:30 on a one minute series gets
    the 11:17 bar and is told so, rather than an empty chain.
    """

    underlying_id: int
    expiry_date: str
    resolution: str
    res_id: int
    ts: int | None = None
    requested_ts: int | None = None
    spot: float | None = None
    atm_strike: float | None = None
    rows: list[ChainRowModel] = Field(default_factory=list)


class ChainAtmResponse(ApiModel):
    """Just the at the money pick, for the caller that wants the pair and not the grid."""

    underlying_id: int
    expiry_date: str
    resolution: str
    res_id: int
    ts: int | None = None
    requested_ts: int | None = None
    spot: float | None = None
    atm_strike: float | None = None
    ce_contract_id: int | None = None
    pe_contract_id: int | None = None
