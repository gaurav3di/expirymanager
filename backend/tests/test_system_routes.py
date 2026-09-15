"""W20: the system routes, API.md section 10.

The two operations that touch the whole database at once, optimise and backup, are run for real
here rather than mocked. A mocked compaction proves that a function was called; what matters is
that the rows survive the rewrite and that the writer is usable afterwards, and only running it
shows that.

The settings tests assert the bounds the Settings screen renders its controls from. Those bounds
are read out of the real validator in `settings_store`, so if that module changes shape these
tests fail rather than every control quietly flattening into a text box.

Every credential in this file is synthetic.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from expirymanager.api.v1.system import write_notification
from expirymanager.app import create_app
from expirymanager.db import sqlite as sqlite_module
from expirymanager.security.sessions import CSRF_COOKIE_NAME

BASE_URL = "https://127.0.0.1:8000"

USERNAME = "operator"
PASSCODE = "correct-horse-battery-staple"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "expirymanager-home"
    monkeypatch.setattr("expirymanager.paths.default_root", lambda: root)
    yield root
    sqlite_module.dispose_engine()


@pytest.fixture
def app(data_dir):
    application = create_app(root=data_dir, serve_static=False)
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


class TestBudget:
    def test_it_reports_the_governor_rather_than_a_second_counter(self, signed_in, app):
        snapshot = app.state.services.governor.snapshot()

        body = signed_in.get("/api/v1/system/budget").json()

        assert body["ist_date"] == snapshot.ist_date
        assert body["plan"] == snapshot.plan
        assert body["plan_limit_day"] == snapshot.plan_limit_day
        assert body["remaining"] == snapshot.requests_remaining
        assert body["minute_headroom"] == snapshot.per_minute
        assert body["strikes_remaining"] == snapshot.strikes_remaining
        assert body["pipeline_mode"] == "running"
        assert body["pipeline_reason"] is None

    def test_the_sweep_reserve_comes_from_the_settings_store(self, signed_in, app):
        app.state.services.settings.set("budget_reserve_fraction", 0.55)

        body = signed_in.get("/api/v1/system/budget").json()

        assert body["sweep_reserve_fraction"] == 0.55

    def test_it_follows_the_governor_into_a_paused_mode(self, signed_in, app):
        import asyncio

        from expirymanager.brokers.fyers.throttle import GovernorMode

        governor = app.state.services.governor
        asyncio.run(governor.set_mode(GovernorMode.PAUSED_USER, reason="paused by the user"))

        body = signed_in.get("/api/v1/system/budget").json()

        assert body["pipeline_mode"] == "paused_user"
        assert body["pipeline_reason"] == "paused by the user"


class TestStorage:
    def test_it_reports_the_real_files(self, signed_in, app):
        body = signed_in.get("/api/v1/system/storage").json()

        assert body["duckdb_bytes"] == app.state.services.paths.duckdb_file.stat().st_size
        assert body["sqlite_bytes"] == app.state.services.paths.sqlite_db.stat().st_size
        assert body["candle_rows"] == 0
        assert body["compaction_suggested"] is False
        assert body["free_disk_bytes"] > 0


class TestHealth:
    def test_every_assertion_reports_a_status(self, signed_in):
        body = signed_in.get("/api/v1/system/health").json()

        names = {row["check_name"] for row in body["rows"]}
        assert "duplicate_keys" in names
        assert "overlapping_id_blocks" in names
        assert all(row["status"] == "ok" for row in body["rows"])
        assert all(row["offending"] == 0 for row in body["rows"])
        assert body["last_maintenance"] is None

    def test_it_reports_the_last_maintenance_run(self, signed_in):
        fired = signed_in.post(
            "/api/v1/schedules/builtin_maintenance/run-now", headers=csrf(signed_in)
        )
        assert fired.status_code == 202

        body = signed_in.get("/api/v1/system/health").json()

        assert body["last_maintenance"]["outcome"] == "completed"
        assert body["last_maintenance"]["ran_at"]
        assert body["last_maintenance"]["detail"]


class TestCheckpoint:
    def test_it_reports_the_write_ahead_log_on_both_sides(self, signed_in):
        response = signed_in.post("/api/v1/system/checkpoint", headers=csrf(signed_in))

        assert response.status_code == 200
        body = response.json()
        assert body["wal_bytes_after"] == 0
        assert body["wal_bytes_before"] >= 0


class TestOptimise:
    def test_it_refuses_without_a_confirmation(self, signed_in):
        response = signed_in.post(
            "/api/v1/system/optimise", json={"confirm": False}, headers=csrf(signed_in)
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "confirm_required"

    def test_it_refuses_while_a_job_is_live(self, signed_in, app):
        with app.state.services.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO job (job_id, kind, status, params_json, created_at)"
                    " VALUES ('j1', 'candle_backfill', 'running', '{}', '2026-01-01T00:00:00Z')"
                )
            )

        response = signed_in.post(
            "/api/v1/system/optimise", json={"confirm": True}, headers=csrf(signed_in)
        )

        assert response.status_code == 409
        body = response.json()["error"]
        assert body["code"] == "pipeline_busy"
        assert body["detail"]["live_jobs"] == 1

    def test_it_refuses_when_the_disk_cannot_hold_a_second_copy(self, signed_in, monkeypatch):
        import expirymanager.api.v1.system as system_route

        class _Usage:
            free = 1

        monkeypatch.setattr(system_route.shutil, "disk_usage", lambda _path: _Usage())

        response = signed_in.post(
            "/api/v1/system/optimise", json={"confirm": True}, headers=csrf(signed_in)
        )

        assert response.status_code == 507
        assert response.json()["error"]["code"] == "insufficient_disk"

    def test_it_rewrites_the_database_and_leaves_it_usable(self, signed_in, app):
        response = signed_in.post(
            "/api/v1/system/optimise", json={"confirm": True}, headers=csrf(signed_in)
        )

        assert response.status_code == 202
        assert response.json()["job_id"]

        # The outcome of an operation nobody stayed to watch is a notification row, and the
        # database is open and answering afterwards.
        notifications = signed_in.get("/api/v1/system/notifications").json()
        assert any(item["code"] == "compaction_done" for item in notifications)
        assert app.state.services.duck.writer.running
        assert signed_in.get("/api/v1/system/storage").json()["duckdb_bytes"] > 0
        assert signed_in.get("/api/v1/system/health").status_code == 200


class TestBackup:
    def test_it_copies_the_duckdb_and_the_sqlite_together(self, signed_in, app, tmp_path):
        target = tmp_path / "backup-target"

        response = signed_in.post(
            "/api/v1/system/backup",
            json={"target_dir": str(target)},
            headers=csrf(signed_in),
        )

        assert response.status_code == 202
        written = response.json()["target_dir"]
        copied = {path.name for path in (target).rglob("*") if path.is_file()}
        assert "market.duckdb" in copied
        assert "config.sqlite3" in copied
        assert written.startswith(str(target))

        notifications = signed_in.get("/api/v1/system/notifications").json()
        assert any(item["code"] == "backup_done" for item in notifications)

    def test_it_defaults_to_the_backups_directory(self, signed_in, app):
        response = signed_in.post("/api/v1/system/backup", json={}, headers=csrf(signed_in))

        assert response.status_code == 202
        target = response.json()["target_dir"]
        assert target.startswith(str(app.state.services.paths.backups_dir))
        assert (app.state.services.paths.backups_dir).exists()


class TestNotifications:
    def test_read_and_dismiss_change_the_row(self, signed_in, app):
        engine = app.state.services.engine
        notification_id = write_notification(
            engine,
            level="warning",
            code="needs_reauth",
            title="Connect Fyers again",
            body="The token expired.",
        )

        listed = signed_in.get("/api/v1/system/notifications").json()
        assert [item["notification_id"] for item in listed] == [notification_id]
        assert listed[0]["read_at"] is None

        assert (
            signed_in.post(
                f"/api/v1/system/notifications/{notification_id}/read",
                headers=csrf(signed_in),
            ).status_code
            == 204
        )
        assert signed_in.get("/api/v1/system/notifications").json()[0]["read_at"]
        assert signed_in.get("/api/v1/system/notifications?unread_only=true").json() == []

        assert (
            signed_in.post(
                f"/api/v1/system/notifications/{notification_id}/dismiss",
                headers=csrf(signed_in),
            ).status_code
            == 204
        )
        assert signed_in.get("/api/v1/system/notifications").json() == []

    def test_marking_an_unknown_notification_is_404(self, signed_in):
        response = signed_in.post(
            "/api/v1/system/notifications/nope/read", headers=csrf(signed_in)
        )

        assert response.status_code == 404

    def test_marking_twice_is_not_an_error(self, signed_in, app):
        notification_id = write_notification(
            app.state.services.engine,
            level="info",
            code="compaction_suggested",
            title="Consider optimising",
        )

        first = signed_in.post(
            f"/api/v1/system/notifications/{notification_id}/read", headers=csrf(signed_in)
        )
        second = signed_in.post(
            f"/api/v1/system/notifications/{notification_id}/read", headers=csrf(signed_in)
        )

        assert first.status_code == 204
        assert second.status_code == 204


class TestSettings:
    def test_it_ships_the_spec_with_the_value(self, signed_in):
        descriptors = signed_in.get("/api/v1/system/settings").json()
        by_key = {item["key"]: item for item in descriptors}

        plan = by_key["plan_tier"]
        assert plan["value_type"] == "str"
        assert plan["choices"] == ["standard", "prime"]
        assert plan["requires_restart"] is True

        workers = by_key["worker_count"]
        assert workers["value_type"] == "int"
        assert workers["minimum"] == 1
        assert workers["maximum"] == 32
        assert workers["choices"] is None
        assert workers["description"]

        assert by_key["chart_persist"]["value_type"] == "bool"
        assert by_key["budget_reserve_fraction"]["value_type"] == "float"
        assert by_key["default_resolutions"]["value_type"] == "list"

    def test_a_patch_reaches_the_one_settings_store(self, signed_in, app):
        response = signed_in.patch(
            "/api/v1/system/settings", json={"chunk_days": 40}, headers=csrf(signed_in)
        )

        assert response.status_code == 200
        returned = {item["key"]: item["value"] for item in response.json()}
        assert returned["chunk_days"] == 40
        # The store every other component reads, not a second copy.
        assert app.state.services.settings.get_int("chunk_days") == 40
        with app.state.services.engine.connect() as connection:
            stored = connection.execute(
                text("SELECT value_json FROM settings WHERE key = 'chunk_days'")
            ).scalar_one()
        assert stored == "40"

    def test_an_unknown_setting_is_400_unknown_setting(self, signed_in):
        response = signed_in.patch(
            "/api/v1/system/settings", json={"nonsense": 1}, headers=csrf(signed_in)
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unknown_setting"

    def test_a_value_out_of_range_is_400_and_writes_nothing(self, signed_in, app):
        before = app.state.services.settings.get_int("worker_count")

        response = signed_in.patch(
            "/api/v1/system/settings",
            json={"chunk_days": 30, "worker_count": 9999},
            headers=csrf(signed_in),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "setting_out_of_range"
        # All or nothing: the valid key in the same body was not written either.
        assert app.state.services.settings.get_int("worker_count") == before
        assert app.state.services.settings.get_int("chunk_days") != 30

    def test_a_string_where_an_integer_belongs_is_refused(self, signed_in):
        response = signed_in.patch(
            "/api/v1/system/settings",
            json={"worker_count": "many"},
            headers=csrf(signed_in),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "setting_out_of_range"


class TestRequestLog:
    def _insert_task(self, engine, **overrides) -> None:
        row = {
            "job_id": "j1",
            "seq": 1,
            "kind": "candle_chunk",
            "state": "done",
            "not_before": "2026-01-01T00:00:00Z",
            "created_at": "2026-01-01T00:00:00Z",
            "http_status": 200,
            "latency_ms": 412,
            "fyers_symbol": "NSE:NIFTY25MAR23000CE",
            "request_params_json": '{"symbol": "NSE:NIFTY25MAR23000CE", "token": "secret"}',
        }
        row.update(overrides)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO job (job_id, kind, status, params_json, created_at)"
                    " VALUES (:job_id, 'candle_backfill', 'completed', '{}', :created_at)"
                    " ON CONFLICT(job_id) DO NOTHING"
                ),
                {"job_id": row["job_id"], "created_at": row["created_at"]},
            )
            columns = ", ".join(row)
            placeholders = ", ".join(f":{name}" for name in row)
            connection.execute(
                text(f"INSERT INTO task ({columns}) VALUES ({placeholders})"), row
            )

    def test_a_task_row_is_projected_as_a_request(self, signed_in, app):
        self._insert_task(app.state.services.engine)

        rows = signed_in.get("/api/v1/system/requests").json()

        assert len(rows) == 1
        assert rows[0]["endpoint"] == "/data/history/fno/expired/historical-data"
        assert rows[0]["outcome"] == "done"
        assert rows[0]["http_status"] == 200
        assert rows[0]["latency_ms"] == 412
        assert rows[0]["fyers_symbol"] == "NSE:NIFTY25MAR23000CE"

    def test_a_parameter_that_looks_like_a_secret_never_leaves(self, signed_in, app):
        self._insert_task(app.state.services.engine)

        response = signed_in.get("/api/v1/system/requests")

        assert "secret" not in response.text
        assert response.json()[0]["params"]["symbol"] == "NSE:NIFTY25MAR23000CE"

    def test_the_filters_use_the_vocabularies_the_rows_are_stored_in(self, signed_in, app):
        engine = app.state.services.engine
        self._insert_task(engine, seq=1)
        self._insert_task(engine, seq=2, kind="spot_chunk", state="empty")

        assert len(signed_in.get("/api/v1/system/requests").json()) == 2
        by_kind = signed_in.get("/api/v1/system/requests?endpoint=spot_chunk").json()
        assert [row["endpoint"] for row in by_kind] == ["/data/history"]
        by_state = signed_in.get("/api/v1/system/requests?outcome=empty").json()
        assert len(by_state) == 1
        assert signed_in.get("/api/v1/system/requests?limit=1").json().__len__() == 1

    def test_since_narrows_by_creation_time(self, signed_in, app):
        engine = app.state.services.engine
        self._insert_task(engine, seq=1, created_at="2026-01-01T00:00:00Z")
        self._insert_task(engine, seq=2, created_at="2026-06-01T00:00:00Z")

        rows = signed_in.get("/api/v1/system/requests?since=2026-03-01T00:00:00Z").json()

        assert len(rows) == 1
        assert rows[0]["requested_at"] == "2026-06-01T00:00:00Z"


class TestAuth:
    def test_every_route_needs_a_session(self, client):
        assert client.get("/api/v1/system/budget").status_code == 401
        assert client.get("/api/v1/system/settings").status_code == 401
        assert client.get("/api/v1/system/requests").status_code == 401

    def test_a_mutation_without_the_csrf_header_is_rejected(self, signed_in):
        response = signed_in.patch("/api/v1/system/settings", json={"chunk_days": 40})

        assert response.status_code == 403
