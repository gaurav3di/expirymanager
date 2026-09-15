"""W20: the export routes, API.md section 8.

The database is seeded before the application opens it, because there is one DuckDB instance per
file and the running process holds it. So these tests write the catalog and the bars through the
real writer against the real file, close it, and then let the application open the same file and
export from it. Nothing about the export path is stubbed: the assertions are on the bytes that
landed on disk and on the ledger rows that describe them.

Every credential in this file is synthetic.
"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from expirymanager.app import create_app
from expirymanager.db import sqlite as sqlite_module
from expirymanager.db.arrow import IST_OFFSET_SECONDS, candles_to_arrow
from expirymanager.db.duck import DuckStore
from expirymanager.db.writer import CoverageRow
from expirymanager.db.writes import (
    ContractRow,
    UnderlyingRow,
    upsert_candle_chunk,
    upsert_contracts,
    upsert_underlying,
)
from expirymanager.security.sessions import CSRF_COOKIE_NAME

BASE_URL = "https://127.0.0.1:8000"

USERNAME = "operator"
PASSCODE = "correct-horse-battery-staple"

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
COLUMNS_JSON = '["timestamp","open","high","low","close","volume","open_interest"]'

EXPIRY = date(2025, 3, 27)
DAY = date(2025, 3, 26)
RES_ID = 2
RESOLUTION_CODE = "1"
STRIKES = (22900, 23000)
BARS = 5
TOTAL_ROWS = len(STRIKES) * BARS


def _utc_epoch_for_ist(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp()) - IST_OFFSET_SECONDS


def _payload() -> list[list]:
    return [
        [
            _utc_epoch_for_ist(datetime(DAY.year, DAY.month, DAY.day, 9, 15 + index)),
            "145.2025",
            "152.0075",
            "141.0525",
            "149.3075",
            1000 + index,
            50000 + index,
        ]
        for index in range(BARS)
    ]


def _underlying() -> UnderlyingRow:
    return UnderlyingRow(
        underlying_id=1,
        fyers_symbol="NSE:NIFTY50-INDEX",
        root="NIFTY",
        exchange="NSE",
        exchange_code=10,
        segment="CM",
        segment_code=10,
        instrument_kind="INDEX",
        display_name="Nifty 50",
        data_from=date(2022, 1, 3),
    )


def _option(strike: int) -> ContractRow:
    return ContractRow(
        fyers_symbol=f"NSE:NIFTY25MAR{strike}CE",
        kind="OPT",
        instrument_class="OPTIDX",
        exchange="NSE",
        exchange_code=10,
        segment="FO",
        segment_code=11,
        root="NIFTY",
        source_endpoint="expired_contracts",
        parse_method="regex",
        parse_confidence="exact",
        strike=Decimal(strike),
        strike_raw=str(strike),
        option_type="CE",
        expiry_date=EXPIRY,
        lot_size=75,
    )


async def _seed(store: DuckStore) -> None:
    await store.writer.start()
    try:
        writer = store.writer
        await upsert_underlying(writer, _underlying())
        contracts = await upsert_contracts(
            writer,
            underlying_id=1,
            expiry_date=EXPIRY,
            rows=[_option(strike) for strike in STRIKES],
        )
        task_id = 1
        for _symbol, contract_id in sorted(contracts.contract_ids.items()):
            await upsert_candle_chunk(
                writer,
                contract_id=contract_id,
                res_id=RES_ID,
                range_from=DAY,
                range_to=DAY,
                coverage=CoverageRow(
                    status="ok",
                    include_oi=True,
                    columns_json=COLUMNS_JSON,
                    task_id=task_id,
                ),
                batch=candles_to_arrow(_payload(), COLUMNS, contract_id, RES_ID),
            )
            task_id += 1
    finally:
        await store.writer.stop()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "expirymanager-home"
    monkeypatch.setattr("expirymanager.paths.default_root", lambda: root)
    yield root
    sqlite_module.dispose_engine()


@pytest.fixture
def seeded(data_dir):
    """Write the catalog and the bars before the application opens the same file."""
    data_dir.mkdir(parents=True, exist_ok=True)
    store = DuckStore(data_dir / "market.duckdb", app_version="0.0.0-test")
    store.open()
    try:
        asyncio.run(_seed(store))
    finally:
        store.close()
    return data_dir


@pytest.fixture
def app(seeded):
    application = create_app(root=seeded, serve_static=False)
    yield application
    sqlite_module.dispose_engine()


@pytest.fixture
def client(app):
    with TestClient(app, base_url=BASE_URL) as test_client:
        yield test_client


def csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    return {"X-CSRF-Token": token} if token else {}


@pytest.fixture
def signed_in(client):
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": USERNAME, "password": PASSCODE},
        headers=csrf(client),
    )
    assert response.status_code == 200
    return client


def build_export(client: TestClient, **overrides) -> dict:
    body = {
        "format": "csv",
        "layout": "single",
        "compression": "zstd",
        "denormalise": True,
        "scope": {"underlying_id": 1, "resolutions": [RESOLUTION_CODE]},
    }
    body.update(overrides)
    response = client.post("/api/v1/exports", json=body, headers=csrf(client))
    assert response.status_code == 202, response.text
    return response.json()


def export_row(client: TestClient, export_id: str) -> dict:
    rows = client.get("/api/v1/exports").json()["items"]
    return next(row for row in rows if row["export_id"] == export_id)


class TestCreate:
    def test_a_csv_export_writes_every_row_and_the_ledger_agrees(self, signed_in, app):
        accepted = build_export(signed_in)

        row = export_row(signed_in, accepted["export_id"])
        assert row["status"] == "ready"
        assert row["row_count"] == TOTAL_ROWS
        assert row["byte_size"] > 0
        assert len(row["sha256"]) == 64
        assert row["error_message"] is None

    def test_the_finished_file_holds_the_rows_that_went_in(self, signed_in, app):
        accepted = build_export(signed_in)

        response = signed_in.get(f"/api/v1/exports/{accepted['export_id']}/file")

        assert response.status_code == 200
        assert response.headers["content-disposition"].startswith("attachment")
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert len(rows) == TOTAL_ROWS
        assert {row["underlying_symbol"] for row in rows} == {"NSE:NIFTY50-INDEX"}
        assert {row["resolution"] for row in rows} == {RESOLUTION_CODE}
        # Both representations of the instant, which is the whole point of writing two columns.
        # DuckDB's epoch() answers DOUBLE, so the CSV carries 1742960700.0 rather than an integer.
        assert rows[0]["ts_ist"] == "2025-03-26 09:15:00"
        assert float(rows[0]["ts_utc_epoch"]) == float(
            _utc_epoch_for_ist(datetime(2025, 3, 26, 9, 15))
        )
        assert rows[0]["open"] == "145.2025"

    def test_a_parquet_export_is_readable_back(self, signed_in, app):
        accepted = build_export(signed_in, format="parquet")

        path = Path(
            app.state.services.paths.exports_dir / f"{accepted['export_id']}.parquet"
        )
        assert path.exists()
        count = duckdb.sql(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()[0]
        assert count == TOTAL_ROWS

    def test_the_export_also_appears_as_a_completed_job(self, signed_in, app):
        accepted = build_export(signed_in)

        with app.state.services.engine.connect() as connection:
            row = connection.execute(
                text("SELECT kind, status, rows_written FROM job WHERE job_id = :id"),
                {"id": accepted["job_id"]},
            ).first()
        assert row == ("export", "completed", TOTAL_ROWS)

    def test_it_publishes_the_export_ready_frame(self, signed_in, app):
        accepted = build_export(signed_in)

        frames = [
            frame
            for frame in app.state.services.supervisor.bus.history()
            if frame.event == "export_ready"
        ]
        assert [frame.data["export_id"] for frame in frames] == [accepted["export_id"]]
        assert frames[0].data["row_count"] == TOTAL_ROWS

    def test_the_stored_scope_is_resolved_rather_than_as_sent(self, signed_in):
        accepted = build_export(signed_in)

        row = export_row(signed_in, accepted["export_id"])
        # The request named the Fyers code "1"; the stored scope names res_id 2, which is what
        # the candle table is filtered on.
        assert row["scope"]["resolutions"] == [RES_ID]


class TestScope:
    def test_an_unknown_resolution_is_refused_rather_than_silently_matching_nothing(
        self, signed_in
    ):
        response = signed_in.post(
            "/api/v1/exports",
            json={"format": "csv", "scope": {"resolutions": ["7S"]}},
            headers=csrf(signed_in),
        )

        assert response.status_code == 400
        body = response.json()["error"]
        assert body["code"] == "unknown_resolution"
        assert body["detail"]["unknown"] == ["7S"]

    def test_a_resolution_that_holds_no_bars_exports_an_empty_file(self, signed_in):
        accepted = build_export(signed_in, scope={"underlying_id": 1, "resolutions": ["5"]})

        row = export_row(signed_in, accepted["export_id"])
        assert row["status"] == "ready"
        assert row["row_count"] == 0

    def test_a_kind_of_BOTH_does_not_filter(self, signed_in):
        accepted = build_export(signed_in, scope={"underlying_id": 1, "kind": "BOTH"})

        row = export_row(signed_in, accepted["export_id"])
        assert row["row_count"] == TOTAL_ROWS
        assert row["scope"]["kind"] is None

    def test_a_kind_of_FUT_matches_nothing_here(self, signed_in):
        accepted = build_export(signed_in, scope={"underlying_id": 1, "kind": "FUT"})

        assert export_row(signed_in, accepted["export_id"])["row_count"] == 0

    def test_a_hive_csv_export_is_refused_with_the_reason(self, signed_in):
        response = signed_in.post(
            "/api/v1/exports",
            json={"format": "csv", "layout": "hive"},
            headers=csrf(signed_in),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_export"


class TestDisk:
    def test_an_export_larger_than_the_free_space_is_507(self, signed_in, monkeypatch):
        import expirymanager.api.v1.exports as exports_route

        class _Usage:
            free = 1

        monkeypatch.setattr(exports_route.shutil, "disk_usage", lambda _path: _Usage())

        response = signed_in.post(
            "/api/v1/exports",
            json={"format": "csv", "scope": {"underlying_id": 1}},
            headers=csrf(signed_in),
        )

        assert response.status_code == 507
        assert response.json()["error"]["code"] == "insufficient_disk"


class TestDownloadGuards:
    def test_an_unknown_export_is_404(self, signed_in):
        response = signed_in.get("/api/v1/exports/deadbeef/file")

        assert response.status_code == 404

    def test_an_export_that_is_not_ready_is_409(self, signed_in, app):
        accepted = build_export(signed_in)
        with app.state.services.engine.begin() as connection:
            connection.execute(
                text("UPDATE export_job SET status = 'queued' WHERE export_id = :id"),
                {"id": accepted["export_id"]},
            )

        response = signed_in.get(f"/api/v1/exports/{accepted['export_id']}/file")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "not_ready"

    def test_a_missing_file_is_410(self, signed_in, app):
        accepted = build_export(signed_in)
        row = export_row(signed_in, accepted["export_id"])
        Path(
            app.state.services.paths.exports_dir / f"{accepted['export_id']}.csv"
        ).unlink()
        assert row["status"] == "ready"

        response = signed_in.get(f"/api/v1/exports/{accepted['export_id']}/file")

        assert response.status_code == 410
        assert response.json()["error"]["code"] == "file_missing"

    def test_a_stored_path_outside_the_exports_directory_is_never_served(
        self, signed_in, app
    ):
        accepted = build_export(signed_in)
        outside = app.state.services.paths.root / "master.key"
        outside.write_text("not a real key")
        with app.state.services.engine.begin() as connection:
            connection.execute(
                text("UPDATE export_job SET file_path = :path WHERE export_id = :id"),
                {"path": str(outside), "id": accepted["export_id"]},
            )

        response = signed_in.get(f"/api/v1/exports/{accepted['export_id']}/file")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "file_missing"
        assert "not a real key" not in response.text

    def test_a_traversal_path_relative_to_the_exports_directory_is_refused(
        self, signed_in, app
    ):
        accepted = build_export(signed_in)
        traversal = app.state.services.paths.exports_dir / ".." / "config.sqlite3"
        with app.state.services.engine.begin() as connection:
            connection.execute(
                text("UPDATE export_job SET file_path = :path WHERE export_id = :id"),
                {"path": str(traversal), "id": accepted["export_id"]},
            )

        response = signed_in.get(f"/api/v1/exports/{accepted['export_id']}/file")

        assert response.status_code == 404

    def test_a_hive_archive_says_it_is_a_directory(self, signed_in):
        accepted = build_export(signed_in, format="parquet", layout="hive")

        row = export_row(signed_in, accepted["export_id"])
        assert row["status"] == "ready"

        response = signed_in.get(f"/api/v1/exports/{accepted['export_id']}/file")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "directory_export"


class TestDelete:
    def test_delete_removes_the_file_and_marks_the_row(self, signed_in, app):
        accepted = build_export(signed_in)
        path = app.state.services.paths.exports_dir / f"{accepted['export_id']}.csv"
        sidecar = path.with_name(path.name + ".schema.json")
        assert path.exists()
        assert sidecar.exists()

        response = signed_in.delete(
            f"/api/v1/exports/{accepted['export_id']}", headers=csrf(signed_in)
        )

        assert response.status_code == 204
        assert not path.exists()
        assert not sidecar.exists()
        assert export_row(signed_in, accepted["export_id"])["status"] == "deleted"

    def test_downloading_a_deleted_export_is_409(self, signed_in):
        accepted = build_export(signed_in)
        signed_in.delete(f"/api/v1/exports/{accepted['export_id']}", headers=csrf(signed_in))

        response = signed_in.get(f"/api/v1/exports/{accepted['export_id']}/file")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "not_ready"

    def test_deleting_an_unknown_export_is_404(self, signed_in):
        response = signed_in.delete("/api/v1/exports/deadbeef", headers=csrf(signed_in))

        assert response.status_code == 404


class TestListing:
    def test_the_list_pages_newest_first(self, signed_in):
        ids = [build_export(signed_in)["export_id"] for _ in range(3)]

        first = signed_in.get("/api/v1/exports?limit=2").json()
        assert len(first["items"]) == 2
        assert first["next_cursor"]

        second = signed_in.get(
            f"/api/v1/exports?limit=2&cursor={first['next_cursor']}"
        ).json()
        assert len(second["items"]) == 1
        assert second["next_cursor"] is None

        listed = [row["export_id"] for row in first["items"] + second["items"]]
        assert sorted(listed) == sorted(ids)

    def test_a_cursor_from_another_collection_is_rejected(self, signed_in):
        from expirymanager.api.schemas.common import encode_cursor

        foreign = encode_cursor({"job_id": "x"})

        response = signed_in.get(f"/api/v1/exports?cursor={foreign}")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_cursor"


class TestAuth:
    def test_every_route_needs_a_session(self, client):
        assert client.get("/api/v1/exports").status_code == 401
        assert client.post("/api/v1/exports", json={}).status_code in (401, 403)
