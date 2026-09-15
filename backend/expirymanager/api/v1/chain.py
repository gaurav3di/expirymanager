"""`GET /chain` and `GET /chain/atm`: the option chain at one instant.

API.md section 7. This is the query the Phase 2 backtester will run hardest, once per bar per
expiry, so the shape it returns is the shape a strategy wants: one row per strike carrying both
rights, the spot that was trading at that instant, and the at the money strike marked.

Two things are delegated rather than reimplemented here.

The chain grid and the at the money pick come from `db/queries/bars.py`, which reads the shipped
DuckDB macros. There is exactly one definition of "the spot at this moment" and exactly one
definition of "the at the money strike" in this codebase, and an API that carried a second one in
Python would eventually disagree with an export and with the backtester.

The instant is snapped to a bar. The grid query matches `ts` exactly, because a chain is a slice
of a bar and not an interpolation, so an instant that is not a bar boundary matches nothing. A
caller asking for 11:17:30 of a one minute series means the 11:17 bar, and gets it, and is told
in `ts` which bar it got. Without the snap the endpoint answers an empty chain for almost every
timestamp a human or a backtester would naturally pass, and the emptiness looks like missing
data.

The snap is a `max(ts)` over the expiry's own contract id block, which is data. It is not a
session close, not a weekday rule and not a calendar: nothing in this module knows when a market
opens or shuts, and the 15:30 to 15:40 close change of 2026-08-03 passes through it unnoticed.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Query

from expirymanager.api.deps import CurrentUserDep, ReaderDep
from expirymanager.api.errors import ApiError
from expirymanager.api.schemas.bars import (
    ChainAtmResponse,
    ChainLegModel,
    ChainResponse,
    ChainRowModel,
)
from expirymanager.api.v1.bars import resolve_resolution
from expirymanager.db.queries import bars as bars_q
from expirymanager.db.reader import DuckReader

__all__ = ["router", "CODE_UNKNOWN_EXPIRY", "snap_to_bar"]

router = APIRouter()

CODE_UNKNOWN_EXPIRY = "unknown_expiry"

# The id block of one expiry. Contract ids are allocated in contiguous padded blocks precisely so
# that a whole expiry is one BETWEEN rather than several hundred scattered lookups.
_BLOCK_SQL = (
    "SELECT contract_id_lo, contract_id_hi FROM dim_expiry "
    " WHERE underlying_id = ? AND expiry_date = ?"
)

_LATEST_SQL = (
    "SELECT max(ts) FROM candles "
    " WHERE res_id = ? AND contract_id BETWEEN ? AND ?"
)

_LATEST_AT_OR_BEFORE_SQL = (
    "SELECT max(ts) FROM candles "
    " WHERE res_id = ? AND contract_id BETWEEN ? AND ? AND ts <= ?"
)


async def _expiry_block(
    reader: DuckReader, *, underlying_id: int, expiry_date: date
) -> tuple[int, int]:
    row = await reader.fetch_one(_BLOCK_SQL, [underlying_id, expiry_date])
    if row is None:
        raise ApiError(
            404,
            CODE_UNKNOWN_EXPIRY,
            f"No expiry {expiry_date.isoformat()} is registered for underlying {underlying_id}.",
        )
    if row[0] is None or row[1] is None:
        # The expiry was discovered but its contracts have not been listed yet, so it has no id
        # block and therefore no chain. Not an error: the screen shows an empty chain and the
        # download screen is where the user fixes it.
        raise ApiError(
            404,
            CODE_UNKNOWN_EXPIRY,
            f"Expiry {expiry_date.isoformat()} has no contracts listed yet.",
        )
    return (int(row[0]), int(row[1]))


async def snap_to_bar(
    reader: DuckReader,
    *,
    block: tuple[int, int],
    res_id: int,
    moment: datetime | None,
) -> datetime | None:
    """The most recent bar at or before `moment` anywhere in the expiry, or the last bar of all.

    Returns None when the expiry holds no bars at that resolution at all, which is the difference
    between "nothing was downloaded" and "nothing traded at that instant".
    """
    lo, hi = block
    if moment is None:
        value = await reader.fetch_value(_LATEST_SQL, [res_id, lo, hi])
    else:
        value = await reader.fetch_value(
            _LATEST_AT_OR_BEFORE_SQL, [res_id, lo, hi, moment]
        )
    return None if value is None else value


def _leg(leg: bars_q.ChainLeg | None) -> ChainLegModel | None:
    if leg is None:
        return None
    return ChainLegModel(
        contract_id=leg.contract_id,
        fyers_symbol=leg.fyers_symbol,
        open=leg.open,
        high=leg.high,
        low=leg.low,
        close=leg.close,
        volume=leg.volume,
        oi=leg.oi,
    )


@router.get("/chain", response_model=ChainResponse, summary="The option chain at one instant")
async def read_chain(
    user: CurrentUserDep,
    reader: ReaderDep,
    underlying_id: Annotated[int, Query(ge=1)],
    expiry_date: Annotated[date, Query()],
    resolution: Annotated[str | None, Query(max_length=8)] = None,
    interval: Annotated[str | None, Query(max_length=8)] = None,
    ts: Annotated[int | None, Query()] = None,
) -> ChainResponse:
    """Every strike of one expiry at one bar, with the at the money row flagged.

    `ts` is optional. Omitted, it means the last bar the expiry holds, which is what a screen
    opening on a contract that expired months ago wants and what a human means by "show me the
    chain". Given, it is snapped back to a bar boundary.
    """
    res = await resolve_resolution(reader, resolution=resolution, interval=interval)
    block = await _expiry_block(
        reader, underlying_id=underlying_id, expiry_date=expiry_date
    )
    requested = None if ts is None else bars_q.to_ist(ts)
    moment = await snap_to_bar(reader, block=block, res_id=res.res_id, moment=requested)
    if moment is None:
        return ChainResponse(
            underlying_id=underlying_id,
            expiry_date=expiry_date.isoformat(),
            resolution=res.fyers_code,
            res_id=res.res_id,
            ts=None,
            requested_ts=ts,
            spot=None,
            atm_strike=None,
            rows=[],
        )
    slice_ = await bars_q.chain(
        reader,
        underlying_id=underlying_id,
        expiry_date=expiry_date,
        res_id=res.res_id,
        moment=moment,
    )
    atm_strike = slice_.atm_strike
    return ChainResponse(
        underlying_id=underlying_id,
        expiry_date=expiry_date.isoformat(),
        resolution=res.fyers_code,
        res_id=res.res_id,
        ts=slice_.ts,
        requested_ts=ts,
        spot=slice_.spot,
        atm_strike=atm_strike,
        rows=[
            ChainRowModel(
                strike=row.strike,
                lot_size=row.lot_size,
                is_atm=atm_strike is not None and row.strike == atm_strike,
                ce=_leg(row.ce),
                pe=_leg(row.pe),
            )
            for row in slice_.rows
        ],
    )


@router.get(
    "/chain/atm",
    response_model=ChainAtmResponse,
    summary="The at the money strike and its two legs",
)
async def read_chain_atm(
    user: CurrentUserDep,
    reader: ReaderDep,
    underlying_id: Annotated[int, Query(ge=1)],
    expiry_date: Annotated[date, Query()],
    resolution: Annotated[str | None, Query(max_length=8)] = None,
    interval: Annotated[str | None, Query(max_length=8)] = None,
    ts: Annotated[int | None, Query()] = None,
) -> ChainAtmResponse:
    """The pair a straddle needs, without paying for the whole grid.

    Same snap, same macros, same definition of at the money as `/chain`, so the row this returns
    is always the row `/chain` flags.
    """
    res = await resolve_resolution(reader, resolution=resolution, interval=interval)
    block = await _expiry_block(
        reader, underlying_id=underlying_id, expiry_date=expiry_date
    )
    requested = None if ts is None else bars_q.to_ist(ts)
    moment = await snap_to_bar(reader, block=block, res_id=res.res_id, moment=requested)
    if moment is None:
        return ChainAtmResponse(
            underlying_id=underlying_id,
            expiry_date=expiry_date.isoformat(),
            resolution=res.fyers_code,
            res_id=res.res_id,
            ts=None,
            requested_ts=ts,
        )
    pick = await bars_q.atm(
        reader,
        underlying_id=underlying_id,
        expiry_date=expiry_date,
        res_id=res.res_id,
        moment=moment,
    )
    return ChainAtmResponse(
        underlying_id=underlying_id,
        expiry_date=expiry_date.isoformat(),
        resolution=res.fyers_code,
        res_id=res.res_id,
        ts=bars_q.to_utc_seconds(moment),
        requested_ts=ts,
        spot=pick.spot,
        atm_strike=pick.atm_strike,
        ce_contract_id=pick.ce_contract_id,
        pe_contract_id=pick.pe_contract_id,
    )
