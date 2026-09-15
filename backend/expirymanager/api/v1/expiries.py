"""Expiries for one underlying, and the discovery that finds them. API.md section 4.

The expiry list is what the download sheet reads to know what is already held, so the coverage
block on every row comes from `db/queries/catalog.list_expiries`, which reads `candle_coverage`
and `contract_bounds` and nothing else. It is not recomputed here and it must never be: the
planner subtracts from that same ledger when it prices a sheet, and a second definition of
"already held" would show the user a download that the planner then refuses as nothing to do. It
also never touches `candles`, which grows to hundreds of millions of rows and would turn one
visit to this screen into a full scan.

Discovery writes its job and its task rows directly rather than through the planner. That is not
a shortcut: the planner prices a sheet of contracts, and discovery is the step that finds out
which contracts exist, so there is nothing yet to price. One task is one expiry-dates request,
and the range is chunked into windows of at most 366 calendar days because the endpoint answers a
wider window with an error rather than a truncation. The newest day the endpoint will serve is
computed from `last_served_day`, not assumed: a window ending today answers 422.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, Query, status
from sqlalchemy import Engine, text
from starlette.concurrency import run_in_threadpool

from expirymanager.api.deps import (
    CurrentUserDep,
    EngineDep,
    RateLimiterDep,
    ReaderDep,
    SupervisorDep,
    TokenBrokerDep,
)
from expirymanager.api.errors import ApiError, needs_reauth, not_found
from expirymanager.api.schemas.catalog import (
    ContractPage,
    DiscoverAccepted,
    DiscoverRequest,
    DiscoverWindow,
    ExpiryCoverage,
    ExpiryOut,
    ExpiryPage,
    as_float,
    as_int,
    iso_text,
)
from expirymanager.api.schemas.common import CursorError, decode_cursor, encode_cursor
from expirymanager.api.v1.contracts import render_contract_page
from expirymanager.api.v1.underlyings import (
    CATALOG_MUTATION_LIMIT,
    CODE_MCX_NOT_SUPPORTED,
    enforce_limit,
    read_registry_rows,
)
from expirymanager.brokers.fyers.calendar import chunk_range, exchange_data_floor
from expirymanager.db.queries import catalog as catalog_queries
from expirymanager.pipeline.handlers.candle_chunk import MCX_REFUSAL
from expirymanager.pipeline.handlers.expiry_discovery import MAX_WINDOW_DAYS, last_served_day
from expirymanager.pipeline.queue import DEFAULT_MAX_ATTEMPTS, iso_at, utc_now

__all__ = [
    "router",
    "CODE_RANGE_BEFORE_FLOOR",
    "CODE_RANGE_AFTER_LAST_SERVED",
    "DISCOVERY_PRIORITY",
    "EXPIRY_CURSOR_KEY",
    "enqueue_expiry_discovery",
]

log = logging.getLogger(__name__)

router = APIRouter()

CODE_RANGE_BEFORE_FLOOR = "range_before_floor"
CODE_RANGE_AFTER_LAST_SERVED = "range_after_last_served_day"

# Discovery is cheap and everything downstream waits on it, so it runs ahead of a backfill, which
# takes the default 100.
DISCOVERY_PRIORITY = 50

EXPIRY_CURSOR_KEY = "expiry_date"

DEFAULT_EXPIRY_PAGE = 200


def _require_underlying(engine: Engine, underlying_id: int) -> dict[str, Any]:
    rows = read_registry_rows(engine, underlying_id=underlying_id)
    if not rows:
        raise not_found(f"There is no underlying {underlying_id}.")
    return rows[0]


# ---------------------------------------------------------------------------
# The expiry list
# ---------------------------------------------------------------------------


def _expiry_out(row: Mapping[str, Any]) -> ExpiryOut:
    contract_count = row.get("contract_count")
    with_data = as_int(row.get("contracts_with_data"))
    return ExpiryOut(
        expiry_id=int(row["expiry_id"]),
        expiry_date=str(iso_text(row["expiry_date"])),
        expiry_dow=None if row.get("expiry_dow") is None else int(row["expiry_dow"]),
        has_futures=bool(row.get("has_futures")),
        has_options=bool(row.get("has_options")),
        futures_count=None if row.get("futures_count") is None else int(row["futures_count"]),
        options_count=None if row.get("options_count") is None else int(row["options_count"]),
        contract_count=None if contract_count is None else int(contract_count),
        expiry_cycle=row.get("expiry_cycle_derived"),
        expiry_cycle_source=row.get("expiry_cycle_source"),
        is_last_of_month=row.get("is_last_of_month"),
        discovered_at=iso_text(row.get("discovered_at")),
        contracts_discovered_at=iso_text(row.get("contracts_discovered_at")),
        contract_id_lo=row.get("contract_id_lo"),
        contract_id_hi=row.get("contract_id_hi"),
        min_strike=as_float(row.get("min_strike")),
        max_strike=as_float(row.get("max_strike")),
        strike_step=as_float(row.get("strike_step")),
        coverage=ExpiryCoverage(
            contracts_with_data=with_data,
            contracts_without_data=max(0, as_int(contract_count) - with_data),
            contracts_sealed=as_int(row.get("contracts_sealed")),
            chunks_ok=as_int(row.get("chunks_ok")),
            chunks_empty=as_int(row.get("chunks_empty")),
            chunks_error=as_int(row.get("chunks_error")),
            rows=as_int(row.get("rows")),
        ),
    )


@router.get(
    "/underlyings/{underlying_id}/expiries",
    response_model=ExpiryPage,
    summary="Expiries for one underlying with their coverage state",
)
async def list_expiries(
    underlying_id: int,
    _user: CurrentUserDep,
    engine: EngineDep,
    reader: ReaderDep,
    range_from: date | None = Query(default=None, alias="from"),
    range_to: date | None = Query(default=None, alias="to"),
    res_id: int | None = Query(default=None, ge=1),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_EXPIRY_PAGE, ge=1, le=catalog_queries.MAX_PAGE),
) -> ExpiryPage:
    await run_in_threadpool(_require_underlying, engine, underlying_id)

    after: str | None = None
    if cursor:
        try:
            after = str(decode_cursor(cursor, expect=(EXPIRY_CURSOR_KEY,))[EXPIRY_CURSOR_KEY])
            date.fromisoformat(after)
        except (CursorError, ValueError) as exc:
            raise CursorError("the cursor does not belong to this collection") from exc

    page = await catalog_queries.list_expiries(
        reader,
        underlying_id=underlying_id,
        expiry_from=range_from,
        expiry_to=range_to,
        res_id=res_id,
        cursor=after,
        limit=limit,
    )
    return ExpiryPage(
        items=[_expiry_out(row) for row in page.items],
        next_cursor=(
            None
            if page.next_cursor is None
            else encode_cursor({EXPIRY_CURSOR_KEY: page.next_cursor})
        ),
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def enqueue_expiry_discovery(
    engine: Engine,
    *,
    underlying_id: int,
    fyers_symbol: str,
    windows: Sequence[tuple[date, date]],
    created_by: str,
    job_id: str | None = None,
) -> str:
    """Write one `expiry_discovery` job and one `expiry_dates` task per window, atomically.

    The column shape, the `queue.iso_at` timestamps and `DEFAULT_MAX_ATTEMPTS` are the ones the
    lease statement already selects on, so a task written here is indistinguishable from one the
    planner wrote. Every other column takes its DDL default rather than being restated, which is
    what keeps this insert from drifting as the table grows columns.
    """
    identifier = job_id or str(uuid.uuid4())
    now = iso_at(utc_now())
    rows = [
        {
            "job_id": identifier,
            "seq": seq,
            "kind": "expiry_dates",
            "state": "pending",
            "priority": DISCOVERY_PRIORITY,
            "underlying_id": underlying_id,
            "fyers_symbol": fyers_symbol,
            "range_from": window_from.isoformat(),
            "range_to": window_to.isoformat(),
            "include_oi": 0,
            "request_params_json": json.dumps(
                {
                    "symbol": fyers_symbol,
                    "from_date": window_from.isoformat(),
                    "to_date": window_to.isoformat(),
                },
                sort_keys=True,
            ),
            "attempt": 0,
            "max_attempts": DEFAULT_MAX_ATTEMPTS,
            "not_before": now,
            "created_at": now,
        }
        for seq, (window_from, window_to) in enumerate(windows)
    ]
    columns = (
        "job_id, seq, kind, state, priority, underlying_id, fyers_symbol, range_from,"
        " range_to, include_oi, request_params_json, attempt, max_attempts, not_before,"
        " created_at"
    )
    placeholders = (
        ":job_id, :seq, :kind, :state, :priority, :underlying_id, :fyers_symbol, :range_from,"
        " :range_to, :include_oi, :request_params_json, :attempt, :max_attempts, :not_before,"
        " :created_at"
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO job (job_id, kind, status, params_json, priority, est_requests,"
                " total_tasks, created_by, created_at)"
                " VALUES (:job_id, 'expiry_discovery', 'queued', :params_json, :priority,"
                " :est_requests, :total_tasks, :created_by, :created_at)"
            ),
            {
                "job_id": identifier,
                "params_json": json.dumps(
                    {
                        "underlying_id": underlying_id,
                        "fyers_symbol": fyers_symbol,
                        "range_from": windows[-1][0].isoformat(),
                        "range_to": windows[0][1].isoformat(),
                        "windows": len(windows),
                    },
                    sort_keys=True,
                ),
                "priority": DISCOVERY_PRIORITY,
                "est_requests": len(rows),
                "total_tasks": len(rows),
                "created_by": created_by,
                "created_at": now,
            },
        )
        connection.execute(
            text(f"INSERT INTO task ({columns}) VALUES ({placeholders})"), rows
        )
    return identifier


@router.post(
    "/underlyings/{underlying_id}/expiries/discover",
    response_model=DiscoverAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Discover which expiries exist over a range",
)
async def discover_expiries(
    underlying_id: int,
    body: DiscoverRequest,
    user: CurrentUserDep,
    engine: EngineDep,
    supervisor: SupervisorDep,
    _broker: TokenBrokerDep,
    limiter: RateLimiterDep,
) -> DiscoverAccepted:
    enforce_limit(limiter, CATALOG_MUTATION_LIMIT, user)
    row = await run_in_threadpool(_require_underlying, engine, underlying_id)
    exchange = str(row["exchange"]).upper()
    symbol = str(row["fyers_symbol"])

    if exchange == "MCX" or symbol.upper().startswith("MCX:"):
        raise ApiError(400, CODE_MCX_NOT_SUPPORTED, MCX_REFUSAL)
    if not _broker.has_valid_token():
        raise needs_reauth()

    floor = exchange_data_floor(exchange)
    if body.range_from < floor:
        raise ApiError(
            400,
            CODE_RANGE_BEFORE_FLOOR,
            f"{exchange} data starts on {floor.isoformat()}. A range that starts earlier would "
            "spend requests on windows the vendor has nothing in.",
            detail={"floor": floor.isoformat(), "range_from": body.range_from.isoformat()},
        )

    # Measured: a window ending today, or later, answers HTTP 422 code -50. The endpoint serves
    # expired contracts only, so the clamp turns a guaranteed refusal into the answer asked for.
    latest = last_served_day(datetime.now().date())
    range_to = min(body.range_to, latest)
    clamped = range_to != body.range_to
    if range_to < body.range_from:
        raise ApiError(
            400,
            CODE_RANGE_AFTER_LAST_SERVED,
            f"The newest day the expiry-dates endpoint serves is {latest.isoformat()}, which is "
            "before the start of that range.",
            detail={"last_served_day": latest.isoformat()},
        )

    windows = chunk_range(body.range_from, range_to, max_days=MAX_WINDOW_DAYS)
    job_id = await run_in_threadpool(
        enqueue_expiry_discovery,
        engine,
        underlying_id=underlying_id,
        fyers_symbol=symbol,
        windows=windows,
        created_by=f"user:{user.username}",
    )
    supervisor.notify(job_id)
    log.info(
        "expiry discovery requested",
        extra={
            "job_id": job_id,
            "underlying_id": underlying_id,
            "total_tasks": len(windows),
            "range_from": body.range_from.isoformat(),
            "range_to": range_to.isoformat(),
        },
    )
    return DiscoverAccepted(
        job_id=job_id,
        total_tasks=len(windows),
        est_requests=len(windows),
        range_from=body.range_from.isoformat(),
        range_to=range_to.isoformat(),
        clamped=clamped,
        windows=[
            DiscoverWindow(range_from=start.isoformat(), range_to=end.isoformat())
            for start, end in windows
        ],
    )


# ---------------------------------------------------------------------------
# One expiry's contracts
# ---------------------------------------------------------------------------


@router.get(
    "/expiries/{underlying_id}/{expiry_date}/contracts",
    response_model=ContractPage,
    summary="The contracts of one expiry",
)
async def list_expiry_contracts(
    underlying_id: int,
    expiry_date: date,
    _user: CurrentUserDep,
    engine: EngineDep,
    reader: ReaderDep,
    kind: str | None = Query(default=None, pattern="^(FUT|OPT)$"),
    option_type: str | None = Query(default=None, pattern="^(CE|PE)$"),
    strike_min: float | None = Query(default=None, ge=0),
    strike_max: float | None = Query(default=None, ge=0),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=catalog_queries.DEFAULT_PAGE, ge=1, le=catalog_queries.MAX_PAGE),
) -> ContractPage:
    await run_in_threadpool(_require_underlying, engine, underlying_id)

    after: int | None = None
    if cursor:
        after = int(decode_cursor(cursor, expect=("contract_id",))["contract_id"])

    page = await catalog_queries.list_expiry_contracts(
        reader,
        underlying_id=underlying_id,
        expiry_date=expiry_date,
        kind=kind,
        option_type=option_type,
        strike_min=strike_min,
        strike_max=strike_max,
        cursor=after,
        limit=limit,
    )
    return await render_contract_page(reader, page)
