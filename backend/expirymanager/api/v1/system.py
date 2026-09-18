"""API.md section 10: budget, storage, health, maintenance, notifications, settings and the
request log.

Two of these are the numbers that decide whether a session goes well. `GET /system/budget` is the
figure on the top bar and it is read straight off `FyersGovernor.snapshot()`, because the governor
is the thing that actually refuses a request and any second counter would eventually disagree with
it. `GET /system/storage` is `db/maintenance.storage_report`, with the paths this process was
started with rather than a guess at where the files are.

Settings are read and written through the one `SettingsStore` the lifespan built. Not a second
store, not a direct UPDATE: the store validates against the spec, writes through and invalidates
its cache, and every other component in this process reads from that same cache. A route that
wrote the `settings` table directly would leave the governor running on the old value with no
indication anywhere that it had.

`optimise` and `backup` are the two operations that touch the whole database at once. Both are
refused while the pipeline is doing anything, both check free space before they start, and both
leave a notification row behind, because a 202 that a user navigated away from is otherwise an
operation with no visible outcome.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Query, Response, status
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from expirymanager.api.deps import (
    CurrentUserDep,
    EngineDep,
    GovernorDep,
    PathsDep,
    ReaderDep,
    SettingsDep,
    StateDep,
)
from expirymanager.api.errors import ApiError, CODE_SERVICE_UNAVAILABLE, not_found
from expirymanager.api.schemas.system import (
    DEFAULT_REQUEST_LOG_LIMIT,
    HEALTH_FAILING,
    HEALTH_OK,
    MAX_REQUEST_LOG_LIMIT,
    BackupAccepted,
    BackupRequest,
    BudgetResponse,
    CheckpointResponse,
    HealthResponse,
    HealthRow,
    MaintenanceRun,
    NotificationRow,
    OptimiseAccepted,
    OptimiseRequest,
    RequestLogRow,
    SettingDescriptor,
    StorageResponse,
    describe_settings,
)
from expirymanager.brokers.fyers import endpoints as fyers_endpoints
from expirymanager.brokers.fyers.symbol_master import MASTER_BASE_URL
from expirymanager.db import maintenance as maintenance_module
from expirymanager.pipeline.events import EVENT_NOTIFICATION
from expirymanager.pipeline.queue import iso_at, utc_now
from expirymanager.security.redaction import redact_value
from expirymanager.settings_store import SettingsError

__all__ = [
    "router",
    "CODE_CONFIRM_REQUIRED",
    "CODE_PIPELINE_BUSY",
    "CODE_INSUFFICIENT_DISK",
    "CODE_UNKNOWN_SETTING",
    "CODE_SETTING_OUT_OF_RANGE",
    "TASK_ENDPOINTS",
    "task_endpoint",
]

log = logging.getLogger(__name__)

router = APIRouter()

CODE_CONFIRM_REQUIRED = "confirm_required"
CODE_PIPELINE_BUSY = "pipeline_busy"
CODE_INSUFFICIENT_DISK = "insufficient_disk"
CODE_UNKNOWN_SETTING = "unknown_setting"
CODE_SETTING_OUT_OF_RANGE = "setting_out_of_range"

# One task row is one outbound request, so the request log can name the endpoint the row spent
# itself on. Taken from `brokers/fyers/endpoints.py` rather than written out here, so a path that
# changes there changes here too.
TASK_ENDPOINTS: dict[str, str] = {
    "expiry_dates": fyers_endpoints.EXPIRY_DATES.path,
    "underlying_symbols": fyers_endpoints.UNDERLYING_SYMBOLS.path,
    "candle_chunk": fyers_endpoints.EXPIRED_HISTORICAL_DATA.path,
    "spot_chunk": fyers_endpoints.HISTORY.path,
    "chain_snapshot": fyers_endpoints.OPTIONS_CHAIN.path,
    # Not an API endpoint. The seven master files are plain unauthenticated objects, which is
    # exactly why the symbol master survives a dead token, and the log should say so.
    "symbol_master": MASTER_BASE_URL,
}

# The states that mean the pipeline still owns the database. Both whole-database operations refuse
# while any of them is present.
_LIVE_JOB_STATUSES = ("queued", "running", "paused", "blocked_auth", "blocked_rate")


def task_endpoint(kind: str) -> str:
    return TASK_ENDPOINTS.get(str(kind), str(kind))


def _duck_store(state: StateDep) -> Any:
    if state.duck is None:
        raise ApiError(
            503,
            CODE_SERVICE_UNAVAILABLE,
            "The market database is not available. The server did not finish starting.",
        )
    return state.duck


# ---------------------------------------------------------------------------
# Budget, storage and health
# ---------------------------------------------------------------------------


def budget_body(governor: Any, settings: Any) -> BudgetResponse:
    """The `/system/budget` body, which is byte for byte the `budget` SSE frame.

    Exported as a function rather than inlined in the route so that anything publishing the frame
    builds it from the same place. Two constructions of this body would drift, and the top bar
    would then show one number over REST and a different one over the stream.
    """
    snapshot = governor.snapshot()
    return BudgetResponse(
        ist_date=snapshot.ist_date,
        plan=snapshot.plan,
        requests_used=snapshot.requests_used,
        plan_limit_day=snapshot.plan_limit_day,
        remaining=snapshot.requests_remaining,
        # The per minute ceiling the governor holds itself to, which is below the published plan
        # limit on purpose. It is what a caller has to stay under, not what is left this minute.
        minute_headroom=snapshot.per_minute,
        minute_violations=snapshot.minute_violations,
        strikes_remaining=snapshot.strikes_remaining,
        blocked_until=snapshot.blocked_until,
        pipeline_mode=str(snapshot.mode),
        pipeline_reason=snapshot.reason,
        sweep_reserve_fraction=settings.get_float("budget_reserve_fraction"),
    )


@router.get("/system/budget", response_model=BudgetResponse, summary="Request budget")
async def read_budget(
    user: CurrentUserDep,
    governor: GovernorDep,
    settings: SettingsDep,
) -> BudgetResponse:
    return budget_body(governor, settings)


@router.get("/system/storage", response_model=StorageResponse, summary="Storage report")
async def read_storage(
    user: CurrentUserDep,
    state: StateDep,
    reader: ReaderDep,
    paths: PathsDep,
) -> StorageResponse:
    _duck_store(state)
    report = await maintenance_module.storage_report(
        reader,
        db_path=paths.duckdb_file,
        sqlite_path=paths.sqlite_db,
        exports_dir=paths.exports_dir,
        raw_dir=paths.raw_dir,
    )
    return StorageResponse(
        duckdb_bytes=report.duckdb_bytes,
        duckdb_wal_bytes=report.duckdb_wal_bytes,
        sqlite_bytes=report.sqlite_bytes,
        exports_bytes=report.exports_bytes,
        raw_payload_bytes=report.raw_payload_bytes,
        candle_rows=report.candle_rows,
        bytes_per_row=report.bytes_per_row,
        modelled_bytes=report.modelled_bytes,
        bloat_ratio=report.bloat_ratio,
        compaction_suggested=report.compaction_suggested,
        free_disk_bytes=report.free_disk_bytes,
    )


def _last_maintenance(engine: Any) -> MaintenanceRun | None:
    """The last fire of the maintenance schedule, whatever it is called or whoever moved it."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT r.fired_at, r.outcome, r.note FROM schedule_run r"
                    " JOIN schedule s ON s.schedule_id = r.schedule_id"
                    " WHERE s.kind = 'maintenance'"
                    " ORDER BY r.fired_at DESC, r.rowid DESC LIMIT 1"
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    return MaintenanceRun(
        ran_at=str(row["fired_at"]), outcome=str(row["outcome"]), detail=row["note"]
    )


