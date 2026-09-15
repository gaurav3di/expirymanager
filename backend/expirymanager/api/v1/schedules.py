"""API.md section 9: list, create, edit, enable, disable, run and audit the schedules.

Every route here is a thin call onto `SchedulerService`, and that is the whole design. The service
owns the `schedule` table, rebuilds every APScheduler trigger from it on each mutation, and runs
one fire path that both the cron trigger and the Run now button go through. A route that wrote the
table itself would leave the running scheduler holding a trigger that no longer matches the row,
which is a schedule that fires at the old time until the next restart.

Enable and disable have no endpoints of their own. They are `PATCH` with `enabled`, because the
scheduler treats them as exactly that: a row change followed by a rebuild. A separate pair of
routes would be a second way to reach the same transition and a second place to forget the sync.

One thing this module does that the service does not: it turns a cron expression Pydantic rejected
into the documented `400 invalid_cron` rather than the generic `422 validation_error`. The
expression is validated by `CronTrigger.from_crontab` inside the model, so the check is the real
one; only the status code and the code string are decided here.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query, Response, status
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from expirymanager.api.deps import CurrentUserDep, SchedulerDep
from expirymanager.api.errors import ApiError, CODE_VALIDATION_ERROR
from expirymanager.api.schemas.schedules import (
    DEFAULT_RUN_HISTORY,
    MAX_RUN_HISTORY,
    RunNowResult,
    ScheduleCreate,
    ScheduleRow,
    ScheduleRunRow,
    ScheduleUpdate,
)
from expirymanager.scheduler.service import SchedulerError

__all__ = ["router", "CODE_INVALID_CRON"]

log = logging.getLogger(__name__)

router = APIRouter()

CODE_INVALID_CRON = "invalid_cron"


def _api_error(exc: SchedulerError) -> ApiError:
    """Every refusal the service can raise already carries its documented status and code."""
    return ApiError(exc.status_code, exc.code, exc.message, detail=exc.detail)


def _parse(model: type, payload: dict[str, Any]) -> Any:
    """Validate a body, and report a bad cron expression as `400 invalid_cron`.

    The `input` and `ctx` members of a Pydantic error are dropped rather than redacted, for the
    same reason `api/errors.py` drops them: the field location says everything that is safe to
    say, and nothing here should be the thing that decides whether a value is echoed.
    """
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        errors = exc.errors()
        for error in errors:
            if "cron" in [str(part) for part in error.get("loc", ())]:
                raise ApiError(400, CODE_INVALID_CRON, str(error.get("msg", ""))) from exc
        raise ApiError(
            422,
            CODE_VALIDATION_ERROR,
            "The request body is not valid.",
            detail={
                "errors": [
                    {
                        "loc": [str(part) for part in error.get("loc", ())],
                        "type": str(error.get("type", "")),
                        "msg": str(error.get("msg", "")),
                    }
                    for error in errors
                ]
            },
        ) from exc


@router.get("/schedules", response_model=list[ScheduleRow], summary="List schedules")
async def list_schedules(user: CurrentUserDep, scheduler: SchedulerDep) -> list[ScheduleRow]:
    return await run_in_threadpool(scheduler.list_schedules)


@router.post(
    "/schedules",
    status_code=status.HTTP_201_CREATED,
    response_model=ScheduleRow,
    summary="Create a schedule",
)
async def create_schedule(
    payload: dict[str, Any],
    user: CurrentUserDep,
    scheduler: SchedulerDep,
) -> ScheduleRow:
    body: ScheduleCreate = _parse(ScheduleCreate, payload)
    try:
        return await run_in_threadpool(scheduler.create, body)
    except SchedulerError as exc:
        raise _api_error(exc) from exc


@router.patch(
    "/schedules/{schedule_id}",
    response_model=ScheduleRow,
    summary="Edit, enable or disable a schedule",
)
async def update_schedule(
    schedule_id: str,
    payload: dict[str, Any],
    user: CurrentUserDep,
    scheduler: SchedulerDep,
) -> ScheduleRow:
    body: ScheduleUpdate = _parse(ScheduleUpdate, payload)
    try:
        return await run_in_threadpool(scheduler.update, schedule_id, body)
    except SchedulerError as exc:
        raise _api_error(exc) from exc


@router.delete(
    "/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a schedule",
)
async def delete_schedule(
    schedule_id: str,
    user: CurrentUserDep,
    scheduler: SchedulerDep,
) -> Response:
    try:
        await run_in_threadpool(scheduler.delete, schedule_id)
    except SchedulerError as exc:
        raise _api_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/schedules/{schedule_id}/run-now",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunNowResult,
    summary="Fire a schedule now",
)
async def run_schedule_now(
    schedule_id: str,
    user: CurrentUserDep,
    scheduler: SchedulerDep,
) -> RunNowResult:
    """Runs the identical body the cron trigger runs, both guards included.

    A disabled schedule still fires here: a user pressing the button is an explicit request, not
    a misfire. Everything else, the auth guard and the budget guard, is the same, so the outcome
    the button reports is the outcome the timer would have recorded.
    """
    try:
        return await scheduler.run_now(schedule_id)
    except SchedulerError as exc:
        raise _api_error(exc) from exc


@router.get(
    "/schedules/{schedule_id}/runs",
    response_model=list[ScheduleRunRow],
    summary="Run history for one schedule",
)
async def schedule_runs(
    schedule_id: str,
    user: CurrentUserDep,
    scheduler: SchedulerDep,
    limit: int = Query(DEFAULT_RUN_HISTORY, ge=1, le=MAX_RUN_HISTORY),
) -> list[ScheduleRunRow]:
    try:
        return await run_in_threadpool(scheduler.runs, schedule_id, limit=limit)
    except SchedulerError as exc:
        raise _api_error(exc) from exc
