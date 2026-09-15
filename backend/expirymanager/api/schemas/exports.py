"""Request and response models for the exports surface, API.md section 8.

Two decisions are made here rather than in the route, because both of them are the difference
between an export that quietly holds less than the user asked for and one that refuses with a
reason.

Resolutions travel as Fyers codes. Every other part of this API speaks the broker's vocabulary
(`"1"`, `"5"`, `"5S"`, `"D"`), and `db/exports.ExportScope` filters on `res_id`, an integer. The
translation happens once, in the route, against `dim_resolution`. Accepting a raw integer here
would mean `"D"` silently matched nothing and `"1"` selected the five second series, because
`res_id` 1 is `5S` while `fyers_code` `"1"` is `res_id` 2. So the model carries strings and the
route resolves them, and an unknown code is a 400 rather than an empty file.

`kind` admits `BOTH`, which the frontend sends when the user has not narrowed the instrument
class. It becomes `None` on the way into the scope, because `ExportScope.kind` is a predicate and
`BOTH` is the absence of one.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import Field

from expirymanager.api.schemas.common import ApiModel, RequestModel

__all__ = [
    "EXPORT_FORMATS",
    "EXPORT_LAYOUTS",
    "EXPORT_COMPRESSIONS",
    "EXPORT_STATUSES",
    "EXPORT_KINDS",
    "MAX_EXPORT_PAGE_SIZE",
    "DEFAULT_EXPORT_PAGE_SIZE",
    "ExportScopeModel",
    "ExportCreateRequest",
    "ExportAccepted",
    "ExportRow",
]

EXPORT_FORMATS = ("parquet", "csv")
EXPORT_LAYOUTS = ("single", "hive")
EXPORT_COMPRESSIONS = ("zstd", "snappy", "gzip", "uncompressed")
EXPORT_STATUSES = ("queued", "running", "ready", "failed", "deleted")

# `BOTH` is the frontend's way of saying "do not filter on instrument class".
EXPORT_KINDS = ("FUT", "OPT", "BOTH")

DEFAULT_EXPORT_PAGE_SIZE = 50
MAX_EXPORT_PAGE_SIZE = 200


class ExportScopeModel(RequestModel):
    """What to export. Every field narrows; none of them is required."""

    underlying_id: int | None = Field(default=None, ge=1)
    expiry_from: date | None = None
    expiry_to: date | None = None
    resolutions: list[str] = Field(default_factory=list, max_length=32)
    kind: Literal["FUT", "OPT", "BOTH"] | None = None
    option_type: Literal["CE", "PE"] | None = None
    contract_ids: list[int] = Field(default_factory=list, max_length=5000)
    include_catalog: bool = False

    def as_params(self, res_ids: list[int]) -> dict[str, Any]:
        """The scope as the plain dict `handlers/export.scope_from_params` reads.

        `res_ids` is what the route resolved from `self.resolutions`. Passing it in rather than
        resolving here keeps the one database read in the route, where the reader lives.
        """
        return {
            "underlying_id": self.underlying_id,
            "expiry_from": self.expiry_from.isoformat() if self.expiry_from else None,
            "expiry_to": self.expiry_to.isoformat() if self.expiry_to else None,
            "resolutions": list(res_ids),
            "kind": None if self.kind in (None, "BOTH") else self.kind,
            "option_type": self.option_type,
            "contract_ids": list(self.contract_ids),
            "include_catalog": self.include_catalog,
        }


class ExportCreateRequest(RequestModel):
    """`POST /api/v1/exports`."""

    format: Literal["parquet", "csv"] = "parquet"
    layout: Literal["single", "hive"] = "single"
    compression: Literal["zstd", "snappy", "gzip", "uncompressed"] = "zstd"
    denormalise: bool = True
    scope: ExportScopeModel = Field(default_factory=ExportScopeModel)

    def as_params(self, res_ids: list[int]) -> dict[str, Any]:
        """The full parameter dict, which is also what is stored in `export_job.scope_json`.

        Stored resolved rather than as sent, so that reading the row back through
        `spec_from_params` rebuilds exactly the export that ran.
        """
        params = self.scope.as_params(res_ids)
        params.update(
            {
                "format": self.format,
                "layout": self.layout,
                "compression": self.compression,
                "denormalise": self.denormalise,
            }
        )
        return params


class ExportAccepted(ApiModel):
    """`202` from `POST /api/v1/exports`."""

    export_id: str
    job_id: str | None = None
    status: str = "queued"


class ExportRow(ApiModel):
    """One `export_job` row, as `GET /api/v1/exports` pages them.

    `error_message` rather than the column's `error_text`: the frontend type was written from
    API.md and names it that way, and the route is the place the two vocabularies meet.
    """

    export_id: str
    job_id: str | None = None
    status: str
    format: str
    layout: str
    compression: str | None = None
    row_count: int | None = None
    byte_size: int | None = None
    sha256: str | None = None
    created_at: str
    finished_at: str | None = None
    error_message: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