@router.get("/system/health", response_model=HealthResponse, summary="Data health assertions")
async def read_health(
    user: CurrentUserDep,
    state: StateDep,
    reader: ReaderDep,
    engine: EngineDep,
) -> HealthResponse:
    _duck_store(state)
    checks = await maintenance_module.health_checks(reader)
    observed_at = datetime.now(UTC).isoformat()
    rows = []
    for check in checks:
        offending = int(check.get("offending") or 0)
        rows.append(
            HealthRow(
                check_name=str(check.get("check_name", "")),
                status=HEALTH_OK if offending == 0 else HEALTH_FAILING,
                offending=offending,
                detail=None if offending == 0 else f"{offending} offending rows",
                observed_at=observed_at,
            )
        )
    last = await run_in_threadpool(_last_maintenance, engine)
    return HealthResponse(rows=rows, last_maintenance=last)


# ---------------------------------------------------------------------------
# The whole-database operations
# ---------------------------------------------------------------------------


def _live_job_count(engine: Any) -> int:
    placeholders = ", ".join(f":s{index}" for index in range(len(_LIVE_JOB_STATUSES)))
    with engine.connect() as connection:
        row = connection.execute(
            text(f"SELECT count(*) FROM job WHERE status IN ({placeholders})"),
            {f"s{index}": value for index, value in enumerate(_LIVE_JOB_STATUSES)},
        ).first()
    return int(row[0]) if row else 0


