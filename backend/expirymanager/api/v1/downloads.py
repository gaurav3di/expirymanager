"""Price a download sheet, then commit it. API.md section 6, the creating half.

Two routes, and the whole design is in the relationship between them.

**A plan costs nothing.** `POST /downloads/plan` is answered entirely from the local registry, the
local contract catalog and the coverage ledger. Not one Fyers request is spent, which is what lets
a user price a sixty thousand request backfill, see that it will not fit in today's budget, narrow
the strike scope and price it again, all before committing to anything.

**The commit gate compares the number the user actually saw.** `confirm_requests` has to equal the
`requests_estimated` of the preview that was rendered. If the catalog moved underneath the sheet
between the preview and the Start button, the recount differs and the commit is refused with 409
`plan_changed`, so the user re-reads a price rather than starting work they never agreed to. The
gate is required on this route specifically. `DownloadRequest` leaves the field optional because a
schedule fire and an internal caller never saw a preview, but a request arriving over HTTP from a
browser did, and a browser that omits the field is a browser that skipped the estimate.

**The arithmetic is the planner's, once.** Neither route recomputes a cost, a row count or a
budget line. `POST /downloads/plan` returns `Plan.preview` unchanged, and `POST /downloads` returns
the preview the job service priced the commit against inside the very same call that wrote the
job. There is no second calculation in this module that could drift from the first, which matters
because that number is what the entire product steers by.

Both routes require a live broker token. Planning spends nothing, but a plan the user cannot act
on is a dead end, and a 409 `needs_reauth` is what raises the reconnect banner that gets them
moving again.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, status

from expirymanager.api.deps import CurrentUserDep, require_broker_token
from expirymanager.api.errors import ApiError
from expirymanager.api.schemas.downloads import (
    DownloadAccepted,
    DownloadRequest,
    PlanPreview,
    PlanRequest,
)
from expirymanager.api.v1.jobs import JobServiceDep, api_error
from expirymanager.pipeline.planner import PipelineRequestError

__all__ = ["router", "CODE_CONFIRM_REQUIRED"]

log = logging.getLogger(__name__)

router = APIRouter()

CODE_CONFIRM_REQUIRED = "confirm_requests_required"

# Applied to both routes rather than to each signature, so that adding a route to this module
# cannot accidentally leave out the token check that keeps a dead session from spending requests.
_BROKER = [Depends(require_broker_token)]


@router.post("/downloads/plan", response_model=PlanPreview, dependencies=_BROKER)
async def plan_download(
    request: PlanRequest,
    user: CurrentUserDep,
    service: JobServiceDep,
) -> PlanPreview:
    """Price a sheet from local state. Zero Fyers requests, by construction."""
    try:
        preview = await service.estimate(request)
    except PipelineRequestError as exc:
        raise api_error(exc) from exc
    log.info(
        "download plan priced",
        extra={
            "underlying_id": request.underlying_id,
            "expiries_planned": preview.expiries_planned,
            "requests_estimated": preview.requests_estimated,
            "exceeds_budget": preview.exceeds_budget,
        },
    )
    return preview


@router.post(
    "/downloads",
    response_model=DownloadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=_BROKER,
)
async def start_download(
    request: DownloadRequest,
    user: CurrentUserDep,
    service: JobServiceDep,
) -> DownloadAccepted:
    """Re-price, gate on the confirmed count, and write the job and all its tasks in one go."""
    if request.confirm_requests is None:
        raise ApiError(
            422,
            CODE_CONFIRM_REQUIRED,
            "Price this download before starting it. The estimate has to be confirmed so that "
            "the requests it will spend are the requests that were shown.",
        )
    try:
        accepted = await service.create(request, created_by=user.username)
    except PipelineRequestError as exc:
        raise api_error(exc) from exc
    return accepted
