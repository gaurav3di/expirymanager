"""Underlyings: list, resolve, register, patch and delete. API.md section 3.

Add Underlying is the only route in this file with teeth, and everything it does is about making
a failure happen here, cheaply, instead of six hours into a backfill.

**A root is resolved from the symbol master, never guessed from the string.** Nothing inside a
derivative symbol names its cash ticker: BANKNIFTY is quoted as NSE:NIFTYBANK-INDEX and SENSEX as
BSE:SENSEX-INDEX. The join that knows this is `under_fytoken`, from the FO master into the CM
master, and `build_root_candidates` in the symbol master module is the one implementation of it.
This route calls it rather than repeating it, so a fix there is a fix here.

**MCX is refused with the measured reason.** The 2026-09-09 probe run put seven MCX underlying
forms through the expired endpoints and every one answered HTTP 422 while BSE:SENSEX-INDEX
answered 200 in the same run. So MCX is not offered by `/resolve` at all, and an MCX symbol sent
to `POST /underlyings` is refused with the text the pipeline uses, not with a generic 400. An MCX
underlying registered here would be an underlying that can never be downloaded, and it would
spend one refused request per contract per night finding that out again.

**A root the parser cannot handle is refused, and said so.** The measured case is the 360ONE
class: a root that begins with a digit, which both `roots.py` and `symbology.py` reject because a
leading digit is ambiguous against a strike. `build_root_candidates` already records those as
rejections rather than raising, and this route returns them to the caller. Accepting one would
produce a registry row whose every contract symbol then failed to parse during discovery, which
is a failure with no path back to the screen that caused it.

**The registry and its mirror are written together.** `sqlite.underlying_registry` is what the
user declared; `dim_underlying` is the DuckDB mirror that nine separate read paths join to. They
have been out of step before, silently, on a fresh install, and the symptom was nine empty
answers and no error anywhere. So the mirror write happens first, because it is the thing that
allocates the reserved spot contract id, and a registry row is never written without one.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, Query, Response, status
from limits import parse
from sqlalchemy import Engine, text
from starlette.concurrency import run_in_threadpool

from expirymanager.api.deps import (
    CurrentUser,
    CurrentUserDep,
    EngineDep,
    RateLimiterDep,
    ReaderDep,
    StateDep,
    WriterDep,
)
from expirymanager.api.errors import ApiError, not_found
from expirymanager.api.schemas.catalog import (
    CreateUnderlyingRequest,
    ResolveCandidate,
    ResolveProbe,
    ResolveRejection,
    ResolveRequest,
    ResolveResponse,
    UnderlyingOut,
    UpdateUnderlyingRequest,
    as_int,
    iso_text,
)
from expirymanager.brokers.fyers import endpoints as ep
from expirymanager.brokers.fyers import roots as roots_module
from expirymanager.brokers.fyers.calendar import exchange_data_floor
from expirymanager.brokers.fyers.roots import SEGMENT_CODES, UnderlyingRoot
from expirymanager.brokers.fyers.symbol_master import RootCandidate, build_root_candidates
from expirymanager.brokers.fyers.symbology import EXCHANGE_CODES
from expirymanager.db.ids import SPOT_ID_MAX, SpotIdSpaceExhausted
from expirymanager.db.queries import catalog as catalog_queries
from expirymanager.db.writes import UnderlyingRow, delete_underlying, upsert_underlying
from expirymanager.pipeline.handlers.candle_chunk import MCX_REFUSAL
from expirymanager.pipeline.handlers.expiry_discovery import (
    MAX_WINDOW_DAYS,
    expiry_dates_from,
    last_served_day,
)
from expirymanager.security.ratelimit import (
    ENFORCED_BY_ROUTE,
    SCOPE_SESSION,
    RateLimited,
    RateLimiter,
    RouteLimit,
)

__all__ = [
    "router",
    "RESOLVE_LIMIT",
    "CATALOG_MUTATION_LIMIT",
    "enforce_limit",
    "CODE_MCX_NOT_SUPPORTED",
    "CODE_NO_CANDIDATES",
    "CODE_UNPARSEABLE_ROOT",
    "CODE_UNRESOLVED_ROOT",
    "CODE_ALREADY_EXISTS",
    "CODE_BUILTIN_UNDERLYING",
    "CODE_HAS_RUNNING_JOBS",
    "CODE_SPOT_ID_SPACE_EXHAUSTED",
    "LIVE_JOB_STATUSES",
    "resolve_candidates",
    "read_registry_rows",
]

log = logging.getLogger(__name__)

router = APIRouter()

CODE_MCX_NOT_SUPPORTED = "mcx_not_supported"
CODE_NO_CANDIDATES = "no_candidates"
CODE_UNPARSEABLE_ROOT = "unparseable_root"
CODE_UNRESOLVED_ROOT = "unresolved_root"
CODE_ALREADY_EXISTS = "already_exists"
CODE_BUILTIN_UNDERLYING = "builtin_underlying"
CODE_HAS_RUNNING_JOBS = "has_running_jobs"
CODE_SPOT_ID_SPACE_EXHAUSTED = "spot_id_space_exhausted"

# The exchanges the expired F and O endpoints actually serve. MCX is absent by measurement, not
# by preference: see the module docstring and docs/API-PROBES.md section 3.
SERVED_EXCHANGES: tuple[str, ...] = ("NSE", "BSE")
SERVED_EXCHANGE_CODES: tuple[int, ...] = tuple(EXCHANGE_CODES[name] for name in SERVED_EXCHANGES)

# A job in any of these states still owns its tasks, so deleting the underlying under it would
# leave the worker holding a contract id that no longer resolves.
LIVE_JOB_STATUSES: tuple[str, ...] = (
    "draft",
    "queued",
    "running",
    "paused",
    "blocked_auth",
    "blocked_rate",
    "deferred_budget",
)

# How many roots a single resolve query may return. A one letter query matches most of the master.
MAX_CANDIDATES = 25

# The window the resolve probe asks for. One request, inside the measured 366 day ceiling, ending
# on the newest day the endpoint serves. Both bounds are computed, never hardcoded.
PROBE_WINDOW_DAYS = MAX_WINDOW_DAYS - 1

_REGISTRY_COLUMNS = (
    "underlying_id",
    "fyers_symbol",
    "root",
    "exchange",
    "segment",
    "instrument_kind",
    "display_name",
    "data_from",
    "default_resolutions",
    "include_oi",
    "option_life_days",
    "future_life_days",
    "spot_contract_id",
    "resolved_root_echo",
    "resolved_at",
    "is_builtin",
    "is_active",
)

_REGISTRY_SELECT = f"SELECT {', '.join(_REGISTRY_COLUMNS)} FROM underlying_registry"


# The two limits API.md names for this file. They are declared here and marked ENFORCED_BY_ROUTE
# rather than added to `security/ratelimit.ROUTE_LIMITS`, because that table is the middleware's
# and the middleware would then own a rule whose route is defined somewhere else. `rule_for` skips
# a route-enforced rule, so there is no double count.
RESOLVE_LIMIT = RouteLimit(
    name="underlying_resolve",
    limit=parse("20/minute"),
    scope=SCOPE_SESSION,
    methods=frozenset({"POST"}),
    pattern=re.compile(r"^(?:/api/v1)?/underlyings/resolve/?$"),
    enforced_by=ENFORCED_BY_ROUTE,
)

# Registering an underlying and asking for expiry discovery share one bucket: both are catalog
# mutations, and 30 a minute is far above any human use of either.
CATALOG_MUTATION_LIMIT = RouteLimit(
    name="catalog_mutations",
    limit=parse("30/minute"),
    scope=SCOPE_SESSION,
    methods=frozenset({"POST"}),
    pattern=re.compile(r"^(?:/api/v1)?/underlyings(?:/.*)?$"),
    enforced_by=ENFORCED_BY_ROUTE,
)


def enforce_limit(limiter: RateLimiter, rule: RouteLimit, user: CurrentUser) -> None:
    """Consume one unit of a route declared limit, or raise the documented 429.

    Keyed the same way the middleware keys a session scoped rule, so a route limit and the global
    fallback describe the same caller rather than two different ones.
    """
    decision = limiter.hit(rule, f"session:{user.session.id_hash.hex()}")
    if not decision.allowed:
        raise RateLimited(decision)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def read_registry_rows(
    engine: Engine, *, underlying_id: int | None = None, active_only: bool = False
) -> list[dict[str, Any]]:
    """The declared registry, which is authoritative for what the user asked for."""
    where: list[str] = []
    params: dict[str, Any] = {}
    if underlying_id is not None:
        where.append("underlying_id = :underlying_id")
        params["underlying_id"] = underlying_id
    if active_only:
        where.append("is_active = 1")
    sql = _REGISTRY_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY underlying_id"
    with engine.connect() as connection:
        rows = connection.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def _resolutions_of(raw: Any) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        # A row whose resolutions cannot be parsed still has to render. Returning an empty list
        # makes the damage visible on screen rather than 500ing the whole listing.
        log.warning("underlying default_resolutions is not valid json")
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip().upper() for item in parsed if str(item).strip()]


def _underlying_out(row: Mapping[str, Any], mirror: Mapping[str, Any] | None) -> UnderlyingOut:
    return UnderlyingOut(
        underlying_id=int(row["underlying_id"]),
        fyers_symbol=str(row["fyers_symbol"]),
        root=str(row["root"]),
        exchange=str(row["exchange"]),
        segment=str(row["segment"]),
        instrument_kind=str(row["instrument_kind"]),
        display_name=str(row["display_name"]),
        data_from=str(row["data_from"]),
        default_resolutions=_resolutions_of(row["default_resolutions"]),
        include_oi=bool(row["include_oi"]),
        option_life_days=int(row["option_life_days"]),
        future_life_days=int(row["future_life_days"]),
        spot_contract_id=int(row["spot_contract_id"]),
        resolved_root_echo=row["resolved_root_echo"],
        resolved_at=row["resolved_at"],
        is_builtin=bool(row["is_builtin"]),
        is_active=bool(row["is_active"]),
        mirrored=mirror is not None,
        expiry_count=as_int(mirror.get("expiry_count") if mirror else None),
        contract_count=as_int(mirror.get("contract_count") if mirror else None),
        first_expiry=iso_text(mirror.get("first_expiry") if mirror else None),
        last_expiry=iso_text(mirror.get("last_expiry") if mirror else None),
        spot_bars=as_int(mirror.get("spot_bars") if mirror else None),
        spot_last_ts=iso_text(mirror.get("spot_last_ts") if mirror else None),
    )


async def _render(reader: Any, rows: Sequence[Mapping[str, Any]]) -> list[UnderlyingOut]:
    mirrors = {
        int(item["underlying_id"]): item
        for item in await catalog_queries.list_underlyings(reader)
    }
    return [_underlying_out(row, mirrors.get(int(row["underlying_id"]))) for row in rows]


@router.get(
    "/underlyings",
    response_model=list[UnderlyingOut],
    summary="Every registered underlying with its rollups",
)
async def list_underlyings(
    _user: CurrentUserDep,
    engine: EngineDep,
    reader: ReaderDep,
    active_only: bool = Query(default=False),
) -> list[UnderlyingOut]:
    rows = await run_in_threadpool(read_registry_rows, engine, active_only=active_only)
    return await _render(reader, rows)


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------


def _refuse_mcx(value: str) -> None:
    """Refuse an MCX symbol or exchange with the measured reason, before anything is spent."""
    upper = value.strip().upper()
    if upper == "MCX" or upper.startswith("MCX:"):
        raise ApiError(400, CODE_MCX_NOT_SUPPORTED, MCX_REFUSAL)


_DERIVATIVE_SQL = f"""
SELECT under_symbol, exchange_code,
       any_value(under_fytoken) AS under_fytoken,
       any_value(segment_code)  AS segment_code,
       count(*)                 AS contract_count
  FROM dim_instrument_master
 WHERE valid_to IS NULL
   AND segment_code = ?
   AND exchange_code IN ({', '.join(str(code) for code in SERVED_EXCHANGE_CODES)})
   AND under_symbol IS NOT NULL AND under_fytoken IS NOT NULL
   AND (contains(upper(under_symbol), ?)
        OR under_fytoken IN (SELECT fytoken FROM dim_instrument_master
                              WHERE valid_to IS NULL AND segment_code = ?
                                AND contains(upper(symbol_ticker), ?)))
 GROUP BY under_symbol, exchange_code
 ORDER BY contract_count DESC
 LIMIT ?