async def _require_idle_pipeline(engine: Any, duck: Any) -> None:
    live = await run_in_threadpool(_live_job_count, engine)
    if live:
        raise ApiError(
            409,
            CODE_PIPELINE_BUSY,
            f"{live} jobs are still live. Let them finish or cancel them first.",
            detail={"live_jobs": live},
        )
    if duck.writer.depth:
        raise ApiError(
            409,
            CODE_PIPELINE_BUSY,
            "The database writer still has queued work.",
            detail={"writer_depth": duck.writer.depth},
        )


def write_notification(
    engine: Any, *, level: str, code: str, title: str, body: str | None = None
) -> str:
    """One notification row. The visible outcome of an operation nobody stayed to watch."""
    notification_id = str(uuid.uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO notification (notification_id, level, code, title, body, created_at)"
                " VALUES (:id, :level, :code, :title, :body, :created_at)"
            ),
            {
                "id": notification_id,
                "level": level,
                "code": code,
                "title": title,
                "body": body,
                "created_at": iso_at(utc_now()),
            },
        )
    return notification_id


def _publish_notification(state: Any, notification_id: str, level: str, title: str) -> None:
    supervisor = state.supervisor
    if supervisor is None:
        return
    supervisor.bus.publish(
        EVENT_NOTIFICATION,
        {
            "notification_id": notification_id,
            "level": level,
            "title": title,
        },
    )


@router.post(
    "/system/checkpoint", response_model=CheckpointResponse, summary="Fold the write ahead log in"
)
async def run_checkpoint(
    user: CurrentUserDep,
    state: StateDep,
) -> CheckpointResponse:
    duck = _duck_store(state)
    result = await maintenance_module.checkpoint(duck)
    return CheckpointResponse(
        wal_bytes_before=result.wal_bytes_before, wal_bytes_after=result.wal_bytes_after
    )


async def _optimise_and_settle(
    *, state: Any, engine: Any, duck: Any, operation_id: str
) -> None:
    """Quiesce the writer, rewrite the file, restart the writer, then say what happened.

    The writer is stopped rather than merely idle-checked, because the rewrite closes and reopens
    the live connection underneath it and a cursor held across that is a cursor onto a file that
    no longer exists.
    """
    supervisor = state.supervisor
    if supervisor is not None:
        await supervisor.dispatcher.pause("a database compaction is running")
    await duck.writer.stop()
    try:
        result = await maintenance_module.compact(duck)
    except maintenance_module.InsufficientDisk as exc:
        await run_in_threadpool(
            write_notification,
            engine,
            level="error",
            code="compaction_failed",
            title="Optimise could not run",
            body=str(exc),
        )
        return
    except Exception as exc:  # noqa: BLE001 - the user must be told, whatever went wrong
        log.exception("compaction failed", extra={"operation_id": operation_id})
        await run_in_threadpool(
            write_notification,
            engine,
            level="error",
            code="compaction_failed",
            title="Optimise failed",
            body=f"{type(exc).__name__}: {exc}"[:2000],
        )
        return
    finally:
        await duck.writer.start()
        if supervisor is not None:
            supervisor.dispatcher.resume()

    notification_id = await run_in_threadpool(
        write_notification,
        engine,
        level="info",
        code="compaction_done",
        title="Optimise finished",
        body=(
            f"{result.rows_copied} rows across {result.tables_copied} tables rewritten in "
            f"{result.duration_seconds} seconds. {result.bytes_before} bytes became "
            f"{result.bytes_after}."
        ),
    )
    _publish_notification(state, notification_id, "info", "Optimise finished")


