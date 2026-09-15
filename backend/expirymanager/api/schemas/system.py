"""Request and response models for the system surface, API.md section 10.

The interesting part of this module is `describe_settings`. `GET /api/v1/system/settings` is the
replacement for a .env file, and the Settings screen renders its control from the descriptor
rather than from a hardcoded list: a select when the setting has choices, a number input with
`min` and `max` when it has a range, a switch when it is a boolean. So the descriptor has to carry
the bounds, and `SettingSpec` stores them inside a validator closure rather than as fields.

Reading them back out of the closure is deliberate and it is the lesser of two evils. The
alternative was a second copy of every bound in this file, which is a copy that drifts silently:
the store would keep refusing 40 workers while the screen kept offering it. Reading the real
validator means the screen can only ever offer what the store will accept. The introspection is
narrow (two helper shapes, `low`/`high` and `permitted`), it degrades to "no bounds" rather than
to a wrong bound, and `tests/test_system_routes.py` asserts the actual numbers for a range setting
and the actual choices for an enumerated one, so a change to `settings_store` that breaks it fails
a test rather than quietly flattening every control into a text box.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from expirymanager.api.schemas.common import ApiModel, RequestModel
from expirymanager.settings_store import SETTINGS, SettingSpec

__all__ = [
    "RESTART_REQUIRED",
    "HEALTH_OK",
    "HEALTH_FAILING",
    "DEFAULT_REQUEST_LOG_LIMIT",
    "MAX_REQUEST_LOG_LIMIT",
    "BudgetResponse",
    "StorageResponse",
    "HealthRow",
    "MaintenanceRun",
    "HealthResponse",
    "CheckpointResponse",
    "OptimiseRequest",
    "OptimiseAccepted",
    "BackupRequest",
    "BackupAccepted",
    "NotificationRow",
    "SettingDescriptor",
    "RequestLogRow",
    "setting_bounds",
    "describe_settings",
]

# Settings the running process reads once, at startup, into something it then keeps. Changing one
# of these writes the row and the value is live for the next process, not for this one, and the
# screen has to say so rather than let the user believe the change took effect.
RESTART_REQUIRED: frozenset[str] = frozenset(
    {
        "throttle_per_second",
        "throttle_per_minute",
        "throttle_in_flight",
        "daily_budget",
        "plan_tier",
        "worker_count",
        "cookie_secure",
    }
)

HEALTH_OK = "ok"
HEALTH_FAILING = "failing"

DEFAULT_REQUEST_LOG_LIMIT = 100
MAX_REQUEST_LOG_LIMIT = 1000


class BudgetResponse(ApiModel):
    """`GET /api/v1/system/budget`, and the identical body of the `budget` SSE frame.

    The two are the same object on purpose: the stream writes straight into the cache slot the
    REST query owns, so a difference of one field would make the top bar flicker between two
    shapes.
    """

    ist_date: str
    plan: str
    requests_used: int
    plan_limit_day: int
    remaining: int
    minute_headroom: int
    minute_violations: int
    strikes_remaining: int
    blocked_until: str | None = None
    pipeline_mode: str
    pipeline_reason: str | None = None
    sweep_reserve_fraction: float


class StorageResponse(ApiModel):
    """`GET /api/v1/system/storage`. Every field comes from `db/maintenance.storage_report`."""

    duckdb_bytes: int
    duckdb_wal_bytes: int
    sqlite_bytes: int
    exports_bytes: int
    raw_payload_bytes: int
    candle_rows: int
    bytes_per_row: float
    modelled_bytes: int
    bloat_ratio: float
    compaction_suggested: bool
    free_disk_bytes: int


class HealthRow(ApiModel):
    """One assertion from `v_data_health` plus the id keyspace checks."""

    check_name: str
    status: str
    offending: int = 0
    detail: str | None = None
    observed_at: str | None = None


class MaintenanceRun(ApiModel):
    """The last fire of the maintenance schedule, read from `schedule_run`."""

    ran_at: str
    outcome: str
    detail: str | None = None


class HealthResponse(ApiModel):
    rows: list[HealthRow] = Field(default_factory=list)
    last_maintenance: MaintenanceRun | None = None


class CheckpointResponse(ApiModel):
    wal_bytes_before: int
    wal_bytes_after: int


class OptimiseRequest(RequestModel):
    """`POST /api/v1/system/optimise`. The confirmation is required, not defaulted."""

    confirm: bool = False


class OptimiseAccepted(ApiModel):
    """`202`. See `api/v1/system.py` for why `job_id` names no row in the `job` table."""

    job_id: str
    status: str = "running"
    bytes_before: int = 0


class BackupRequest(RequestModel):
    target_dir: str | None = None


class BackupAccepted(ApiModel):
    job_id: str
    status: str = "running"
    target_dir: str


class NotificationRow(ApiModel):
    """One `notification` row.

    `job_id` is always null and the column does not exist. It is declared because the frontend
    type carries it, and declaring it null is more honest than inventing a value for it.
    """

    notification_id: str
    level: str
    code: str
    title: str
    body: str | None = None
    created_at: str
    read_at: str | None = None
    dismissed_at: str | None = None
    job_id: str | None = None


class SettingDescriptor(ApiModel):
    """One typed setting: its value, its default and everything the control needs."""

    key: str
    value: Any = None
    value_type: Literal["str", "int", "bool", "float", "list"]
    default: Any = None
    description: str
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str] | None = None
    requires_restart: bool = False


class RequestLogRow(ApiModel):
    """One `task` row projected as a request, for the Diagnostics tab."""

    task_id: str
    job_id: str | None = None
    endpoint: str
    outcome: str
    http_status: int | None = None
    latency_ms: int | None = None
    requested_at: str
    fyers_symbol: str | None = None
    error_code: str | None = None
    params: dict[str, Any] | None = None


def setting_bounds(spec: SettingSpec) -> tuple[float | None, float | None, list[str] | None]:
    """`(minimum, maximum, choices)` for one setting, read off its validator.

    Returns all-None for a setting with no validator, and for a validator whose shape this does
    not recognise. That is a control with no bounds rather than a control with the wrong bounds,
    which is the only degradation worth having here.
    """
    validator = spec.validator
    if validator is None:
        return (None, None, None)
    code = getattr(validator, "__code__", None)
    cells = getattr(validator, "__closure__", None)
    if code is None or not cells:
        return (None, None, None)
    captured = {
        name: cell.cell_contents for name, cell in zip(code.co_freevars, cells, strict=False)
    }
    low = captured.get("low")
    high = captured.get("high")
    permitted = captured.get("permitted")
    choices = (
        [str(item) for item in permitted]
        if isinstance(permitted, (list, tuple)) and permitted
        else None
    )
    minimum = float(low) if isinstance(low, (int, float)) and not isinstance(low, bool) else None
    maximum = (
        float(high) if isinstance(high, (int, float)) and not isinstance(high, bool) else None
    )
    return (minimum, maximum, choices)


def describe_settings(store: Any) -> list[SettingDescriptor]:
    """Every setting, current value first, in the order `settings_store` declares them."""
    current = store.all()
    out: list[SettingDescriptor] = []
    for key, spec in SETTINGS.items():
        minimum, maximum, choices = setting_bounds(spec)
        out.append(
            SettingDescriptor(
                key=key,
                value=current.get(key, spec.default),
                value_type=spec.type.__name__,  # type: ignore[arg-type]
                default=spec.default,
                description=spec.description,
                minimum=minimum,
                maximum=maximum,
                choices=choices,
                requires_restart=key in RESTART_REQUIRED,
            )
        )
    return out
