"""Contracts: the server driven listing, one contract, and its chart bounds. API.md section 5.

**The filtering, the sorting and the paging all happen in DuckDB.** One NIFTY weekly expiry
carries 482 contracts and a year of them carries tens of thousands, so a client that fetched the
list and filtered it in the browser would move megabytes to render twenty rows. Every predicate
here is a column the store already has, and the page is keyset paged on `contract_id` rather than
OFFSET paged, because OFFSET makes the last page cost as much as the whole scan.

**The cursor is opaque and is checked.** It is minted by `encode_cursor` and decoded with the key
set this collection uses, so a cursor from the jobs listing cannot be fed here and silently page
from the wrong key. A cursor that was not minted by this application is a 400, not a 500.

**Bounds are read separately from the listing, on purpose.** `list_contracts` joins
`contract_bounds` to support `has_data` and the `rows` sort, and that join emits one row per
resolution held, so a contract with a one minute and a five minute series appears twice. The
listing therefore groups by `contract_id`, and then reads every bound for the ids on the page in
one statement. Deriving the resolutions from the joined rows instead would be wrong for
`sort=rows`, where the two rows of one contract sort apart and can land on different pages.

**`/contracts/{id}/bounds` answers in UTC seconds.** That is what the chart adapter clamps its
window with, and it is the single lookup that stops every expired contract rendering as "No
bars": the chart's default window is the live present, and an expired contract has nothing there.
The conversion happens in DuckDB, in `db/queries/catalog.contract_bounds`, because this route is
called on every symbol change.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, Query

from expirymanager.api.deps import CurrentUserDep, ReaderDep
from expirymanager.api.errors import ApiError, not_found
from expirymanager.api.schemas.catalog import (
    BoundsResolution,
    ContractBoundsOut,
    ContractCoverage,
    ContractDetail,
    ContractOut,
    ContractPage,
    ContractResolution,
    as_float,
    as_int,
    iso_text,
)
from expirymanager.api.schemas.common import decode_cursor, encode_cursor
from expirymanager.db.queries import catalog as catalog_queries

__all__ = [
    "router",
    "CONTRACT_CURSOR_KEY",
    "CODE_UNKNOWN_SORT",
    "render_contract_page",
    "read_bounds_for",
]

log = logging.getLogger(__name__)

router = APIRouter()

CONTRACT_CURSOR_KEY = "contract_id"
CODE_UNKNOWN_SORT = "unknown_sort_column"


async def read_bounds_for(
    reader: Any, contract_ids: Sequence[int]
) -> dict[int, list[ContractResolution]]:
    """Every held resolution for a page of contracts, in one statement.

    One statement rather than one per contract: a full page is 100 contracts and a round trip
    each would put a hundred thread pool hops in front of a listing that is otherwise one scan.
    """
    ids = [int(value) for value in contract_ids]
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    columns, rows = await reader.fetch_columns(
        "SELECT b.contract_id, b.res_id, r.fyers_code, r.chart_interval, b.row_count,"
        "       b.first_ts, b.last_ts"
        "  FROM contract_bounds b LEFT JOIN dim_resolution r USING (res_id)"
        f" WHERE b.contract_id IN ({placeholders})"
        " ORDER BY b.contract_id, r.seconds",
        ids,
    )
    out: dict[int, list[ContractResolution]] = {}
    for row in rows:
        record = dict(zip(columns, row))
        out.setdefault(int(record["contract_id"]), []).append(
            ContractResolution(
                res_id=int(record["res_id"]),
                fyers_code=record["fyers_code"],
                chart_interval=record["chart_interval"],
                rows=as_int(record["row_count"]),
                first_ts=iso_text(record["first_ts"]),
                last_ts=iso_text(record["last_ts"]),
            )
        )
    return out


def _contract_out(row: Mapping[str, Any]) -> ContractOut:
    return ContractOut(
        contract_id=int(row["contract_id"]),
        fyers_symbol=str(row["fyers_symbol"]),
        underlying_id=int(row["underlying_id"]),
        kind=str(row["kind"]),
        instrument_class=row.get("instrument_class"),
        expiry_date=iso_text(row.get("expiry_date")),
        strike=as_float(row.get("strike")),
        strike_raw=row.get("strike_raw"),
        option_type=row.get("option_type"),
        lot_size=row.get("lot_size"),
        tick_size=as_float(row.get("tick_size")),
        fytoken=row.get("fytoken"),
        symbol_expiry_encoding=row.get("symbol_expiry_encoding"),
        expiry_cycle=row.get("expiry_cycle"),
        parse_confidence=row.get("parse_confidence"),
        sealed_at=iso_text(row.get("sealed_at")),
    )


async def render_contract_page(reader: Any, page: catalog_queries.Page) -> ContractPage:
    """Group the joined rows by contract and attach every resolution each one holds.

    Shared with `GET /expiries/{underlying_id}/{expiry_date}/contracts`, which returns the same
    shape over the same query, so the two listings cannot drift into two different row models.
    """
    grouped: dict[int, ContractOut] = {}
    for row in page.items:
        contract_id = int(row["contract_id"])
        if contract_id not in grouped:
            grouped[contract_id] = _contract_out(row)

    bounds = await read_bounds_for(reader, list(grouped))
    items: list[ContractOut] = []
    for contract_id, item in grouped.items():
        resolutions = bounds.get(contract_id, [])
        items.append(
            item.model_copy(
                update={
                    "resolutions": resolutions,
                    "rows": sum(entry.rows for entry in resolutions),
                }
            )
        )
    return ContractPage(
        items=items,
        next_cursor=(
            None
            if page.next_cursor is None
            else encode_cursor({CONTRACT_CURSOR_KEY: int(page.next_cursor)})
        ),
    )


@router.get(
    "/contracts",
    response_model=ContractPage,
    summary="Server driven contract listing",
)
async def list_contracts(
    _user: CurrentUserDep,
    reader: ReaderDep,
    underlying_id: int | None = Query(default=None, ge=1),
    expiry_from: date | None = Query(default=None),
    expiry_to: date | None = Query(default=None),
    kind: str | None = Query(default=None, pattern="^(FUT|OPT)$"),
    option_type: str | None = Query(default=None, pattern="^(CE|PE)$"),
    strike_min: float | None = Query(default=None, ge=0),
    strike_max: float | None = Query(default=None, ge=0),
    symbol_contains: str | None = Query(default=None, max_length=64),
    has_data: bool | None = Query(default=None),
    sealed: bool | None = Query(default=None),
    res_id: int | None = Query(default=None, ge=1),
    sort: str = Query(default="expiry_date"),
    dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=catalog_queries.DEFAULT_PAGE, ge=1, le=catalog_queries.MAX_PAGE),
) -> ContractPage:
    if sort not in catalog_queries.CONTRACT_SORTS:
        # Named here rather than let through to the query layer, so the caller gets the allowed
        # set back instead of a 500 from a ValueError raised inside a thread pool.
        raise ApiError(
            400,
            CODE_UNKNOWN_SORT,
            "Sortable columns are: " + ", ".join(sorted(catalog_queries.CONTRACT_SORTS)),
            detail={"allowed": sorted(catalog_queries.CONTRACT_SORTS)},
        )

    after: int | None = None
    if cursor:
        after = int(decode_cursor(cursor, expect=(CONTRACT_CURSOR_KEY,))[CONTRACT_CURSOR_KEY])

    page = await catalog_queries.list_contracts(
        reader,
        filters=catalog_queries.ContractFilters(
            underlying_id=underlying_id,
            expiry_from=expiry_from,
            expiry_to=expiry_to,
            kind=kind,
            option_type=option_type,
            strike_min=strike_min,
            strike_max=strike_max,
            symbol_contains=symbol_contains,
            has_data=has_data,
            sealed=sealed,
            res_id=res_id,
        ),
        sort=sort,
        direction=dir,
        cursor=after,
        limit=limit,
    )
    return await render_contract_page(reader, page)


@router.get(
    "/contracts/{contract_id}",
    response_model=ContractDetail,
    summary="One contract with its bounds and coverage",
)
async def read_contract(
    contract_id: int,
    _user: CurrentUserDep,
    reader: ReaderDep,
) -> ContractDetail:
    row = await catalog_queries.get_contract(reader, contract_id)
    if row is None:
        raise not_found(f"There is no contract {contract_id}.")

    # `get_contract` attaches its resolutions from the bounds query, which answers in UTC seconds
    # for the chart. This view is text, so the bounds are read again as timestamps rather than
    # converted back from seconds, which is where an off by five and a half hours would come in.
    resolutions = (await read_bounds_for(reader, [contract_id])).get(contract_id, [])

    base = _contract_out(row).model_dump()
    base.pop("resolutions", None)
    base.pop("rows", None)
    return ContractDetail(
        **base,
        expiry_id=row.get("expiry_id"),
        root=row.get("root"),
        exchange=row.get("exchange"),
        segment=row.get("segment"),
        underlying_symbol=row.get("underlying_symbol"),
        underlying_name=row.get("underlying_name"),
        underlying_kind=row.get("underlying_kind"),
        exchange_token=row.get("exchange_token"),
        isin=row.get("isin"),
        qty_freeze=row.get("qty_freeze"),
        qty_multiplier=as_float(row.get("qty_multiplier")),
        trading_session=row.get("trading_session"),
        symbol_description=row.get("symbol_description"),
        parsed_expiry_date=iso_text(row.get("parsed_expiry_date")),
        parse_method=row.get("parse_method"),
        parse_warnings=row.get("parse_warnings"),
        first_seen_at=iso_text(row.get("first_seen_at")),
        last_seen_at=iso_text(row.get("last_seen_at")),
        resolutions=resolutions,
        rows=sum(entry.rows for entry in resolutions),
        coverage=[
            ContractCoverage(
                res_id=int(entry["res_id"]),
                chunks=as_int(entry.get("chunks")),
                chunks_ok=as_int(entry.get("chunks_ok")),
                chunks_empty=as_int(entry.get("chunks_empty")),
                chunks_error=as_int(entry.get("chunks_error")),
                have_from=iso_text(entry.get("have_from")),
                have_to=iso_text(entry.get("have_to")),
                rows=as_int(entry.get("rows")),
            )
            for entry in row.get("coverage", [])
        ],
    )


@router.get(
    "/contracts/{contract_id}/bounds",
    response_model=ContractBoundsOut,
    summary="First and last bar per resolution, as UTC seconds",
)
async def read_contract_bounds(
    contract_id: int,
    _user: CurrentUserDep,
    reader: ReaderDep,
) -> ContractBoundsOut:
    bounds = await catalog_queries.contract_bounds(reader, contract_id)
    if bounds["fyers_symbol"] is None:
        raise not_found(f"There is no contract {contract_id}.")
    return ContractBoundsOut(
        contract_id=int(bounds["contract_id"]),
        fyers_symbol=bounds["fyers_symbol"],
        resolutions=[
            BoundsResolution(
                res_id=int(entry["res_id"]),
                fyers_code=entry.get("fyers_code"),
                chart_interval=entry.get("chart_interval"),
                first_ts=None if entry.get("first_ts") is None else int(entry["first_ts"]),
                last_ts=None if entry.get("last_ts") is None else int(entry["last_ts"]),
                rows=as_int(entry.get("rows")),
            )
            for entry in bounds["resolutions"]
        ],
    )