@router.post(
    "/system/optimise",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OptimiseAccepted,
    summary="Rewrite the database to reclaim space",
)
async def run_optimise(
    body: OptimiseRequest,
    background: BackgroundTasks,
    user: CurrentUserDep,
    state: StateDep,
    engine: EngineDep,
    paths: PathsDep,
) -> OptimiseAccepted:
    """`job_id` here is an operation id and names no row in the `job` table.

    `job.kind` lists nine kinds and none of them is a compaction, correctly: a job is work made of
    tasks and a task is one outbound request, while this is a local file rewrite. The field keeps
    its documented name so the frontend has one correlation id to hold, and the outcome arrives as
    a notification rather than as a job row.
    """
    if not body.confirm:
        raise ApiError(
            400,
            CODE_CONFIRM_REQUIRED,
            "Optimise rewrites the whole database and closes it while it runs. Send "
            "confirm true to proceed.",
        )
    duck = _duck_store(state)
    await _require_idle_pipeline(engine, duck)

    db_path = paths.duckdb_file
    current = await run_in_threadpool(
        lambda: db_path.stat().st_size if db_path.exists() else 0
    )
    free = await run_in_threadpool(lambda: shutil.disk_usage(db_path.parent).free)
    needed = int(current * (1 + maintenance_module.COMPACTION_MARGIN))
    if needed > free:
        raise ApiError(
            507,
            CODE_INSUFFICIENT_DISK,
            "A rewrite needs the current database plus a margin free at once, and the disk "
            "does not have it.",
            detail={"needed_bytes": needed, "free_bytes": free},
        )

    operation_id = str(uuid.uuid4())
    background.add_task(
        _optimise_and_settle,
        state=state,
        engine=engine,
        duck=duck,
        operation_id=operation_id,
    )
    return OptimiseAccepted(job_id=operation_id, status="running", bytes_before=current)