"""


def _search_term(query: str) -> str:
    """The body of a full Fyers symbol, or the query itself.

    A user who pastes NSE:NIFTY50-INDEX means NIFTY50-INDEX. Searching for the whole string
    including the exchange prefix would match nothing, because the master stores the prefix in a
    column of its own.
    """
    value = query.strip().upper()
    if ":" in value:
        value = value.partition(":")[2]
    return value


async def resolve_candidates(
    reader: Any, query: str, *, limit: int = MAX_CANDIDATES
) -> tuple[tuple[RootCandidate, ...], tuple[Any, ...]]:
    """Search the latest symbol master snapshot for roots matching `query`.

    Reads only, costs zero Fyers requests. The grouping and the `under_fytoken` join into the
    cash master are `build_root_candidates`, which also records the roots it had to refuse.
    """
    term = _search_term(query)
    derivative = await reader.fetch_all(
        _DERIVATIVE_SQL,
        [SEGMENT_CODES["FO"], term, SEGMENT_CODES["CM"], term, int(limit)],
    )
    derivative_rows = [
        {
            "under_symbol": row[0],
            "exchange_code": row[1],
            "under_fytoken": row[2],
            "segment_code": row[3],
            "contract_count": row[4],
        }
        for row in derivative
    ]
    if not derivative_rows:
        return ((), ())

    tokens = sorted({str(row["under_fytoken"]) for row in derivative_rows})
    placeholders = ", ".join("?" for _ in tokens)
    cash = await reader.fetch_all(
        "SELECT fytoken, symbol_ticker, ex_instrument_type, symbol_details"
        "  FROM dim_instrument_master"
        f" WHERE valid_to IS NULL AND segment_code = ? AND fytoken IN ({placeholders})",
        [SEGMENT_CODES["CM"], *tokens],
    )
    cash_rows = [
        {
            "fytoken": row[0],
            "symbol_ticker": row[1],
            "ex_instrument_type": row[2],
            "symbol_details": row[3],
        }
        for row in cash
    ]
    return build_root_candidates(derivative_rows, cash_rows)


def _rank(candidates: Sequence[RootCandidate], query: str) -> list[RootCandidate]:
    """Exact root first, then a prefix match, then the deepest chain."""
    term = _search_term(query)

    def key(candidate: RootCandidate) -> tuple[int, int, str]:
        if candidate.root == term:
            rank = 0
        elif candidate.root.startswith(term) or candidate.fyers_symbol.endswith(term):
            rank = 1
        else:
            rank = 2
        return (rank, -candidate.contract_count, candidate.root)

    return sorted(candidates, key=key)


async def _probe(state: Any, symbol: str) -> ResolveProbe:
    """Spend exactly one governed expiry-dates request and read `data.symbol` back.

    The echo is the authoritative root. A probe that cannot run is reported as not attempted
    rather than as a failure of the whole resolve, because the symbol master search is local and
    is still a useful answer without it.
    """
    broker = state.token_broker
    client = state.fyers_client
    if broker is None or client is None or not broker.has_valid_token():
        return ResolveProbe(
            attempted=False,
            symbol=symbol,
            reason="needs_reauth",
        )

    today = datetime.now().date()
    range_to = last_served_day(today)
    range_from = range_to - timedelta(days=PROBE_WINDOW_DAYS)
    probe = ResolveProbe(
        attempted=True,
        symbol=symbol,
        range_from=range_from.isoformat(),
        range_to=range_to.isoformat(),
    )
    try:
        response = await ep.expiry_dates(
            client, symbol=symbol, range_from=range_from, range_to=range_to
        )
    except Exception as exc:  # noqa: BLE001 - a probe failure must not lose the local answer
        log.warning(
            "the resolve probe could not be sent",
            extra={"fyers_symbol": symbol, "reason": type(exc).__name__},
        )
        return probe.model_copy(update={"reason": "probe_failed"})

    if not response.ok:
        # The vendor message is not echoed: a Fyers error body is untrusted text on its way to a
        # browser console. The code is enough for the caller to tell one refusal from another.
        return probe.model_copy(
            update={"reason": f"fyers_rejected_{response.code}" if response.code else "fyers_rejected"}
        )

    payload = response.data
    echo = payload.get("symbol") if isinstance(payload, Mapping) else None
    futures, options = expiry_dates_from(payload)
    return probe.model_copy(
        update={
            "root_echo": str(echo).strip() if isinstance(echo, str) and echo.strip() else None,
            "expiry_count": len(set(futures) | set(options)),
        }
    )


@router.post(
    "/underlyings/resolve",
    response_model=ResolveResponse,
    summary="Resolve a typed root to a real Fyers underlying",
)
async def resolve_underlying(
    body: ResolveRequest,
    user: CurrentUserDep,
    state: StateDep,
    engine: EngineDep,
    reader: ReaderDep,
    limiter: RateLimiterDep,
) -> ResolveResponse:
    # Before the MCX check and before any read: this route can spend a Fyers request, so the
    # ceiling has to apply to every call including the ones that end in a refusal.
    enforce_limit(limiter, RESOLVE_LIMIT, user)
    _refuse_mcx(body.query)

    candidates, rejected = await resolve_candidates(reader, body.query)
    rejections = [
        ResolveRejection(
            root=item.root,
            exchange=item.exchange,
            reason=item.reason,
            fo_contract_count=item.contract_count,
        )
        for item in rejected
    ]

    if not candidates:
        if rejections:
            raise ApiError(
                400,
                CODE_UNPARSEABLE_ROOT,
                "That underlying exists at the exchange but its root cannot be parsed by this "
                "application, so its contract symbols would not survive discovery. A root has to "
                "start with a letter; the known case is the 360ONE class, which starts with a "
                "digit.",
                detail={"rejected": [item.model_dump() for item in rejections]},
            )
        raise ApiError(
            404,
            CODE_NO_CANDIDATES,
            "No NSE or BSE underlying in the current symbol master matches that. Download a "
            "symbol master snapshot if none has been taken, and remember that MCX is not served "
            "by the expired endpoints at all.",
        )

    ranked = _rank(candidates, body.query)
    registered = await run_in_threadpool(_registered_symbols, engine)
    probe = await _probe(state, ranked[0].fyers_symbol)

    return ResolveResponse(
        candidates=[
            ResolveCandidate(
                fyers_symbol=item.fyers_symbol,
                root=item.root,
                exchange=item.exchange,
                segment=item.derivative_segment,
                instrument_kind=item.instrument_kind,
                display_name=item.display_name,
                under_fytoken=item.under_fytoken,
                fo_contract_count=item.contract_count,
                already_registered=item.fyers_symbol in registered,
            )
            for item in ranked
        ],
        rejected=rejections,
        probe=probe,
    )


def _registered_symbols(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT fyers_symbol FROM underlying_registry")).scalars()
        return {str(value).upper() for value in rows}


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


def _register_root(candidate: RootCandidate, *, underlying_id: int, spot_contract_id: int) -> None:
    """Teach the process wide symbology registry the new root.

    Not decoration. Two weekly decompositions of NSE:NIFTY2292217000CE are syntactically valid,
    and knowing which strings are real roots is what picks the right one. Without this the
    contract discovery that follows a registration would parse every symbol of the new underlying
    against a registry that has never heard of it.
    """
    entry = UnderlyingRoot(
        root=candidate.root,
        fyers_symbol=candidate.fyers_symbol,
        display_name=candidate.display_name,
        exchange=candidate.exchange,
        instrument_kind=candidate.instrument_kind,
        derivative_segment=candidate.derivative_segment,
        data_from=exchange_data_floor(candidate.exchange),
        underlying_id=underlying_id,
        spot_contract_id=spot_contract_id,
    )
    roots_module.default_registry().register(entry, replace_existing=True)


def _insert_registry_row(
    engine: Engine,
    *,
    underlying_id: int,
    spot_contract_id: int,
    candidate: RootCandidate,
    body: CreateUnderlyingRequest,
    data_from: date,
) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO underlying_registry (underlying_id, fyers_symbol, root, exchange,"
                " segment, instrument_kind, display_name, data_from, default_resolutions,"
                " include_oi, option_life_days, future_life_days, spot_contract_id, is_builtin,"
                " is_active, created_at, updated_at)"
                " VALUES (:underlying_id, :fyers_symbol, :root, :exchange, :segment,"
                " :instrument_kind, :display_name, :data_from, :default_resolutions,"
                " :include_oi, :option_life_days, :future_life_days, :spot_contract_id, 0, 1,"
                " :now, :now)"
            ),
            {
                "underlying_id": underlying_id,
                "fyers_symbol": candidate.fyers_symbol,
                "root": candidate.root,
                "exchange": candidate.exchange,
                # The cash segment of the ticker, matching the four seeded rows. The derivative
                # segment lives on the root registry entry, where the parser reads it.
                "segment": "CM",
                "instrument_kind": candidate.instrument_kind,
                "display_name": body.display_name or candidate.display_name,
                "data_from": data_from.isoformat(),
                "default_resolutions": json.dumps(body.default_resolutions),
                "include_oi": 1 if body.include_oi else 0,
                "option_life_days": body.option_life_days,
                "future_life_days": body.future_life_days,
                "spot_contract_id": spot_contract_id,
                "now": now,
            },
        )


def _next_underlying_id(engine: Engine) -> int:
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT coalesce(max(underlying_id), 0) FROM underlying_registry")
        ).scalar()
    return int(value or 0) + 1


@router.post(
    "/underlyings",
    response_model=UnderlyingOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register an underlying",
)
async def create_underlying(
    body: CreateUnderlyingRequest,
    user: CurrentUserDep,
    engine: EngineDep,
    reader: ReaderDep,
    writer: WriterDep,
    limiter: RateLimiterDep,
) -> UnderlyingOut:
    enforce_limit(limiter, CATALOG_MUTATION_LIMIT, user)
    _refuse_mcx(body.fyers_symbol)

    existing = await run_in_threadpool(_registered_symbols, engine)
    if body.fyers_symbol in existing:
        raise ApiError(
            409,
            CODE_ALREADY_EXISTS,
            f"{body.fyers_symbol} is already registered.",
        )

    candidates, rejected = await resolve_candidates(reader, body.fyers_symbol)
    candidate = next(
        (item for item in candidates if item.fyers_symbol == body.fyers_symbol), None
    )
    if candidate is None:
        refusal = next(
            (item for item in rejected if item.root in body.fyers_symbol), None
        )
        if refusal is not None:
            raise ApiError(
                400,
                CODE_UNPARSEABLE_ROOT,
                f"The root {refusal.root} cannot be parsed by this application, so every "
                "contract symbol filed under it would fail during discovery.",
                detail={"root": refusal.root, "reason": refusal.reason},
            )
        raise ApiError(
            400,
            CODE_UNRESOLVED_ROOT,
            f"{body.fyers_symbol} does not resolve to a derivative root in the current symbol "
            "master. Call /underlyings/resolve first and register one of the symbols it returns.",
        )

    underlying_id = await run_in_threadpool(_next_underlying_id, engine)
    data_from = exchange_data_floor(candidate.exchange)
    exchange_code = EXCHANGE_CODES[candidate.exchange]

    try:
        # The mirror first, because it owns the reserved spot id allocator. A registry row must
        # never be written without the id it will be joined on.
        result = await upsert_underlying(
            writer,
            UnderlyingRow(
                underlying_id=underlying_id,
                fyers_symbol=candidate.fyers_symbol,
                root=candidate.root,
                exchange=candidate.exchange,
                exchange_code=exchange_code,
                segment="CM",
                segment_code=SEGMENT_CODES["CM"],
                instrument_kind=candidate.instrument_kind,
                display_name=body.display_name or candidate.display_name,
                data_from=data_from,
                spot_contract_id=None,
                underlying_fytoken=candidate.under_fytoken,
            ),
        )
    except SpotIdSpaceExhausted as exc:
        raise ApiError(
            507,
            CODE_SPOT_ID_SPACE_EXHAUSTED,
            f"All {SPOT_ID_MAX} reserved spot contract ids are in use. More than {SPOT_ID_MAX} "
            "underlyings is not a supported configuration.",
        ) from exc

    try:
        await run_in_threadpool(
            _insert_registry_row,
            engine,
            underlying_id=underlying_id,
            spot_contract_id=result.spot_contract_id,
            candidate=candidate,
            body=body,
            data_from=data_from,
        )
    except Exception:
        # The mirror row is meaningless without the registry row that declares it, and leaving it
        # behind would hand the next registration a spot id that is taken by nothing.
        await delete_underlying(writer, underlying_id, purge_data=False)
        raise

    _register_root(
        candidate, underlying_id=underlying_id, spot_contract_id=result.spot_contract_id
    )
    log.info(
        "underlying registered",
        extra={
            "underlying_id": underlying_id,
            "fyers_symbol": candidate.fyers_symbol,
            "root": candidate.root,
            "spot_contract_id": result.spot_contract_id,
        },
    )

    rows = await run_in_threadpool(read_registry_rows, engine, underlying_id=underlying_id)
    rendered = await _render(reader, rows)
    return rendered[0]


# ---------------------------------------------------------------------------
# Patch and delete
# ---------------------------------------------------------------------------


def _require_row(engine: Engine, underlying_id: int) -> dict[str, Any]:
    rows = read_registry_rows(engine, underlying_id=underlying_id)
    if not rows:
        raise not_found(f"There is no underlying {underlying_id}.")
    return rows[0]


def _apply_patch(
    engine: Engine, underlying_id: int, body: UpdateUnderlyingRequest
) -> dict[str, Any]:
    row = _require_row(engine, underlying_id)
    assignments: dict[str, Any] = {}
    if "display_name" in body.model_fields_set and body.display_name is not None:
        assignments["display_name"] = body.display_name
    if "default_resolutions" in body.model_fields_set and body.default_resolutions is not None:
        assignments["default_resolutions"] = json.dumps(body.default_resolutions)
    if "include_oi" in body.model_fields_set and body.include_oi is not None:
        assignments["include_oi"] = 1 if body.include_oi else 0
    if "option_life_days" in body.model_fields_set and body.option_life_days is not None:
        assignments["option_life_days"] = body.option_life_days
    if "future_life_days" in body.model_fields_set and body.future_life_days is not None:
        assignments["future_life_days"] = body.future_life_days
    if "is_active" in body.model_fields_set and body.is_active is not None:
        assignments["is_active"] = 1 if body.is_active else 0

    if assignments:
        clause = ", ".join(f"{column} = :{column}" for column in assignments)
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"UPDATE underlying_registry SET {clause}, updated_at = :updated_at"
                    " WHERE underlying_id = :underlying_id"
                ),
                {**assignments, "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                 "underlying_id": underlying_id},
            )
    return {**row, **assignments}


@router.patch(
    "/underlyings/{underlying_id}",
    response_model=UnderlyingOut,
    summary="Update an underlying",
)
async def patch_underlying(
    underlying_id: int,
    body: UpdateUnderlyingRequest,
    _user: CurrentUserDep,
    engine: EngineDep,
    reader: ReaderDep,
    writer: WriterDep,
) -> UnderlyingOut:
    row = await run_in_threadpool(_apply_patch, engine, underlying_id, body)

    # Re-mirror unconditionally. `display_name` and `is_active` are both read off dim_underlying
    # by the chain, the export and the catalog joins, and the mirror has silently drifted from
    # the registry before. Rewriting it costs one small transaction and removes the question.
    await upsert_underlying(
        writer,
        UnderlyingRow(
            underlying_id=underlying_id,
            fyers_symbol=str(row["fyers_symbol"]),
            root=str(row["root"]),
            exchange=str(row["exchange"]).upper(),
            exchange_code=EXCHANGE_CODES[str(row["exchange"]).upper()],
            segment=str(row["segment"]).upper(),
            segment_code=SEGMENT_CODES[str(row["segment"]).upper()],
            instrument_kind=str(row["instrument_kind"]),
            display_name=str(row["display_name"]),
            data_from=date.fromisoformat(str(row["data_from"])),
            spot_contract_id=int(row["spot_contract_id"]),
            is_active=bool(row["is_active"]),
        ),
    )

    rows = await run_in_threadpool(read_registry_rows, engine, underlying_id=underlying_id)
    rendered = await _render(reader, rows)
    return rendered[0]


def _live_job_count(engine: Engine, underlying_id: int) -> int:
    placeholders = ", ".join(f":s{index}" for index in range(len(LIVE_JOB_STATUSES)))
    params: dict[str, Any] = {
        f"s{index}": value for index, value in enumerate(LIVE_JOB_STATUSES)
    }
    params["underlying_id"] = underlying_id
    with engine.connect() as connection:
        value = connection.execute(
            text(
                "SELECT count(DISTINCT j.job_id) FROM job j JOIN task t USING (job_id)"
                " WHERE t.underlying_id = :underlying_id"
                f"   AND j.status IN ({placeholders})"
            ),
            params,
        ).scalar()
    return int(value or 0)


def _delete_registry_row(engine: Engine, underlying_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM underlying_registry WHERE underlying_id = :underlying_id"),
            {"underlying_id": underlying_id},
        )


@router.delete(
    "/underlyings/{underlying_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an underlying",
)
async def remove_underlying(
    underlying_id: int,
    _user: CurrentUserDep,
    engine: EngineDep,
    writer: WriterDep,
    purge_data: bool = Query(default=False),
) -> Response:
    row = await run_in_threadpool(_require_row, engine, underlying_id)
    if bool(row["is_builtin"]):
        raise ApiError(
            409,
            CODE_BUILTIN_UNDERLYING,
            "A builtin underlying cannot be deleted. Deactivate it instead, which hides it "
            "everywhere without touching a single bar.",
        )
    live = await run_in_threadpool(_live_job_count, engine, underlying_id)
    if live:
        raise ApiError(
            409,
            CODE_HAS_RUNNING_JOBS,
            f"{live} job(s) are still working on this underlying. Cancel them first.",
            detail={"jobs": live},
        )

    deleted = await delete_underlying(writer, underlying_id, purge_data=purge_data)
    await run_in_threadpool(_delete_registry_row, engine, underlying_id)
    roots_module.default_registry().unregister(str(row["root"]))
    log.info(
        "underlying deleted",
        extra={
            "underlying_id": underlying_id,
            "purge_data": purge_data,
            "candles_deleted": deleted,
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
