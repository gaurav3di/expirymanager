"""The job list, the job detail, the task drill down and the four lifecycle commands.

API.md section 6, the half that steers a job that already exists. The half that creates one is in
`downloads.py`, which imports the service dependency and the error translation from here.

**Nothing in this module reimplements the engine.** `JobService` already owns create, pause,
resume, cancel and retry-failed, including every refusal and the transaction boundaries, and it is
used by the scheduler as well as by these routes. A second copy of "can this job be paused" living
in a route handler would be a second answer, and the two would diverge the first time a status was
added. So a route here parses the query, calls one service method, turns a `PipelineRequestError`
into the documented envelope, and serialises the result.

**Progress is recomputed, never accumulated.** Every count returned by these routes comes from a
fresh `GROUP BY state` over the `task` table, not from the counters denormalised onto the job row,
because the counters are a cache the supervisor refreshes and a page served straight after a
restart would otherwise show stale numbers. `eta_seconds` is produced by `progress.snapshot_of`,
the same function that fills the `job_progress` SSE frame, so the REST body a screen loads with
and the stream that patches it afterwards are the same arithmetic rather than two that agree
today.

**A parked job is reported as parked.** The 03:00 IST logout moves every running job to
`blocked_auth`. That is not a failure, it needs no retry, and it clears itself when the user logs
in again, so it is returned with `state_group` `blocked`, `needs_reauth` true and `is_failed`
false. A screen that offered Retry there would be asking the user to duplicate work that is about
to resume on its own.

**Reading a job never needs a broker token.** Only planning and committing spend Fyers requests.
Requiring authentication to look at the job list would blank the one screen that explains why
everything stopped at exactly the moment it stopped.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Mapping, Sequence

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import Engine, text
from starlette.concurrency import run_in_threadpool

from expirymanager.api.deps import (
    CurrentUserDep,
    EngineDep,
    ReaderDep,
    SettingsDep,
    StateDep,
)
from expirymanager.api.errors import ApiError, CODE_INVALID_CURSOR
from expirymanager.api.schemas.common import CursorError, decode_cursor, encode_cursor
from expirymanager.api.schemas.downloads import JobActionResult, RetryFailedAccepted
from expirymanager.api.schemas.jobs import (
    JobDetail,
    JobPage,
    JobSummary,
    TaskPage,
    TaskRow,
    block_code,
    scrub_request_params,
    state_group,
)
from expirymanager.pipeline.jobs import (
    ACTIVE_JOB_STATUSES,
    RESUMABLE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    JobService,
    build_job_service,
)
from expirymanager.pipeline.planner import PipelineRequestError
from expirymanager.pipeline.progress import snapshot_of
from expirymanager.pipeline.queue import (
    TASK_CANCELLED,
    TASK_DONE,
    TASK_EMPTY,
    TASK_FAILED,
    TASK_SKIPPED,
    TASK_STATES,
    JobAggregate,
)

__all__ = [
    "router",
    "DEFAULT_JOB_PAGE",
    "DEFAULT_TASK_PAGE",
    "MAX_JOB_PAGE",
    "MAX_TASK_PAGE",
    "JOB_CURSOR_KEYS",
    "TASK_CURSOR_KEYS",
    "get_job_service",
    "JobServiceDep",
    "api_error",
    "effective_per_minute",
    "job_summary",
    "task_row",
]

log = logging.getLogger(__name__)

router = APIRouter()

# API.md section 6: jobs default 25, tasks default 100.
DEFAULT_JOB_PAGE = 25
MAX_JOB_PAGE = 200
DEFAULT_TASK_PAGE = 100
MAX_TASK_PAGE = 500

# The key set each cursor carries. A cursor minted by the job list cannot be fed to the task
# list: `decode_cursor` compares the key set rather than trusting the caller to pick the right one.
JOB_CURSOR_KEYS = ("created_at",)
TASK_CURSOR_KEYS = ("task_id",)

# The settled states, used for the observed throughput. A task that is still pending or leased has
# not cost the job any wall clock yet.
_SETTLED_STATES = (TASK_DONE, TASK_EMPTY, TASK_FAILED, TASK_SKIPPED, TASK_CANCELLED)

_TASK_COLUMNS = (
    "task_id, job_id, seq, kind, state, priority, underlying_id, contract_id, fyers_symbol,"
    " expiry_date, resolution, range_from, range_to, include_oi, request_params_json,"
    " parent_task_id, attempt, max_attempts, not_before, http_status, fyers_s, fyers_code,"
    " last_error_text, latency_ms, response_bytes, row_count, first_ts, last_ts, raw_body_path,"
    " started_at, finished_at, created_at"
)

_COUNTS_FOR_JOBS = (
    "SELECT job_id, state, count(*) AS n FROM task WHERE job_id IN ({placeholders})"
    " GROUP BY job_id, state"
)


# ---------------------------------------------------------------------------
# Shared plumbing, also used by downloads.py
# ---------------------------------------------------------------------------


def get_job_service(state: StateDep, engine: EngineDep, reader: ReaderDep) -> JobService:
    """The one job service, rebuilt per request because it holds no connection of its own.

    `engine` and `reader` are asked for by name rather than reached for through the state object.
    `build_job_service` needs both (its planner reads the registry from SQLite and coverage from
    DuckDB) and raises a bare RuntimeError when one is missing. Depending on them here turns a
    half-started server into the documented 503 from `api/deps.py` instead of a 500.
    """
    return build_job_service(state)


JobServiceDep = Annotated[JobService, Depends(get_job_service)]


def api_error(exc: PipelineRequestError) -> ApiError:
    """Turn any refusal from the pipeline layer into the documented error envelope.

    The pipeline deliberately does not import the API error helpers, so this is the single place
    the two layers meet. Every code and status the planner and the job service raise already match
    API.md, so the translation is a copy and never a remapping that could soften a 409 into a 200.
    """
    return ApiError(exc.status_code, exc.code, exc.message, detail=exc.detail)


def effective_per_minute(settings: Any) -> float:
    """The rate the pipeline actually targets, which is what an ETA has to be built on.

    Eight a second and 170 a minute against a published ten and 200: an estimate built on the
    published ceiling is an estimate the pipeline will never meet, and a progress bar that always
    runs late reads as a broken progress bar.
    """
    return float(settings.get_int("throttle_per_minute"))


def _invalid_cursor(exc: CursorError) -> ApiError:
    return ApiError(400, CODE_INVALID_CURSOR, str(exc))


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _params(raw: Any) -> dict[str, Any] | None:
    """The stored plan sheet, parsed and scrubbed. Unparseable JSON is reported as absent."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    scrubbed = scrub_request_params(parsed)
    return scrubbed if isinstance(scrubbed, dict) else None


