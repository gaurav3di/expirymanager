"""W20: the schedule routes, API.md section 9.

Every test runs a real application over a temporary data directory, so the schedules these
assertions see are the twelve builtin rows migration 0005 seeds and the real
`SchedulerService` rebuilt from them. Nothing is stubbed, because the properties worth asserting
here are exactly the ones a stub satisfies for free: that a create reaches the table and the
running scheduler, that a builtin cannot be deleted, and that Run now writes the run history row
the screen reads back.

Every credential in this file is synthetic.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

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


class TestListing:
    def test_it_lists_every_builtin_schedule(self, signed_in):
        response = signed_in.get("/api/v1/schedules")

        assert response.status_code == 200
        rows = response.json()
        ids = {row["schedule_id"] for row in rows}
        assert "builtin_maintenance" in ids
        assert "builtin_rolling_backfill" in ids
        assert all(row["is_builtin"] for row in rows)

    def test_a_row_carries_the_description_of_its_action(self, signed_in):
        rows = signed_in.get("/api/v1/schedules").json()
        maintenance = next(r for r in rows if r["schedule_id"] == "builtin_maintenance")

        assert maintenance["kind"] == "maintenance"
        assert maintenance["cron"] == "0 2 * * *"
        assert maintenance["description"]

    def test_the_running_scheduler_holds_a_trigger_for_every_enabled_row(self, signed_in, app):
        rows = signed_in.get("/api/v1/schedules").json()
        enabled = {row["schedule_id"] for row in rows if row["enabled"]}
        installed = set(app.state.services.scheduler.snapshot().schedule_ids)

        # The list is not a report of intent. What the screen shows is what will fire.
        assert enabled == installed


class TestCreate:
    def test_a_bad_cron_expression_is_400_invalid_cron(self, signed_in):
        response = signed_in.post(
            "/api/v1/schedules",
            json={
                "name": "Broken",
                "kind": "maintenance",
                "cron": "not a cron expression",
            },
            headers=csrf(signed_in),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_cron"

    def test_a_cron_with_the_wrong_field_count_is_also_invalid_cron(self, signed_in):
        response = signed_in.post(
            "/api/v1/schedules",
            json={"name": "Broken", "kind": "maintenance", "cron": "0 2 * *"},
            headers=csrf(signed_in),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_cron"

    def test_an_unknown_kind_is_400_unknown_kind_and_names_what_is_known(self, signed_in):
        response = signed_in.post(
            "/api/v1/schedules",
            json={"name": "Nonsense", "kind": "not_a_kind", "cron": "0 2 * * *"},
            headers=csrf(signed_in),
        )

        assert response.status_code == 400
        body = response.json()["error"]
        assert body["code"] == "unknown_kind"
        assert "maintenance" in body["detail"]["known_kinds"]

    def test_a_field_that_is_not_the_cron_is_still_a_422(self, signed_in):
        response = signed_in.post(
            "/api/v1/schedules",
            json={
                "name": "",
                "kind": "maintenance",
                "cron": "0 2 * * *",
            },
            headers=csrf(signed_in),
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    def test_a_created_schedule_reaches_the_table_and_the_running_scheduler(
        self, signed_in, app
    ):
        response = signed_in.post(
            "/api/v1/schedules",
            json={
                "name": "Nightly maintenance",
                "kind": "maintenance",
                "cron": "30 3 * * *",
                "params": {"note": "mine"},
                "max_requests_per_run": 100,
            },
            headers=csrf(signed_in),
        )

        assert response.status_code == 201
        created = response.json()
        assert created["is_builtin"] is False
        assert created["cron"] == "30 3 * * *"
        assert created["params"] == {"note": "mine"}
        assert created["next_fire_at"]

        with app.state.services.engine.connect() as connection:
            row = connection.execute(
                text("SELECT name, cron FROM schedule WHERE schedule_id = :id"),
                {"id": created["schedule_id"]},
            ).first()
        assert row == ("Nightly maintenance", "30 3 * * *")
        assert created["schedule_id"] in app.state.services.scheduler.snapshot().schedule_ids


class TestUpdate:
    def test_disabling_a_builtin_removes_its_trigger_and_keeps_the_row(self, signed_in, app):
        response = signed_in.patch(
            "/api/v1/schedules/builtin_maintenance",
            json={"enabled": False},
            headers=csrf(signed_in),
        )

        assert response.status_code == 200
        assert response.json()["enabled"] is False
        assert "builtin_maintenance" not in app.state.services.scheduler.snapshot().schedule_ids

        rows = signed_in.get("/api/v1/schedules").json()
        assert any(row["schedule_id"] == "builtin_maintenance" for row in rows)

    def test_re_enabling_puts_the_trigger_back(self, signed_in, app):
        signed_in.patch(
            "/api/v1/schedules/builtin_maintenance",
            json={"enabled": False},
            headers=csrf(signed_in),
        )
        response = signed_in.patch(
            "/api/v1/schedules/builtin_maintenance",
            json={"enabled": True},
            headers=csrf(signed_in),
        )

        assert response.status_code == 200
        assert "builtin_maintenance" in app.state.services.scheduler.snapshot().schedule_ids

    def test_re_timing_a_builtin_installs_the_new_trigger(self, signed_in, app):
        response = signed_in.patch(
            "/api/v1/schedules/builtin_maintenance",
            json={"cron": "45 4 * * *"},
            headers=csrf(signed_in),
        )

        assert response.status_code == 200
        assert response.json()["cron"] == "45 4 * * *"
        job = next(
            job
            for job in app.state.services.scheduler.scheduler.get_jobs()
            if job.id == "builtin_maintenance"
        )
        assert job.next_run_time.hour == 4
        assert job.next_run_time.minute == 45

    def test_a_bad_cron_on_a_patch_is_400_invalid_cron(self, signed_in):
        response = signed_in.patch(
            "/api/v1/schedules/builtin_maintenance",
            json={"cron": "every other tuesday"},
            headers=csrf(signed_in),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_cron"

    def test_an_unknown_schedule_is_404(self, signed_in):
        response = signed_in.patch(
            "/api/v1/schedules/does-not-exist",
            json={"enabled": False},
            headers=csrf(signed_in),
        )

        assert response.status_code == 404


class TestDelete:
    def test_a_builtin_cannot_be_deleted(self, signed_in, app):
        response = signed_in.delete(
            "/api/v1/schedules/builtin_maintenance", headers=csrf(signed_in)
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "builtin_schedule"
        with app.state.services.engine.connect() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM schedule WHERE schedule_id = 'builtin_maintenance'")
            ).scalar_one()
        assert count == 1

    def test_a_custom_schedule_is_deleted_and_its_trigger_removed(self, signed_in, app):
        created = signed_in.post(
            "/api/v1/schedules",
            json={"name": "Temporary", "kind": "maintenance", "cron": "0 5 * * *"},
            headers=csrf(signed_in),
        ).json()

        response = signed_in.delete(
            f"/api/v1/schedules/{created['schedule_id']}", headers=csrf(signed_in)
        )

        assert response.status_code == 204
        ids = {row["schedule_id"] for row in signed_in.get("/api/v1/schedules").json()}
        assert created["schedule_id"] not in ids
        assert (
            created["schedule_id"]
            not in app.state.services.scheduler.snapshot().schedule_ids
        )


class TestRunNowAndHistory:
    def test_run_now_records_a_run_and_the_history_route_reads_it_back(self, signed_in, app):
        response = signed_in.post(
            "/api/v1/schedules/builtin_maintenance/run-now", headers=csrf(signed_in)
        )

        assert response.status_code == 202
        result = response.json()
        # Maintenance needs no token and spends no budget, so it runs rather than skipping.
        assert result["outcome"] == "completed"
        assert result["job_id"] is None

        history = signed_in.get("/api/v1/schedules/builtin_maintenance/runs")
        assert history.status_code == 200
        runs = history.json()
        assert len(runs) == 1
        assert runs[0]["run_id"] == result["run_id"]
        assert runs[0]["outcome"] == "completed"
        assert runs[0]["note"]

        with app.state.services.engine.connect() as connection:
            stored = connection.execute(
                text("SELECT outcome FROM schedule_run WHERE run_id = :id"),
                {"id": result["run_id"]},
            ).scalar_one()
        assert stored == "completed"

    def test_run_now_fires_a_disabled_schedule_because_the_user_asked(self, signed_in):
        signed_in.patch(
            "/api/v1/schedules/builtin_maintenance",
            json={"enabled": False},
            headers=csrf(signed_in),
        )

        response = signed_in.post(
            "/api/v1/schedules/builtin_maintenance/run-now", headers=csrf(signed_in)
        )

        assert response.status_code == 202
        assert response.json()["outcome"] == "completed"

    def test_a_kind_that_needs_a_token_is_skipped_rather_than_spent(self, signed_in):
        response = signed_in.post(
            "/api/v1/schedules/builtin_rolling_backfill/run-now", headers=csrf(signed_in)
        )

        assert response.status_code == 202
        body = response.json()
        assert body["outcome"] == "skipped_needs_auth"
        assert body["job_id"] is None

    def test_the_history_limit_is_honoured(self, signed_in):
        for _ in range(3):
            signed_in.post(
                "/api/v1/schedules/builtin_maintenance/run-now", headers=csrf(signed_in)
            )

        response = signed_in.get("/api/v1/schedules/builtin_maintenance/runs?limit=2")

        assert response.status_code == 200
        assert len(response.json()) == 2

    def test_history_for_an_unknown_schedule_is_404(self, signed_in):
        response = signed_in.get("/api/v1/schedules/nope/runs")

        assert response.status_code == 404


class TestAuthAndCsrf:
    def test_every_route_needs_a_session(self, client):
        assert client.get("/api/v1/schedules").status_code == 401

    def test_a_mutation_without_the_csrf_header_is_rejected(self, signed_in):
        response = signed_in.patch(
            "/api/v1/schedules/builtin_maintenance", json={"enabled": False}
        )

        assert response.status_code == 403
