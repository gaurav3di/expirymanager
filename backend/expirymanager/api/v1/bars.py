"""`GET /bars`, `/bars/before`, `/bars/oi` and `/spot/bars`: everything the chart reads.

API.md section 7. The whole module exists to satisfy one contract, the `DataFeed.getBars` the
chart library defines, and one property that contract does not state but every screen depends on.

The property is the window clamp. `openalgo-charts` builds its first request as a now relative
span: `lookbackBars` multiplied by the interval, counted back from the present. Every contract
this application serves is expired, so that span lands months after the last bar the contract
ever printed, the query matches nothing, and the widget renders its empty state with no warning
and no placeholder. So a window that runs past the contract's own last bar is slid back to end
there, keeping its length, and a window that starts before the contract's first bar is pulled
forward to start there. `clamped` on the response says it happened, so a screen can label what it
is showing rather than implying the caller asked for it.

The clamp reads `contract_bounds`, which the writer refreshes inside the same transaction that
inserts candles. It is not a scan of `candles` and it is not a guess from the expiry date.

Three conventions, all of them from the research note on the chart library:

- Time on the wire is integer UTC seconds. Not milliseconds, which silently produce a chart
  labelled tens of thousands of years out, and not a string.
- Rows are positional, in Fyers column order, read through the `columns` array.
- Bars are ascending, one per timestamp. Both are properties of the query, not of this module.

Nothing here knows when a session opens or closes. A window is bounded by observed data, and the
only two facts that bound it are the contract's own first and last bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query

from expirymanager.api.deps import CurrentUserDep, ReaderDep
from expirymanager.api.errors import ApiError, CODE_VALIDATION_ERROR
from expirymanager.api.schemas.bars import BarsResponse, OpenInterestResponse
from expirymanager.db.queries import bars as bars_q
from expirymanager.db.queries import catalog as catalog_q
from expirymanager.db.reader import DuckReader

__all__ = [
    "router",
    "CODE_UNKNOWN_CONTRACT",
    "CODE_UNKNOWN_RESOLUTION",
    "CODE_RANGE_TOO_LARGE",
    "CODE_UNKNOWN_UNDERLYING",
    "DEFAULT_RESOLUTION",
    "Resolution",
    "ContractRef",
    "Window",
    "normalise_interval",
    "resolve_resolution",
    "resolve_contract",
    "clamp_window",
]

router = APIRouter()

CODE_UNKNOWN_CONTRACT = "unknown_contract"
CODE_UNKNOWN_RESOLUTION = "unknown_resolution"
CODE_RANGE_TOO_LARGE = "range_too_large"
CODE_UNKNOWN_UNDERLYING = "unknown_underlying"

# The resolution a caller gets when it names none. One minute is the only resolution the
# downloader fetches from Fyers, so it is the only one that is always populated, and defaulting
# to anything else would answer a bare request with an empty chart.
DEFAULT_RESOLUTION = "1"

# The bare unit tokens the chart library accepts, which mean one of that unit. Upper case M is
# absent on purpose: the library reads it as a calendar month and refuses to treat it as minutes,
# and silently answering it with one minute bars is exactly the bug it refuses to have.
_BARE_INTERVAL_UNITS = frozenset({"s", "m", "h", "d", "w"})


@dataclass(frozen=True, slots=True)
class Resolution:
    """One row of `dim_resolution`, resolved from whichever code the caller used."""

    res_id: int
    fyers_code: str
    chart_interval: str
    seconds: int


@dataclass(frozen=True, slots=True)
class ContractRef:
    """A contract and the bounds of one of its series, all in UTC seconds."""

    contract_id: int
    fyers_symbol: str | None
    first_ts: int | None
    last_ts: int | None

    @property
    def has_bars(self) -> bool:
        return self.first_ts is not None and self.last_ts is not None


@dataclass(frozen=True, slots=True)
class Window:
    """The window that will actually be queried, in UTC seconds."""

    from_ts: int | None
    to_ts: int | None
    clamped: bool

    @property
    def is_empty(self) -> bool:
        return self.from_ts is None or self.to_ts is None or self.from_ts > self.to_ts


def _missing(message: str) -> ApiError:
    return ApiError(422, CODE_VALIDATION_ERROR, message)


def normalise_interval(code: str) -> str | None:
    """A chart interval token to the `chart_interval` spelling `dim_resolution` stores.

    `1m` is already the stored spelling. A bare unit such as `D` or `W` means one of that unit and
    becomes `1d` or `1w`. Upper case `M` returns None, because the library treats it as a calendar
    month rather than as minutes and there is no monthly series in this store.
    """
    token = code.strip()
    if not token or token == "M":
        return None
    lowered = token.lower()
    if lowered in _BARE_INTERVAL_UNITS:
        return f"1{lowered}"
    return lowered


async def resolve_resolution(
    reader: DuckReader, *, resolution: str | None = None, interval: str | None = None
) -> Resolution:
    """Look a resolution up by its Fyers code or by its chart interval code.

    Both spellings reach this application: the planner and every download route speak Fyers codes,
    the chart speaks its own interval tokens. `dim_resolution` holds both columns, so this is one
    lookup rather than a map in Python that can drift from the table.
    """
    if resolution:
        row = await reader.fetch_one(
            "SELECT res_id, fyers_code, chart_interval, seconds FROM dim_resolution "
            " WHERE upper(fyers_code) = upper(?)",
            [resolution.strip()],
        )
        given = resolution
    elif interval:
        normalised = normalise_interval(interval)
        row = (
            None
            if normalised is None
            else await reader.fetch_one(
                "SELECT res_id, fyers_code, chart_interval, seconds FROM dim_resolution "
                " WHERE lower(chart_interval) = ?",
                [normalised],
            )
        )
        given = interval
    else:
        row = await reader.fetch_one(
            "SELECT res_id, fyers_code, chart_interval, seconds FROM dim_resolution "
            " WHERE fyers_code = ?",
            [DEFAULT_RESOLUTION],
        )
        given = DEFAULT_RESOLUTION
    if row is None:
        raise ApiError(
            400,
            CODE_UNKNOWN_RESOLUTION,
            f"{given!r} is not a resolution this application stores.",
        )
    return Resolution(
        res_id=int(row[0]),
        fyers_code=str(row[1]),
        chart_interval=str(row[2]),
        seconds=int(row[3]),
    )


def _bounds_for(bounds: dict[str, Any], res_id: int) -> tuple[int | None, int | None]:
    for entry in bounds["resolutions"]:
        if int(entry["res_id"]) == res_id:
            first = entry["first_ts"]
            last = entry["last_ts"]
            return (
                None if first is None else int(first),
                None if last is None else int(last),
            )
    return (None, None)


async def _contract_id_for_symbol(reader: DuckReader, symbol: str) -> int | None:
    value = await reader.fetch_value(
        "SELECT contract_id FROM dim_contract WHERE upper(fyers_symbol) = upper(?)",
        [symbol.strip()],
    )
    return None if value is None else int(value)


async def resolve_contract(
    reader: DuckReader,
    *,
    contract_id: int | None,
    symbol: str | None,
    res_id: int,
) -> ContractRef:
    """The contract the caller named, with the bounds of the requested series.

    One call to `catalog.contract_bounds` answers both questions, and it is the same lookup the
    chart makes on a symbol change, so the row is already warm.
    """
    if contract_id is None and symbol is None:
        raise _missing("Name the contract with contract_id or with symbol.")
    resolved = contract_id
    if resolved is None and symbol is not None:
        resolved = await _contract_id_for_symbol(reader, symbol)
        if resolved is None:
            raise ApiError(
                404, CODE_UNKNOWN_CONTRACT, f"{symbol} is not a contract in this store."
            )
    assert resolved is not None
    bounds = await catalog_q.contract_bounds(reader, resolved)
    if bounds["fyers_symbol"] is None:
        raise ApiError(
            404, CODE_UNKNOWN_CONTRACT, f"Contract {resolved} is not in this store."
        )
    first, last = _bounds_for(bounds, res_id)
    return ContractRef(
        contract_id=resolved,
        fyers_symbol=str(bounds["fyers_symbol"]),
        first_ts=first,
        last_ts=last,
    )


def clamp_window(
    ref: ContractRef, *, from_ts: int | None, to_ts: int | None
) -> Window:
    """Slide the requested span inside the contract's own first and last bar.

    The span length is preserved, because the chart sizes its first request from the number of
    bars it wants on screen and shrinking the window would leave it half empty.

    A caller that named no window gets the contract's whole life, which is the honest answer to
    "show me this contract" and is what the feed adapter asks for once it knows the bounds.

    A window that ends before the first bar is left alone and answers empty. That case is a chart
    scrolled to the left of the data, where empty is the truth, rather than a chart that has not
    been told where the data is.
    """
    if not ref.has_bars:
        return Window(from_ts=from_ts, to_ts=to_ts, clamped=False)
    first = ref.first_ts
    last = ref.last_ts
    assert first is not None and last is not None
    if from_ts is None or to_ts is None:
        return Window(from_ts=first, to_ts=last, clamped=from_ts is not None or to_ts is not None)
    requested_from = int(from_ts)
    requested_to = int(to_ts)
    if requested_to < first:
        # Entirely to the left of the data. Left exactly as asked, because here empty is the
        # truth and sliding the window forward would invent bars the caller did not ask for.
        return Window(from_ts=requested_from, to_ts=requested_to, clamped=False)
    span = max(1, requested_to - requested_from)
    end = min(requested_to, last)
    start = max(first, end - span)
    return Window(
        from_ts=start, to_ts=end, clamped=start != requested_from or end != requested_to
    )


def _ist_range(window: Window) -> tuple[datetime, datetime]:
    """The half open naive IST window the store is keyed on.

    The right edge gains a second before it is converted, because the query is half open on the
    right and the caller's `to` names a bar it expects to receive.
    """
    assert window.from_ts is not None and window.to_ts is not None
    start = bars_q.to_ist(window.from_ts)
    end = bars_q.to_ist(window.to_ts) + timedelta(seconds=1)
    return (start, end)


def _range_too_large(exc: bars_q.RangeTooLarge) -> ApiError:
    return ApiError(
        400,
        CODE_RANGE_TOO_LARGE,
        f"That range holds {exc.available} candles and one response carries at most "
        f"{exc.maximum}. Narrow the range or ask for a coarser resolution.",
        detail={"available": exc.available, "maximum": exc.maximum},
    )


def _empty_columns(include_oi: bool) -> list[str]:
    return list(bars_q.FYERS_COLUMNS if include_oi else bars_q.FYERS_COLUMNS_NO_OI)


def _integer_time(rows: list[list[Any]]) -> list[list[Any]]:
    """Force the timestamp column to a Python int, in place.

    DuckDB's `epoch()` returns a DOUBLE, so the column arrives as `1742960700.0` and would be
    written to the wire with a decimal point. The chart library's `Bar.time` is declared as an
    integer number of UTC seconds, and any consumer that is not JavaScript, the Phase 2
    backtester included, would otherwise read a float and have to round it back.

    One pass over a list that has already been materialised, which costs a fraction of what
    encoding the same list as JSON costs.
    """
    for row in rows:
        row[0] = int(row[0])
    return rows


@router.get("/bars", response_model=BarsResponse, summary="Bars for one contract")
async def read_bars(
    user: CurrentUserDep,
    reader: ReaderDep,
    contract_id: Annotated[int | None, Query(ge=1)] = None,
    symbol: Annotated[str | None, Query(max_length=64)] = None,
    resolution: Annotated[str | None, Query(max_length=8)] = None,
    interval: Annotated[str | None, Query(max_length=8)] = None,
    from_ts: Annotated[int | None, Query(alias="from")] = None,
    to_ts: Annotated[int | None, Query(alias="to")] = None,
    include_oi: bool = True,
) -> BarsResponse:
    """The chart's main request, clamped to the contract's own life."""
    res = await resolve_resolution(reader, resolution=resolution, interval=interval)
    ref = await resolve_contract(
        reader, contract_id=contract_id, symbol=symbol, res_id=res.res_id
    )
    window = clamp_window(ref, from_ts=from_ts, to_ts=to_ts)
    if window.is_empty:
        return BarsResponse(
            contract_id=ref.contract_id,
            symbol=ref.fyers_symbol,
            resolution=res.fyers_code,
            res_id=res.res_id,
            columns=_empty_columns(include_oi),
            candles=[],
            from_ts=window.from_ts,
            to_ts=window.to_ts,
            first_ts=ref.first_ts,
            last_ts=ref.last_ts,
            clamped=window.clamped,
        )
    start, end = _ist_range(window)
    try:
        page = await bars_q.bars(
            reader,
            contract_id=ref.contract_id,
            res_id=res.res_id,
            start=start,
            end=end,
            include_oi=include_oi,
            symbol=ref.fyers_symbol,
            resolution=res.fyers_code,
        )
    except bars_q.RangeTooLarge as exc:
        raise _range_too_large(exc) from exc
    return BarsResponse(
        contract_id=page.contract_id,
        symbol=page.symbol,
        resolution=res.fyers_code,
        res_id=res.res_id,
        columns=list(page.columns),
        candles=_integer_time(page.candles),
        from_ts=window.from_ts,
        to_ts=window.to_ts,
        first_ts=ref.first_ts,
        last_ts=ref.last_ts,
        clamped=window.clamped,
    )


@router.get(
    "/bars/before",
    response_model=BarsResponse,
    summary="The bars immediately before a timestamp",
)
async def read_bars_before(
    user: CurrentUserDep,
    reader: ReaderDep,
    before: Annotated[int, Query()],
    contract_id: Annotated[int | None, Query(ge=1)] = None,
    symbol: Annotated[str | None, Query(max_length=64)] = None,
    resolution: Annotated[str | None, Query(max_length=8)] = None,
    interval: Annotated[str | None, Query(max_length=8)] = None,
    count: Annotated[int, Query(ge=1, le=bars_q.MAX_BEFORE_COUNT)] = (
        bars_q.DEFAULT_BEFORE_COUNT
    ),
    include_oi: bool = True,
) -> BarsResponse:
    """Backs `chart.setHistoryLoader`, which pages left from the oldest bar on screen.

    No clamp here. The caller already holds a bar and is asking for what precedes it, so its
    `before` is a real timestamp from this store rather than a now relative guess.
    """
    res = await resolve_resolution(reader, resolution=resolution, interval=interval)
    ref = await resolve_contract(
        reader, contract_id=contract_id, symbol=symbol, res_id=res.res_id
    )
    page = await bars_q.bars_before(
        reader,
        contract_id=ref.contract_id,
        res_id=res.res_id,
        before=bars_q.to_ist(before),
        count=count,
        include_oi=include_oi,
        symbol=ref.fyers_symbol,
        resolution=res.fyers_code,
    )
    served_from = page.candles[0][0] if page.candles else None
    served_to = page.candles[-1][0] if page.candles else None
    return BarsResponse(
        contract_id=page.contract_id,
        symbol=page.symbol,
        resolution=res.fyers_code,
        res_id=res.res_id,
        columns=list(page.columns),
        candles=_integer_time(page.candles),
        from_ts=served_from,
        to_ts=served_to,
        first_ts=ref.first_ts,
        last_ts=ref.last_ts,
        clamped=False,
    )


@router.get(
    "/bars/oi",
    response_model=OpenInterestResponse,
    summary="Open interest points for one contract",
)
async def read_open_interest(
    user: CurrentUserDep,
    reader: ReaderDep,
    contract_id: Annotated[int | None, Query(ge=1)] = None,
    symbol: Annotated[str | None, Query(max_length=64)] = None,
    resolution: Annotated[str | None, Query(max_length=8)] = None,
    interval: Annotated[str | None, Query(max_length=8)] = None,
    from_ts: Annotated[int | None, Query(alias="from")] = None,
    to_ts: Annotated[int | None, Query(alias="to")] = None,
) -> OpenInterestResponse:
    """The Tier 2 indicator's own series.

    Clamped by the same rule as `/bars` and for a stronger reason: an unclamped indicator under a
    clamped chart renders empty beneath visible candles, which reads as missing open interest
    rather than as a window that missed.
    """
    res = await resolve_resolution(reader, resolution=resolution, interval=interval)
    ref = await resolve_contract(
        reader, contract_id=contract_id, symbol=symbol, res_id=res.res_id
    )
    window = clamp_window(ref, from_ts=from_ts, to_ts=to_ts)
    if window.is_empty:
        return OpenInterestResponse(
            contract_id=ref.contract_id,
            resolution=res.fyers_code,
            res_id=res.res_id,
            points=[],
            from_ts=window.from_ts,
            to_ts=window.to_ts,
            clamped=window.clamped,
        )
    start, end = _ist_range(window)
    points = await bars_q.oi_series(
        reader,
        contract_id=ref.contract_id,
        res_id=res.res_id,
        start=start,
        end=end,
    )
    return OpenInterestResponse(
        contract_id=ref.contract_id,
        resolution=res.fyers_code,
        res_id=res.res_id,
        points=_integer_time(points),
        from_ts=window.from_ts,
        to_ts=window.to_ts,
        clamped=window.clamped,
    )


@router.get(
    "/spot/bars", response_model=BarsResponse, summary="Bars for an underlying's spot series"
)
async def read_spot_bars(
    user: CurrentUserDep,
    reader: ReaderDep,
    underlying_id: Annotated[int, Query(ge=1)],
    resolution: Annotated[str | None, Query(max_length=8)] = None,
    interval: Annotated[str | None, Query(max_length=8)] = None,
    from_ts: Annotated[int | None, Query(alias="from")] = None,
    to_ts: Annotated[int | None, Query(alias="to")] = None,
) -> BarsResponse:
    """The index or equity series behind an expiry.

    Spot lives in `candles` as a contract of kind SPOT, so once its contract id is known this is
    the identical path, clamp included. An index is still trading, so its clamp is usually a no
    op, but an underlying that was delisted or simply stopped being downloaded has the same cliff
    an expired option has.
    """
    res = await resolve_resolution(reader, resolution=resolution, interval=interval)
    spot_id = await bars_q.spot_contract_id(reader, underlying_id)
    if spot_id is None:
        exists = await reader.fetch_value(
            "SELECT 1 FROM dim_underlying WHERE underlying_id = ?", [underlying_id]
        )
        if exists is None:
            raise ApiError(
                404,
                CODE_UNKNOWN_UNDERLYING,
                f"Underlying {underlying_id} is not registered.",
            )
        # Registered, but its spot series has never been resolved to a contract. An empty answer
        # rather than a 404: the underlying is real and the chart should say "no bars", not
        # "no such underlying".
        return BarsResponse(
            contract_id=None,
            symbol=None,
            resolution=res.fyers_code,
            res_id=res.res_id,
            columns=_empty_columns(False),
            candles=[],
        )
    ref = await resolve_contract(
        reader, contract_id=spot_id, symbol=None, res_id=res.res_id
    )
    window = clamp_window(ref, from_ts=from_ts, to_ts=to_ts)
    if window.is_empty:
        return BarsResponse(
            contract_id=ref.contract_id,
            symbol=ref.fyers_symbol,
            resolution=res.fyers_code,
            res_id=res.res_id,
            columns=_empty_columns(False),
            candles=[],
            from_ts=window.from_ts,
            to_ts=window.to_ts,
            first_ts=ref.first_ts,
            last_ts=ref.last_ts,
            clamped=window.clamped,
        )
    start, end = _ist_range(window)
    try:
        page = await bars_q.bars(
            reader,
            contract_id=ref.contract_id,
            res_id=res.res_id,
            start=start,
            end=end,
            include_oi=False,
            symbol=ref.fyers_symbol,
            resolution=res.fyers_code,
        )
    except bars_q.RangeTooLarge as exc:
        raise _range_too_large(exc) from exc
    return BarsResponse(
        contract_id=page.contract_id,
        symbol=page.symbol,
        resolution=res.fyers_code,
        res_id=res.res_id,
        columns=list(page.columns),
        candles=_integer_time(page.candles),
        from_ts=window.from_ts,
        to_ts=window.to_ts,
        first_ts=ref.first_ts,
        last_ts=ref.last_ts,
        clamped=window.clamped,
    )