def _throughput(row: Mapping[str, Any], counts: Mapping[str, int], now: datetime) -> float | None:
    """Observed tasks per minute: settled work over the wall clock the job has actually had.

    None until the job has started and settled something, because a made up zero on the first
    render is indistinguishable from a job that is genuinely stuck.
    """
    started = _parse_ts(row.get("started_at"))
    if started is None:
        return None
    settled = sum(int(counts.get(state, 0)) for state in _SETTLED_STATES)
    if settled <= 0:
        return None
    finished = _parse_ts(row.get("finished_at"))
    elapsed = ((finished or now) - started).total_seconds()
    if elapsed < 1.0:
        elapsed = 1.0
    return round(settled / (elapsed / 60.0), 2)


def job_summary(
    row: Mapping[str, Any],
    counts: Mapping[str, int],
    *,
    effective_rpm: float,
    now: datetime | None = None,
) -> JobSummary:
    """One job row plus its live task counts, read the way the UI needs to read it."""
    moment = now or datetime.now(UTC)
    job_status = str(row["status"])
    aggregate = JobAggregate(
        job_id=str(row["job_id"]),
        status=job_status,
        cancel_requested=bool(row.get("cancel_requested", 0)),
        counts={state: int(counts.get(state, 0)) for state in TASK_STATES},
        requests_used=int(row.get("requests_used", 0) or 0),
        rows_written=int(row.get("rows_written", 0) or 0),
        bytes_downloaded=int(row.get("bytes_downloaded", 0) or 0),
    )
    # The same call the progress aggregator makes for the SSE frame, so the two cannot drift.
    snapshot = snapshot_of(aggregate, effective_rpm=effective_rpm)
    failed = aggregate.failed
    return JobSummary(
        job_id=aggregate.job_id,
        kind=str(row["kind"]),
        status=job_status,
        created_at=_as_text(row.get("created_at")),
        started_at=_as_text(row.get("started_at")),
        finished_at=_as_text(row.get("finished_at")),
        total_tasks=int(row.get("total_tasks", 0) or 0),
        done_tasks=aggregate.done,
        empty_tasks=aggregate.empty,
        failed_tasks=failed,
        skipped_tasks=aggregate.skipped,
        cancelled_tasks=aggregate.cancelled,
        pending_tasks=aggregate.pending,
        leased_tasks=aggregate.leased,
        open_tasks=aggregate.open,
        est_requests=int(row.get("est_requests", 0) or 0),
        requests_used=aggregate.requests_used,
        rows_written=aggregate.rows_written,
        bytes_downloaded=aggregate.bytes_downloaded,
        throughput_per_minute=_throughput(row, aggregate.counts, moment),
        eta_seconds=snapshot["eta_seconds"],
        reason=_as_text(row.get("block_reason")) or _as_text(row.get("error_text")),
        parent_job_id=_as_text(row.get("parent_job_id")),
        schedule_id=_as_text(row.get("schedule_id")),
        priority=int(row.get("priority", 100) or 100),
        created_by=_as_text(row.get("created_by")),
        params=_params(row.get("params_json")),
        state_group=state_group(job_status),
        needs_reauth=job_status == "blocked_auth",
        blocked_reason=block_code(job_status),
        cancel_requested=aggregate.cancel_requested,
        is_failed=job_status == "failed",
        has_failures=failed > 0,
        can_pause=job_status in ACTIVE_JOB_STATUSES,
        can_resume=job_status in RESUMABLE_JOB_STATUSES,
        can_cancel=job_status not in TERMINAL_JOB_STATUSES,
        can_retry_failed=failed > 0,
    )


