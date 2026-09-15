"""API.md section 8: build an export, list what has been built, and serve the finished file.

There is exactly one export code path in this application and it is `db/exports.run_export`,
driven by `pipeline/handlers/export.run_export_job`. This module writes the ledger rows, guards
the free space, and hands the work to that runner. It does not build a COPY statement of its own,
because a second export path is a second set of conventions for the timestamp columns and a second
place for the atomic rename to be forgotten.

The work runs as a Starlette background task rather than as a queued pipeline task, and the reason
is in the schema rather than in preference: `task.kind` admits six kinds and every one of them is
one outbound Fyers request, which is what makes a task row simultaneously the queue entry, the
retry record and the request provenance record. An export makes no request, spends no budget and
has nothing to retry. It still gets a `job` row, because `job.kind` does admit `export` and the
jobs screen is where a user looks for anything long running.

The file route re-derives the path from the row and asserts it resolves inside the exports
directory. A stored path is data, and data that becomes a filesystem path is checked at the point
of use, not at the point it was written.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Query, Response, status
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse

from expirymanager.api.deps import (
    CurrentUserDep,
    EngineDep,
    ReaderDep,
    StateDep,
)
from expirymanager.api.errors import ApiError, CODE_SERVICE_UNAVAILABLE, not_found
from expirymanager.api.schemas.common import Page, decode_cursor, encode_cursor
from expirymanager.api.schemas.exports import (
    DEFAULT_EXPORT_PAGE_SIZE,
    MAX_EXPORT_PAGE_SIZE,
    ExportAccepted,
    ExportCreateRequest,
    ExportRow,
)
from expirymanager.db.exports import ExportSpec, ExportSpecError, estimate_bytes, estimate_rows
from expirymanager.pipeline.handlers.export import (
    create_export_row,
    run_export_job,
    spec_from_params,
)
from expirymanager.pipeline.queue import iso_at, utc_now

__all__ = [
    "router",
    "CODE_INVALID_EXPORT",
    "CODE_INSUFFICIENT_DISK",
    "CODE_NOT_READY",
    "CODE_FILE_MISSING",
    "CODE_UNKNOWN_RESOLUTION",
    "CODE_DIRECTORY_EXPORT",
    "resolve_res_ids",
]

log = logging.getLogger(__name__)

router = APIRouter()

CODE_INVALID_EXPORT = "invalid_export"
CODE_INSUFFICIENT_DISK = "insufficient_disk"
CODE_NOT_READY = "not_ready"
CODE_FILE_MISSING = "file_missing"
CODE_UNKNOWN_RESOLUTION = "unknown_resolution"

# A hive archive is a directory of Parquet parts plus its manifest and schema sidecars. There is
# no single file to attach, so the download route says so with its own code rather than reusing
# `not_ready`, which would send the user back to wait for something that has already finished.
CODE_DIRECTORY_EXPORT = "directory_export"

_CURSOR_KEYS = ("created_at", "export_id")

_ROW_COLUMNS = (
    "export_id, job_id, format, layout, compression, scope_json, file_path, row_count,"
    " byte_size, sha256, status, error_text, created_at, finished_at"
)


def _duck_store(state: StateDep) -> Any:
    """The one DuckDB instance. Named explicitly so a missing one is a 503, never a no-op."""
    if state.duck is None:
        raise ApiError(
            503,
            CODE_SERVICE_UNAVAILABLE,
            "The market database is not available. The server did not finish starting.",
        )
    return state.duck


async def resolve_res_ids(reader: Any, codes: list[str]) -> list[int]:
    """Translate Fyers resolution codes into `res_id` values, refusing an unknown one.

    `dim_resolution.res_id` 1 is the five second series and `fyers_code` "1" is the one minute
    series, so accepting a raw integer here would export the wrong resolution without any error.
    An unknown code is a 400 rather than a filter that matches nothing.
    """
    if not codes:
        return []
    rows = await reader.fetch_all("SELECT fyers_code, res_id FROM dim_resolution")
    known = {str(code).strip().upper(): int(res_id) for code, res_id in rows}
    out: list[int] = []
    unknown: list[str] = []
    for code in codes:
        key = str(code).strip().upper()
        if key in known:
            out.append(known[key])
        else:
            unknown.append(str(code))
    if unknown:
        raise ApiError(
            400,
            CODE_UNKNOWN_RESOLUTION,
            "This export names resolutions this build does not know: " + ", ".join(unknown),
            detail={"unknown": unknown, "known": sorted(known)},
        )
    return out


def _create_job_row(engine: Any, *, job_id: str, params: dict[str, Any], created_by: str) -> None:
    """One `job` row of kind export, so the jobs screen shows it like anything else long running.

    No `task` rows: `task.kind` has no export member, and inventing one would make a row that the
    dispatcher would try to lease and no handler could run.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO job (job_id, kind, status, params_json, priority, est_requests,"
                " total_tasks, created_by, created_at)"
                " VALUES (:job_id, 'export', 'queued', :params_json, 100, 0, 1, :created_by,"
                " :created_at)"
            ),
            {
                "job_id": job_id,
                "params_json": json.dumps(params, sort_keys=True, default=str),
                "created_by": created_by,
                "created_at": iso_at(utc_now()),
            },
        )