def _copy_database(paths: Any, target_dir: Path) -> list[str]:
    """Copy the DuckDB file, its WAL and the SQLite file with its sidecars, together.

    The WAL is not optional. A copy of the .duckdb taken without it, or taken without a checkpoint
    first, restores a database missing the most recent writes and nothing about the restored file
    says so.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    candidates = [
        paths.duckdb_file,
        paths.duckdb_file.with_name(paths.duckdb_file.name + ".wal"),
        paths.sqlite_db,
        paths.sqlite_db.with_name(paths.sqlite_db.name + "-wal"),
        paths.sqlite_db.with_name(paths.sqlite_db.name + "-shm"),
    ]
    for source in candidates:
        if not source.exists():
            continue
        destination = target_dir / source.name
        shutil.copy2(source, destination)
        copied.append(destination.name)
    return copied


def _copy_database_offline(duck: Any, paths: Any, target_dir: Path) -> list[str]:
    """Close the live database, copy the files, and reopen it.

    Windows is where the close stops being optional: DuckDB holds the file there with a share mode
    that denies every other reader, so a copy taken while the store is open fails outright with a
    sharing violation and the backup produces nothing. POSIX has no mandatory locking and the same
    copy succeeds, which is the worse outcome of the two: a file copied byte by byte from
    underneath an open writer can land torn, and nothing about the restored file says so.

    So the close is not a Windows workaround. It is the guarantee both platforms should have had,
    and it is the same close and reopen the compaction path already does for the same reason.
    """
    duck.close()
    try:
        return _copy_database(paths, target_dir)
    finally:
        duck.open()


async def _backup_and_settle(
    *, state: Any, engine: Any, duck: Any, paths: Any, target_dir: Path
) -> None:
    """Quiesce the writer, copy the files with the database closed, then restart the writer.

    The endpoint refuses unless the pipeline is already idle, but it refuses at request time and
    this runs later, so the pause is what closes the window in between.
    """
    supervisor = state.supervisor
    try:
        await maintenance_module.checkpoint(duck)
        if supervisor is not None:
            await supervisor.dispatcher.pause("a database backup is running")
        await duck.writer.stop()
        try:
            copied = await run_in_threadpool(_copy_database_offline, duck, paths, target_dir)
        finally:
            await duck.writer.start()
            if supervisor is not None:
                supervisor.dispatcher.resume()
    except Exception as exc:  # noqa: BLE001 - the user must be told, whatever went wrong
        log.exception("backup failed", extra={"target_dir": str(target_dir)})
        await run_in_threadpool(
            write_notification,
            engine,
            level="error",
            code="backup_failed",
            title="Backup failed",
            body=f"{type(exc).__name__}: {exc}"[:2000],
        )
        return
    notification_id = await run_in_threadpool(
        write_notification,
        engine,
        level="info",
        code="backup_done",
        title="Backup finished",
        body=f"{len(copied)} files copied to {target_dir}: " + ", ".join(copied),
    )
    _publish_notification(state, notification_id, "info", "Backup finished")


@router.post(
    "/system/backup",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=BackupAccepted,
    summary="Checkpoint and copy the database",
)
async def run_backup(
    body: BackupRequest,
    background: BackgroundTasks,
    user: CurrentUserDep,
    state: StateDep,
    engine: EngineDep,
    paths: PathsDep,
) -> BackupAccepted:
    duck = _duck_store(state)
    await _require_idle_pipeline(engine, duck)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target_dir = (
        Path(body.target_dir).expanduser() / stamp
        if body.target_dir
        else paths.backups_dir / stamp
    )
    operation_id = str(uuid.uuid4())
    background.add_task(
        _backup_and_settle,
        state=state,
        engine=engine,
        duck=duck,
        paths=paths,
        target_dir=target_dir,
    )
    return BackupAccepted(job_id=operation_id, status="running", target_dir=str(target_dir))


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

_NOTIFICATION_COLUMNS = (
    "notification_id, level, code, title, body, created_at, read_at, dismissed_at"
)


def _read_notifications(engine: Any, *, unread_only: bool, limit: int) -> list[Any]:
    sql = f"SELECT {_NOTIFICATION_COLUMNS} FROM notification WHERE dismissed_at IS NULL"
    if unread_only:
        sql += " AND read_at IS NULL"
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT :limit"
    with engine.connect() as connection:
        return connection.execute(text(sql), {"limit": limit}).mappings().all()


@router.get(
    "/system/notifications", response_model=list[NotificationRow], summary="Notifications"
)
async def list_notifications(
    user: CurrentUserDep,
    engine: EngineDep,
    unread_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
) -> list[NotificationRow]:
    rows = await run_in_threadpool(
        _read_notifications, engine, unread_only=unread_only, limit=limit
    )
    return [
        NotificationRow(
            notification_id=str(row["notification_id"]),
            level=str(row["level"]),
            code=str(row["code"]),
            title=str(row["title"]),
            body=row["body"],
            created_at=str(row["created_at"]),
            read_at=row["read_at"],
            dismissed_at=row["dismissed_at"],
        )
        for row in rows
    ]


def _stamp_notification(engine: Any, notification_id: str, column: str) -> int:
    with engine.begin() as connection:
        result = connection.execute(
            text(
                f"UPDATE notification SET {column} = :now"
                f" WHERE notification_id = :id AND {column} IS NULL"
            ),
            {"now": iso_at(utc_now()), "id": notification_id},
        )
        if result.rowcount:
            return int(result.rowcount)
        exists = connection.execute(
            text("SELECT 1 FROM notification WHERE notification_id = :id"),
            {"id": notification_id},
        ).first()
    return 0 if exists is None else -1


async def _mark_notification(engine: Any, notification_id: str, column: str) -> Response:
    outcome = await run_in_threadpool(_stamp_notification, engine, notification_id, column)
    if outcome == 0:
        raise not_found("No notification with that id.")
    # An already-stamped row answers 204 as well. Marking read twice is the same end state, and
    # a second click from a second tab is not an error.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/system/notifications/{notification_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark a notification read",
)
async def mark_notification_read(
    notification_id: str, user: CurrentUserDep, engine: EngineDep
) -> Response:
    return await _mark_notification(engine, notification_id, "read_at")


@router.post(
    "/system/notifications/{notification_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Dismiss a notification",
)
async def dismiss_notification(
    notification_id: str, user: CurrentUserDep, engine: EngineDep
) -> Response:
    return await _mark_notification(engine, notification_id, "dismissed_at")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get(
    "/system/settings", response_model=list[SettingDescriptor], summary="Typed settings"
)
async def read_settings(user: CurrentUserDep, settings: SettingsDep) -> list[SettingDescriptor]:
    return describe_settings(settings)


@router.patch(
    "/system/settings", response_model=list[SettingDescriptor], summary="Change settings"
)
async def patch_settings(
    body: dict[str, Any],
    user: CurrentUserDep,
    settings: SettingsDep,
) -> list[SettingDescriptor]:
    """A partial object. Every key is validated before any key is written.

    `set_many` is all or nothing on purpose: a PATCH that wrote three of five keys and then
    refused the fourth would leave the store in a state the user never asked for and cannot see.
    """
    if not body:
        return describe_settings(settings)
    try:
        await run_in_threadpool(settings.set_many, body)
    except SettingsError as exc:
        # `SettingsError.code` is already the documented API code, unknown_setting or
        # setting_out_of_range, so the route neither renames it nor invents a third.
        raise ApiError(400, exc.code, str(exc)) from exc
    return describe_settings(settings)


# ---------------------------------------------------------------------------
# The request log
# ---------------------------------------------------------------------------

_REQUEST_COLUMNS = (
    "task_id, job_id, kind, state, http_status, latency_ms, fyers_s, fyers_code,"
    " fyers_symbol, request_params_json, created_at, started_at, finished_at"
)


def _read_requests(
    engine: Any,
    *,
    since: str | None,
    endpoint: str | None,
    outcome: str | None,
    limit: int,
) -> list[Any]:
    sql = f"SELECT {_REQUEST_COLUMNS} FROM task WHERE 1 = 1"
    params: dict[str, Any] = {"limit": limit}
    if since:
        sql += " AND created_at >= :since"
        params["since"] = since
    if endpoint:
        sql += " AND kind = :kind"
        params["kind"] = endpoint
    if outcome:
        sql += " AND state = :state"
        params["state"] = outcome
    sql += " ORDER BY created_at DESC, task_id DESC LIMIT :limit"
    with engine.connect() as connection:
        return connection.execute(text(sql), params).mappings().all()


def _scrubbed_params(raw: Any) -> dict[str, Any] | None:
    """The verbatim query parameters, with anything that looks like a secret removed.

    `request_params_json` is written by the worker and is documented never to carry a token, so
    this is the second fence rather than the first. It costs one pass over a small dict and it is
    the difference between a diagnostics tab and an accident.
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    # The whole mapping, so that redaction is applied by key name as well as by pattern. Passing
    # each value on its own loses the key, and a value under `token` is then only caught if it
    # happens to look like one.
    redacted = redact_value(parsed)
    return redacted if isinstance(redacted, dict) else None