def task_row(row: Mapping[str, Any]) -> TaskRow:
    """One task row, with the two things the browser must never receive taken out.

    `raw_body_path` becomes a boolean, because a path the browser knows is a path the browser can
    ask a future route to read. `request_params_json` is scrubbed, because it is stored verbatim
    and verbatim query parameters are one refactor away from carrying a token.
    """
    fyers_code = row.get("fyers_code")
    error_code = None
    if fyers_code is not None:
        error_code = str(fyers_code)
    elif row.get("fyers_s"):
        error_code = str(row["fyers_s"])
    finished = _as_text(row.get("finished_at"))
    started = _as_text(row.get("started_at"))
    return TaskRow(
        task_id=int(row["task_id"]),
        job_id=str(row["job_id"]),
        seq=int(row.get("seq", 0) or 0),
        kind=str(row["kind"]),
        state=str(row["state"]),
        priority=int(row.get("priority", 100) or 100),
        underlying_id=_as_int(row.get("underlying_id")),
        contract_id=_as_int(row.get("contract_id")),
        fyers_symbol=_as_text(row.get("fyers_symbol")),
        expiry_date=_as_text(row.get("expiry_date")),
        resolution=_as_text(row.get("resolution")),
        range_from=_as_text(row.get("range_from")),
        range_to=_as_text(row.get("range_to")),
        include_oi=bool(row.get("include_oi", 1)),
        attempt=int(row.get("attempt", 0) or 0),
        max_attempts=int(row.get("max_attempts", 0) or 0),
        not_before=_as_text(row.get("not_before")),
        parent_task_id=_as_int(row.get("parent_task_id")),
        http_status=_as_int(row.get("http_status")),
        error_code=error_code,
        error_message=_scrub_text(row.get("last_error_text")),
        latency_ms=_as_int(row.get("latency_ms")),
        response_bytes=_as_int(row.get("response_bytes")),
        row_count=_as_int(row.get("row_count")),
        first_ts=_as_text(row.get("first_ts")),
        last_ts=_as_text(row.get("last_ts")),
        has_raw_body=bool(row.get("raw_body_path")),
        request_params_json=scrub_request_params(_maybe_json(row.get("request_params_json"))),
        started_at=started,
        finished_at=finished,
        created_at=_as_text(row.get("created_at")),
        updated_at=finished or started or _as_text(row.get("created_at")),
    )