def _mark_job(engine: Any, job_id: str, status_value: str, **columns: Any) -> None:
    assignments = {"status": status_value, **columns}
    clause = ", ".join(f"{name} = :{name}" for name in assignments)
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE job SET {clause} WHERE job_id = :job_id"),
            {**assignments, "job_id": job_id},
        )


async def _run_and_settle(
    *,
    engine: Any,
    store: Any,
    writer: Any,
    exports_dir: Path,
    spec: ExportSpec,
    export_id: str,
    job_id: str,
    bus: Any,
) -> None:
    """Run the export and leave both ledgers telling the same story.

    `run_export_job` owns the `export_job` row and the `export_ready` frame. This wrapper owns the
    `job` row, so the jobs screen and the exports screen never disagree about whether it finished.
    """
    await run_in_threadpool(
        _mark_job, engine, job_id, "running", started_at=iso_at(utc_now())
    )
    try:
        run = await run_export_job(
            engine=engine,
            store=store,
            writer=writer,
            exports_dir=exports_dir,
            spec=spec,
            export_id=export_id,
            bus=bus,
            job_id=job_id,
        )
    except Exception as exc:  # noqa: BLE001 - the job row must record what went wrong
        log.exception("an export job failed outside the runner", extra={"job_id": job_id})
        await run_in_threadpool(
            _mark_job,
            engine,
            job_id,
            "failed",
            finished_at=iso_at(utc_now()),
            failed_tasks=1,
            error_text=f"{type(exc).__name__}: {exc}"[:2000],
        )
        return

    if run.ok:
        await run_in_threadpool(
            _mark_job,
            engine,
            job_id,
            "completed",
            finished_at=iso_at(utc_now()),
            done_tasks=1,
            rows_written=int(run.row_count or 0),
            bytes_downloaded=int(run.byte_size or 0),
        )
    else:
        await run_in_threadpool(
            _mark_job,
            engine,
            job_id,
            "failed",
            finished_at=iso_at(utc_now()),
            failed_tasks=1,
            error_text=(run.error_text or "the export failed")[:2000],
        )


@router.post(
    "/exports",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ExportAccepted,
    summary="Build an export",
)
async def create_export(
    body: ExportCreateRequest,
    background: BackgroundTasks,
    user: CurrentUserDep,
    state: StateDep,
    engine: EngineDep,
    reader: ReaderDep,
) -> ExportAccepted:
    duck = _duck_store(state)
    res_ids = await resolve_res_ids(reader, body.scope.resolutions)
    params = body.as_params(res_ids)

    try:
        spec = spec_from_params(params)
    except ExportSpecError as exc:
        raise ApiError(400, CODE_INVALID_EXPORT, str(exc)) from exc

    exports_dir = state.paths.exports_dir
    await run_in_threadpool(exports_dir.mkdir, parents=True, exist_ok=True)

    rows = await estimate_rows(reader, spec)
    needed = estimate_bytes(spec, rows)
    free = await run_in_threadpool(lambda: shutil.disk_usage(exports_dir).free)
    if needed > free:
        raise ApiError(
            507,
            CODE_INSUFFICIENT_DISK,
            "This export is estimated to need more free space than the disk has.",
            detail={"needed_bytes": needed, "free_bytes": free, "row_count": rows},
        )

    job_id = str(uuid.uuid4())
    export_id = uuid.uuid4().hex
    await run_in_threadpool(
        _create_job_row,
        engine,
        job_id=job_id,
        params=params,
        created_by=f"user:{user.username}",
    )
    await run_in_threadpool(
        create_export_row,
        engine,
        spec,
        export_id=export_id,
        job_id=job_id,
        params=params,
    )

    supervisor = state.supervisor
    background.add_task(
        _run_and_settle,
        engine=engine,
        store=duck,
        writer=duck.writer,
        exports_dir=exports_dir,
        spec=spec,
        export_id=export_id,
        job_id=job_id,
        # No getattr default. A missing supervisor means no bus and the export still runs; the
        # UI then learns it finished from its own refetch, which is the documented contract.
        bus=None if supervisor is None else supervisor.bus,
    )
    return ExportAccepted(export_id=export_id, job_id=job_id, status="queued")


def _export_row(row: Any) -> ExportRow:
    raw = row["scope_json"] or "{}"
    try:
        scope = json.loads(raw)
    except (TypeError, ValueError):
        scope = {}
    return ExportRow(
        export_id=str(row["export_id"]),
        job_id=row["job_id"],
        status=str(row["status"]),
        format=str(row["format"]),
        layout=str(row["layout"]),
        compression=row["compression"],
        row_count=row["row_count"],
        byte_size=row["byte_size"],
        sha256=row["sha256"],
        created_at=str(row["created_at"]),
        finished_at=row["finished_at"],
        error_message=row["error_text"],
        scope=scope if isinstance(scope, dict) else {},
    )