@router.get("/system/requests", response_model=list[RequestLogRow], summary="Request log")
async def read_requests(
    user: CurrentUserDep,
    engine: EngineDep,
    since: str | None = Query(None),
    endpoint: str | None = Query(None),
    outcome: str | None = Query(None),
    limit: int = Query(DEFAULT_REQUEST_LOG_LIMIT, ge=1, le=MAX_REQUEST_LOG_LIMIT),
) -> list[RequestLogRow]:
    """`endpoint` filters on the task kind, which is the request family, and `outcome` on the
    task state. Both are the vocabularies the rows are stored in, so a filter cannot silently
    match nothing because of a translation."""
    rows = await run_in_threadpool(
        _read_requests,
        engine,
        since=since,
        endpoint=endpoint,
        outcome=outcome,
        limit=limit,
    )
    return [
        RequestLogRow(
            task_id=str(row["task_id"]),
            job_id=row["job_id"],
            endpoint=task_endpoint(row["kind"]),
            outcome=str(row["state"]),
            http_status=row["http_status"],
            latency_ms=row["latency_ms"],
            requested_at=str(row["started_at"] or row["created_at"]),
            fyers_symbol=row["fyers_symbol"],
            error_code=None if row["fyers_code"] is None else str(row["fyers_code"]),
            params=_scrubbed_params(row["request_params_json"]),
        )
        for row in rows
    ]