def _as_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _as_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _scrub_text(value: Any) -> str | None:
    if value is None:
        return None
    scrubbed = scrub_request_params(str(value))
    return None if scrubbed is None else str(scrubbed)


def _maybe_json(raw: Any) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        # Stored as something other than JSON. Returned as the string it is rather than dropped,
        # because a malformed params blob is exactly what a person debugging a task wants to see.
        return str(raw)


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------


def _counts_for(engine: Engine, job_ids: Sequence[str]) -> dict[str, dict[str, int]]:
    """One grouped scan over `task` for a whole page of jobs.

    `idx_task_job` is `(job_id, state)`, so this is an index only aggregate and it stays one round
    trip whatever the page size. The alternative, reading the denormalised counters on the job
    row, serves stale numbers straight after a restart and after any settle the supervisor has not
    flushed yet.
    """
    if not job_ids:
        return {}
    placeholders = ", ".join(f":j{index}" for index in range(len(job_ids)))
    params = {f"j{index}": job_id for index, job_id in enumerate(job_ids)}
    counts: dict[str, dict[str, int]] = {job_id: {} for job_id in job_ids}
    with engine.connect() as connection:
        for row in connection.execute(
            text(_COUNTS_FOR_JOBS.format(placeholders=placeholders)), params
        ).mappings():
            counts[str(row["job_id"])][str(row["state"])] = int(row["n"])
    return counts


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/jobs", response_model=JobPage)
async def list_jobs(
    user: CurrentUserDep,
    engine: EngineDep,
    settings: SettingsDep,
    service: JobServiceDep,
    job_status: Annotated[str | None, Query(alias="status")] = None,
    kind: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_JOB_PAGE)] = DEFAULT_JOB_PAGE,
) -> JobPage:
    """Newest first, keyset paged on `created_at` so a running job cannot shift a page."""
    after = None
    if cursor:
        try:
            after = str(decode_cursor(cursor, expect=JOB_CURSOR_KEYS)["created_at"])
        except CursorError as exc:
            raise _invalid_cursor(exc) from exc

    rows, next_created_at = await run_in_threadpool(
        lambda: service.list_jobs(
            status=job_status, kind=kind, since=since, cursor=after, limit=limit
        )
    )
    job_ids = [str(row["job_id"]) for row in rows]
    counts = await run_in_threadpool(_counts_for, engine, job_ids)
    rpm = effective_per_minute(settings)
    now = datetime.now(UTC)
    return JobPage(
        items=[
            job_summary(row, counts.get(str(row["job_id"]), {}), effective_rpm=rpm, now=now)
            for row in rows
        ],
        next_cursor=(
            encode_cursor({"created_at": next_created_at}) if next_created_at else None
        ),
    )


@router.get("/jobs/{job_id}", response_model=JobDetail)
async def get_job(
    job_id: str,
    user: CurrentUserDep,
    settings: SettingsDep,
    service: JobServiceDep,
) -> JobDetail:
    """The job row, a fresh aggregate over its own tasks, and its family."""
    try:
        record = await run_in_threadpool(service.get, job_id)
    except PipelineRequestError as exc:
        raise api_error(exc) from exc

    counts: Mapping[str, int] = record["counts"]
    summary = job_summary(record, counts, effective_rpm=effective_per_minute(settings))
    return JobDetail(
        **summary.model_dump(),
        task_states={state: int(counts.get(state, 0)) for state in TASK_STATES},
        child_job_ids=list(record["children"]),
        error_text=_scrub_text(record.get("error_text")),
    )


