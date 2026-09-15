"""What is already downloaded, and where the holes are. API.md section 6, the ledger half.

Both routes read `candle_coverage` and never `candles`. That is the reason the ledger exists: the
heatmap on the download screen has to render in the same few milliseconds when the candle table
holds fifty thousand rows and when it holds five hundred million, and a `count(*)` over candles
grouped by expiry cannot do that. One coverage row is one recorded fetch chunk, so the grid is an
aggregate over thousands of rows rather than over hundreds of millions.

`contracts_total` comes from the expiry row rather than from a count of the contracts that have
coverage, so a cell holding nothing still reports how much is missing instead of reading as
complete. A heatmap whose empty cells are invisible is a heatmap that hides exactly what it exists
to show.

The gaps route reports both edges. `held_to` and `held_from` are the two chunks the view found on
either side of the hole, and `gap_from` and `gap_to` are the inclusive days that are missing,
which is the window a repair download would ask for. Returning only the edges would make every
consumer do the same two date additions, and one of them would eventually do it wrong.

Neither route spends a Fyers request and neither needs a broker token. Looking at what is already
stored is exactly what a user does while the broker session is dead.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query

from expirymanager.api.deps import CurrentUserDep, ReaderDep
from expirymanager.api.schemas.jobs import (
    CoverageCell,
    CoverageGapRow,
    CoverageGapsResponse,
    CoverageGridResponse,
    CoverageResolution,
)
from expirymanager.db.queries import catalog, coverage as coverage_queries

__all__ = ["router", "DEFAULT_GAP_LIMIT", "MAX_GAP_LIMIT"]

log = logging.getLogger(__name__)

router = APIRouter()

DEFAULT_GAP_LIMIT = 200
MAX_GAP_LIMIT = 2000

ONE_DAY = timedelta(days=1)


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_int(value: Any) -> int:
    return 0 if value is None else int(value)


@router.get("/coverage/grid", response_model=CoverageGridResponse)
async def coverage_grid(
    user: CurrentUserDep,
    reader: ReaderDep,
    underlying_id: Annotated[int, Query(ge=1)],
    res_id: Annotated[int | None, Query(ge=1)] = None,
    expiry_from: Annotated[date | None, Query()] = None,
    expiry_to: Annotated[date | None, Query()] = None,
) -> CoverageGridResponse:
    """The expiry by resolution matrix the heatmap renders."""
    rows = await coverage_queries.coverage_grid(
        reader,
        underlying_id=underlying_id,
        res_id=res_id,
        expiry_from=expiry_from,
        expiry_to=expiry_to,
    )

    cells: list[CoverageCell] = []
    for row in rows:
        total = _as_int(row.get("contracts_total"))
        covered = _as_int(row.get("contracts_covered"))
        cells.append(
            CoverageCell(
                expiry_date=_as_date(row["expiry_date"]),
                res_id=_as_int(row["res_id"]),
                contracts_total=total,
                contracts_with_data=covered,
                # Never negative: a chain that gained contracts after the expiry row was written
                # would otherwise report a negative shortfall and render as an inverted bar.
                contracts_missing=max(total - covered, 0),
                chunks_ok=_as_int(row.get("chunks_ok")),
                chunks_empty=_as_int(row.get("chunks_empty")),
                chunks_error=_as_int(row.get("chunks_error")),
                rows=_as_int(row.get("rows")),
                have_from=_as_date(row.get("have_from")),
                have_to=_as_date(row.get("have_to")),
            )
        )

    # The column headers are the resolutions that actually appear in the grid, read from the
    # resolution reference so the labels and the ordering are the ones every other screen uses.
    # Listing all fourteen would give the heatmap a dozen permanently empty columns.
    present = {cell.res_id for cell in cells}
    if res_id is not None:
        present.add(res_id)
    reference = await catalog.resolutions(reader)
    resolutions = [
        CoverageResolution(
            res_id=int(item["res_id"]),
            fyers_code=str(item["fyers_code"]),
            label=str(item["label"]),
            seconds=int(item["seconds"]),
        )
        for item in reference
        if int(item["res_id"]) in present
    ]
    return CoverageGridResponse(
        underlying_id=underlying_id, resolutions=resolutions, cells=cells
    )


@router.get("/coverage/gaps", response_model=CoverageGapsResponse)
async def coverage_gaps(
    user: CurrentUserDep,
    reader: ReaderDep,
    underlying_id: Annotated[int | None, Query(ge=1)] = None,
    res_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_GAP_LIMIT)] = DEFAULT_GAP_LIMIT,
) -> CoverageGapsResponse:
    """Holes between two recorded chunks of the same contract at the same resolution."""
    rows = await coverage_queries.coverage_gaps(
        reader, underlying_id=underlying_id, res_id=res_id, limit=limit
    )
    items: list[CoverageGapRow] = []
    for row in rows:
        held_to = _as_date(row["gap_after"])
        held_from = _as_date(row["gap_before"])
        if held_to is None or held_from is None:
            continue
        gap_from = held_to + ONE_DAY
        gap_to = held_from - ONE_DAY
        items.append(
            CoverageGapRow(
                contract_id=int(row["contract_id"]),
                fyers_symbol=(
                    None if row.get("fyers_symbol") is None else str(row["fyers_symbol"])
                ),
                expiry_date=_as_date(row.get("expiry_date")),
                res_id=int(row["res_id"]),
                held_to=held_to,
                held_from=held_from,
                gap_from=gap_from,
                gap_to=gap_to,
                missing_days=(gap_to - gap_from).days + 1,
            )
        )
    return CoverageGapsResponse(items=items)