def _read_page(engine: Any, *, limit: int, after: dict[str, Any] | None) -> list[Any]:
    sql = f"SELECT {_ROW_COLUMNS} FROM export_job"
    params: dict[str, Any] = {"limit": limit + 1}
    if after is not None:
        sql += (
            " WHERE (created_at < :created_at)"
            "    OR (created_at = :created_at AND export_id < :export_id)"
        )
        params["created_at"] = after["created_at"]
        params["export_id"] = after["export_id"]
    sql += " ORDER BY created_at DESC, export_id DESC LIMIT :limit"
    with engine.connect() as connection:
        return connection.execute(text(sql), params).mappings().all()


@router.get("/exports", response_model=Page[ExportRow], summary="List exports")
async def list_exports(
    user: CurrentUserDep,
    engine: EngineDep,
    limit: int = Query(DEFAULT_EXPORT_PAGE_SIZE, ge=1, le=MAX_EXPORT_PAGE_SIZE),
    cursor: str | None = Query(None),
) -> Page[ExportRow]:
    after = decode_cursor(cursor, expect=_CURSOR_KEYS) if cursor else None
    rows = await run_in_threadpool(_read_page, engine, limit=limit, after=after)
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(
            {"created_at": str(last["created_at"]), "export_id": str(last["export_id"])}
        )
    return Page[ExportRow](items=[_export_row(row) for row in rows], next_cursor=next_cursor)


def _read_one(engine: Any, export_id: str) -> Any:
    with engine.connect() as connection:
        return (
            connection.execute(
                text(f"SELECT {_ROW_COLUMNS} FROM export_job WHERE export_id = :id"),
                {"id": export_id},
            )
            .mappings()
            .first()
        )


def resolved_export_path(stored: str, exports_dir: Path) -> Path:
    """Re-derive the path and assert it stays inside the exports directory.

    `Path.resolve()` on both sides, so a stored `../../master.key`, a symlink planted in the
    exports directory, or a row edited by hand all fail the containment check rather than
    becoming a download.
    """
    root = exports_dir.resolve()
    candidate = Path(stored).resolve()
    if candidate != root and root not in candidate.parents:
        raise not_found("That export is no longer available.", code=CODE_FILE_MISSING)
    return candidate


@router.get("/exports/{export_id}/file", summary="Download a finished export")
async def download_export(
    export_id: str,
    user: CurrentUserDep,
    state: StateDep,
    engine: EngineDep,
) -> FileResponse:
    row = await run_in_threadpool(_read_one, engine, export_id)
    if row is None:
        raise not_found("No export with that id.")
    if row["status"] != "ready" or not row["file_path"]:
        raise ApiError(
            409,
            CODE_NOT_READY,
            f"That export is {row['status']}. Only a finished export can be downloaded.",
        )

    path = resolved_export_path(str(row["file_path"]), state.paths.exports_dir)
    exists = await run_in_threadpool(path.exists)
    if not exists:
        raise ApiError(
            410,
            CODE_FILE_MISSING,
            "The export file is no longer on disk. Build the export again.",
        )
    if await run_in_threadpool(path.is_dir):
        raise ApiError(
            409,
            CODE_DIRECTORY_EXPORT,
            "A hive archive is a directory of files rather than one download. Copy it from "
            f"{path} instead.",
            detail={"path": str(path)},
        )

    media_type = "text/csv" if row["format"] == "csv" else "application/vnd.apache.parquet"
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        content_disposition_type="attachment",
        # Set here so the security header middleware leaves it alone. An export file is a byte
        # for byte copy of durable rows, and a browser that re-requests it is not a correctness
        # problem, but nothing under /api is cached by a shared cache either.
        headers={"Cache-Control": "no-store"},
    )


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _mark_deleted(engine: Any, export_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE export_job SET status = 'deleted', file_path = NULL"
                " WHERE export_id = :id"
            ),
            {"id": export_id},
        )


@router.delete(
    "/exports/{export_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an export and its file",
)
async def delete_export(
    export_id: str,
    user: CurrentUserDep,
    state: StateDep,
    engine: EngineDep,
) -> Response:
    row = await run_in_threadpool(_read_one, engine, export_id)
    if row is None:
        raise not_found("No export with that id.")
    if row["file_path"]:
        path = resolved_export_path(str(row["file_path"]), state.paths.exports_dir)
        await run_in_threadpool(_remove_path, path)
        sidecar = path.with_name(path.name + ".schema.json")
        await run_in_threadpool(_remove_path, sidecar)
    await run_in_threadpool(_mark_deleted, engine, export_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