@router.get("/jobs/{job_id}/tasks", response_model=TaskPage)
async def list_job_tasks(
    job_id: str,
    user: CurrentUserDep,
    engine: EngineDep,
    service: JobServiceDep,
    state: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    contract_id: Annotated[int | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_TASK_PAGE)] = DEFAULT_TASK_PAGE,
) -> TaskPage:
    """The task breakdown, which is how a failure is read down to the request that caused it."""
    # Existence is checked through the service so a missing job is the same 404 here as it is on
    # the detail route, rather than an empty page that reads as a job with no tasks.
    try:
        await run_in_threadpool(service.get, job_id)
    except PipelineRequestError as exc:
        raise api_error(exc) from exc

    if state is not None and state not in TASK_STATES:
        raise ApiError(
            422,
            "invalid_task_state",
            f"{state!r} is not a task state.",
            detail={"known": list(TASK_STATES)},
        )

    after = None
    if cursor:
        try:
            after = int(decode_cursor(cursor, expect=TASK_CURSOR_KEYS)["task_id"])
        except (CursorError, TypeError, ValueError) as exc:
            raise _invalid_cursor(CursorError("the cursor is not a task cursor")) from exc

    where = ["job_id = :job_id"]
    params: dict[str, Any] = {"job_id": job_id, "limit": limit + 1}
    if state is not None:
        where.append("state = :state")
        params["state"] = state
    if kind is not None:
        where.append("kind = :kind")
        params["kind"] = kind
    if contract_id is not None:
        where.append("contract_id = :contract_id")
        params["contract_id"] = contract_id
    if after is not None:
        where.append("task_id > :after")
        params["after"] = after

    sql = (
        f"SELECT {_TASK_COLUMNS} FROM task WHERE "
        + " AND ".join(where)
        + " ORDER BY task_id LIMIT :limit"
    )

    def read() -> list[dict[str, Any]]:
        with engine.connect() as connection:
            return [dict(item) for item in connection.execute(text(sql), params).mappings()]

    rows = await run_in_threadpool(read)
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor({"task_id": int(rows[-1]["task_id"])})
    return TaskPage(items=[task_row(row) for row in rows], next_cursor=next_cursor)


@router.post("/jobs/{job_id}/pause", response_model=JobActionResult)
async def pause_job(
    job_id: str, user: CurrentUserDep, service: JobServiceDep
) -> JobActionResult:
    """Stop leasing. Not one task row is rewritten, which is what makes resume free."""
    try:
        return await run_in_threadpool(service.pause, job_id)
    except PipelineRequestError as exc:
        raise api_error(exc) from exc


@router.post("/jobs/{job_id}/resume", response_model=JobActionResult)
async def resume_job(
    job_id: str, user: CurrentUserDep, service: JobServiceDep
) -> JobActionResult:
    """Back to queued, including from `blocked_auth` once the broker is connected again."""
    try:
        return await run_in_threadpool(service.resume, job_id)
    except PipelineRequestError as exc:
        raise api_error(exc) from exc


@router.post("/jobs/{job_id}/cancel", response_model=JobActionResult)
async def cancel_job(
    job_id: str, user: CurrentUserDep, service: JobServiceDep
) -> JobActionResult:
    """Cooperative: pending tasks are cancelled and in-flight ones finish and write their data.

    That is what leaves a cancelled job with valid partial data and a coverage ledger that still
    describes exactly what was and was not fetched.
    """
    try:
        return await service.cancel(job_id)
    except PipelineRequestError as exc:
        raise api_error(exc) from exc


@router.post(
    "/jobs/{job_id}/retry-failed",
    response_model=RetryFailedAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_failed_tasks(
    job_id: str, user: CurrentUserDep, service: JobServiceDep
) -> RetryFailedAccepted:
    """A child job holding copies of exactly the failed tasks. The parent is never rewritten."""
    try:
        return await run_in_threadpool(service.retry_failed, job_id)
    except PipelineRequestError as exc:
        raise api_error(exc) from exc
