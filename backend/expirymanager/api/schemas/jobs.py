"""Response models for the job list, the job detail, the task drill down and the coverage grid.

The plan and commit models are not here. They live in `api/schemas/downloads.py`, written by the
item that built the planner, and this module deliberately does not restate them: the number the
user is shown and the number the commit gate compares against have to be the same field of the
same class or the gate is decorative.

Three ideas shape everything below.

**A status is reported, never re-derived.** The pipeline owns the eleven job statuses in
PIPELINE.md section 9.1 and this module copies the string out. What it adds is the reading the UI
needs on top of that string, computed once here rather than in four screens: which group the
status belongs to, whether the job is waiting for a human to log in again, and which of pause,
resume, cancel and retry are legal right now.

**Awaiting authentication is not failure.** The 03:00 IST logout parks every running job in
`blocked_auth`, and those jobs resume by themselves after the next login. A UI that renders that
as a failure asks the user to retry work that is not broken, and a UI that renders it as merely
paused hides the one action that actually unblocks it. `blocked` plus `needs_reauth` says exactly
what happened and exactly what to do, and `failed` is reserved for a job that genuinely cannot
continue.

**A task row is scrubbed on the way out.** `request_params_json` is stored verbatim and a verbatim
query string is one refactor away from carrying a token, so it is passed through `redact_value`
before it is serialised. `raw_body_path` never crosses at all: the browser gets the boolean
`has_raw_body` and no path it could ask the server to read.

The coverage models live here too rather than in a file of their own, because API.md section 6
covers downloads, jobs and coverage as one surface and this item owns one schema module for it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import Field

from expirymanager.api.schemas.common import ApiModel
from expirymanager.security.redaction import redact_value

__all__ = [
    "GROUP_DRAFT",
    "GROUP_ACTIVE",
    "GROUP_PAUSED",
    "GROUP_BLOCKED",
    "GROUP_DEFERRED",
    "GROUP_TERMINAL",
    "BLOCK_AUTHENTICATION",
    "BLOCK_RATE_LIMIT",
    "BLOCK_BUDGET",
    "STATE_GROUPS",
    "state_group",
    "block_code",
    "JobSummary",
    "JobDetail",
    "TaskRow",
    "JobPage",
    "TaskPage",
    "CoverageResolution",
    "CoverageCell",
    "CoverageGridResponse",
    "CoverageGapRow",
    "CoverageGapsResponse",
    "scrub_request_params",
]

GROUP_DRAFT = "draft"
GROUP_ACTIVE = "active"
GROUP_PAUSED = "paused"
GROUP_BLOCKED = "blocked"
GROUP_DEFERRED = "deferred"
GROUP_TERMINAL = "terminal"

# Why a job is not moving, as a code a screen can switch on. `blocked_auth` is the one the 03:00
# logout produces and the only one whose remedy is a login rather than a decision.
BLOCK_AUTHENTICATION = "authentication"
BLOCK_RATE_LIMIT = "rate_limit"
BLOCK_BUDGET = "budget"

# Every status in PIPELINE.md section 9.1, mapped to the group the UI groups by. Written out in
# full rather than matched by prefix, so that a status added to the pipeline shows up here as a
# KeyError in a test rather than as a silent default that renders a blocked job as active.
STATE_GROUPS: dict[str, str] = {
    "draft": GROUP_DRAFT,
    "queued": GROUP_ACTIVE,
    "running": GROUP_ACTIVE,
    "paused": GROUP_PAUSED,
    "blocked_auth": GROUP_BLOCKED,
    "blocked_rate": GROUP_BLOCKED,
    "deferred_budget": GROUP_DEFERRED,
    "completed": GROUP_TERMINAL,
    "completed_with_errors": GROUP_TERMINAL,
    "cancelled": GROUP_TERMINAL,
    "failed": GROUP_TERMINAL,
}

_BLOCK_CODES: dict[str, str] = {
    "blocked_auth": BLOCK_AUTHENTICATION,
    "blocked_rate": BLOCK_RATE_LIMIT,
    "deferred_budget": BLOCK_BUDGET,
}


def state_group(status: str) -> str:
    """The group a status belongs to. An unknown status reads as blocked, never as active.

    An unrecognised status means this module is behind the pipeline. Rendering it as active would
    put a spinner on a job nothing is working on; rendering it as blocked shows the user that
    something needs attention, which is the truthful reading of "this server does not know".
    """
    return STATE_GROUPS.get(status, GROUP_BLOCKED)


def block_code(status: str) -> str | None:
    """The remedy code for a job that is held, or None for one that is not held."""
    return _BLOCK_CODES.get(status)


def scrub_request_params(value: Any) -> Any:
    """Redact a stored `request_params_json` before it is returned to the browser."""
    if value is None:
        return None
    return redact_value(value)


class JobSummary(ApiModel):
    """One row of `GET /api/v1/jobs`.

    `throughput_per_minute` is observed, not planned: settled tasks divided by the wall clock
    minutes the job has actually been running. `eta_seconds` is the pipeline's own estimate,
    produced by the same function that fills the `job_progress` SSE frame, so an open job detail
    screen and the stream patching it can never disagree.
    """

    job_id: str
    kind: str
    status: str
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    total_tasks: int = 0
    done_tasks: int = 0
    empty_tasks: int = 0
    failed_tasks: int = 0
    skipped_tasks: int = 0
    cancelled_tasks: int = 0
    pending_tasks: int = 0
    leased_tasks: int = 0
    open_tasks: int = 0

    est_requests: int = 0
    requests_used: int = 0
    rows_written: int = 0
    bytes_downloaded: int = 0

    throughput_per_minute: float | None = None
    eta_seconds: int | None = None

    reason: str | None = None
    parent_job_id: str | None = None
    schedule_id: str | None = None
    priority: int = 100
    created_by: str | None = None
    params: dict[str, Any] | None = None

    # The honest reading of `status`, so no screen has to keep its own table of what a status
    # means or which button to offer.
    state_group: str = GROUP_ACTIVE
    needs_reauth: bool = False
    blocked_reason: str | None = None
    cancel_requested: bool = False
    is_failed: bool = False
    has_failures: bool = False
    can_pause: bool = False
    can_resume: bool = False
    can_cancel: bool = False
    can_retry_failed: bool = False


class JobDetail(JobSummary):
    """`GET /api/v1/jobs/{job_id}`: the row, the live aggregate, the family and the schedule."""

    task_states: dict[str, int] = Field(default_factory=dict)
    child_job_ids: list[str] = Field(default_factory=list)
    error_text: str | None = None


class TaskRow(ApiModel):
    """One row of `GET /api/v1/jobs/{job_id}/tasks`."""

    task_id: int
    job_id: str
    seq: int = 0
    kind: str
    state: str
    priority: int = 100

    underlying_id: int | None = None
    contract_id: int | None = None
    fyers_symbol: str | None = None
    expiry_date: str | None = None
    resolution: str | None = None
    range_from: str | None = None
    range_to: str | None = None
    include_oi: bool = True

    attempt: int = 0
    max_attempts: int = 0
    not_before: str | None = None
    parent_task_id: int | None = None

    http_status: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: int | None = None
    response_bytes: int | None = None
    row_count: int | None = None
    first_ts: str | None = None
    last_ts: str | None = None

    # A path the browser could ask for is a path the browser could ask for. Only its existence
    # crosses.
    has_raw_body: bool = False
    request_params_json: Any | None = None

    started_at: str | None = None
    finished_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class JobPage(ApiModel):
    """A page of jobs. Keyset paged on `created_at`, newest first."""

    items: list[JobSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class TaskPage(ApiModel):
    """A page of tasks. Keyset paged on `task_id`, oldest first, which is dispatch order."""

    items: list[TaskRow] = Field(default_factory=list)
    next_cursor: str | None = None


class CoverageResolution(ApiModel):
    """One column header of the heatmap."""

    res_id: int
    fyers_code: str
    label: str
    seconds: int


class CoverageCell(ApiModel):
    """One expiry by resolution cell, read entirely from `candle_coverage`.

    `contracts_total` comes from the expiry row rather than from a count of covered contracts, so
    a cell that holds nothing still reports how much is missing instead of reading as complete.
    """

    expiry_date: date
    res_id: int
    contracts_total: int = 0
    contracts_with_data: int = 0
    contracts_missing: int = 0
    chunks_ok: int = 0
    chunks_empty: int = 0
    chunks_error: int = 0
    rows: int = 0
    have_from: date | None = None
    have_to: date | None = None


class CoverageGridResponse(ApiModel):
    """`GET /api/v1/coverage/grid`."""

    underlying_id: int
    resolutions: list[CoverageResolution] = Field(default_factory=list)
    cells: list[CoverageCell] = Field(default_factory=list)


class CoverageGapRow(ApiModel):
    """One hole between two recorded chunks of the same contract at the same resolution.

    Both the held edges and the missing window are returned. `gap_from` and `gap_to` are the
    inclusive days that are not held, which is what a repair download would ask for, while
    `held_to` and `held_from` are the chunk edges the view actually reports.
    """

    contract_id: int
    fyers_symbol: str | None = None
    expiry_date: date | None = None
    res_id: int
    held_to: date
    held_from: date
    gap_from: date
    gap_to: date
    missing_days: int


class CoverageGapsResponse(ApiModel):
    """`GET /api/v1/coverage/gaps`."""

    items: list[CoverageGapRow] = Field(default_factory=list)
