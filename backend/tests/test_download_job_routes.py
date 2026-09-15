"""W18: the download, job, task and coverage routes.

Every test runs a real application over a temporary data directory: real SQLite with real
migrations, a real DuckDB file, the real middleware stack, the real planner and the real job
service. Only two things are faked, and both for the same reason. The broker token gate is
overridden, because minting a real Fyers token would mean writing a credential into a test. And no
Fyers transport is installed at all, which is exactly what proves the plan route spends nothing: a
route that tried to reach the vendor here would fail loudly rather than quietly pass.

The assertions are on results, never on return codes alone. A refused commit is checked by reading
the `job` and `task` tables back, because a 409 with a job row behind it is the failure mode that
matters. A retry is checked by counting the child's task rows. A redaction is checked by searching
the whole response body for the string that must not be in it.

Every credential and every token-shaped value in this file is synthetic.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from expirymanager.api.deps import require_broker_token
from expirymanager.app import create_app
from expirymanager.db import sqlite as sqlite_module
from expirymanager.security.sessions import CSRF_COOKIE_NAME
from tests.test_pipeline_planner import EXPIRY, cover, register_underlying, seed_catalog

BASE_URL = "https://127.0.0.1:8000"
USERNAME = "operator"
PASSCODE = "correct-horse-battery-staple"

# res_id 2 is the one minute row of dim_resolution, the resolution every sheet below asks for.
RES_ID_ONE_MINUTE = 2

# Synthetic, and shaped like the thing that must never be returned: three dot separated base64
# segments is what the redaction filter recognises as a bearer token.
FAKE_TOKEN_IN_ERROR = (
    "upstream rejected eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c2lnbmF0dXJl"
)


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
def raw_client(app):
    """A client with the broker gate left real, for the needs_reauth test."""
    with TestClient(app, base_url=BASE_URL) as client:
        yield client


@pytest.fixture
def connected(app):
    """A client whose broker token gate is satisfied.

    The override replaces only the token check. The planner, the job service, the queue and the
    database are all the real ones.
    """
    app.dependency_overrides[require_broker_token] = lambda: "connected"
    with TestClient(app, base_url=BASE_URL) as client:
        yield client
    app.dependency_overrides.clear()


def csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    return {"X-CSRF-Token": token} if token else {}


def sign_in(client: TestClient) -> TestClient:
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": USERNAME, "password": PASSCODE},
        headers=csrf(client),
    )
    assert response.status_code == 200, response.text
    return client


def seed(app) -> list[int]:
    """A registered underlying and one discovered expiry with six option contracts."""
    state = app.state.services
    register_underlying(state.engine)
    return seed_catalog(state.duck)


SHEET = {
    "underlying_id": 1,
    "expiry_dates": [EXPIRY.isoformat()],
    "resolutions": ["1"],
    "instrument_class": "OPT",
    "option_types": ["CE", "PE"],
    "strike_scope": {"mode": "all"},
    "include_oi": True,
    "force_refresh": False,
}


def plan(client: TestClient, **overrides) -> dict:
    body = {**SHEET, **overrides}
    response = client.post("/api/v1/downloads/plan", json=body, headers=csrf(client))
    assert response.status_code == 200, response.text
    return response.json()


def start(client: TestClient, confirm: int | None, **overrides):
    body = {**SHEET, **overrides}
    if confirm is not None:
        body["confirm_requests"] = confirm
    return client.post("/api/v1/downloads", json=body, headers=csrf(client))


def job_count(app) -> int:
    with app.state.services.engine.connect() as connection:
        return int(connection.execute(text("SELECT count(*) FROM job")).scalar_one())


def task_count(app, job_id: str | None = None) -> int:
    sql = "SELECT count(*) FROM task"
    params: dict[str, object] = {}
    if job_id is not None:
        sql += " WHERE job_id = :job_id"
        params["job_id"] = job_id
    with app.state.services.engine.connect() as connection:
        return int(connection.execute(text(sql), params).scalar_one())


def set_status(app, job_id: str, status: str, *, reason: str | None = None) -> None:
    with app.state.services.engine.begin() as connection:
        connection.execute(
            text("UPDATE job SET status = :status, block_reason = :reason WHERE job_id = :id"),
            {"status": status, "reason": reason, "id": job_id},
        )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


class TestPlan:
    def test_it_prices_a_sheet_without_spending_a_single_fyers_request(self, app, connected):
        sign_in(connected)
        seed(app)
        used_before = app.state.services.governor.requests_used

        preview = plan(connected)

        assert preview["requests_estimated"] > 0
        assert preview["contracts_planned"] == 6
        assert preview["tasks_total"] > 0
        # The number that matters: the governor counted nothing, because nothing was sent.
        assert app.state.services.governor.requests_used == used_before == 0

    def test_the_preview_budget_line_is_the_planners_own_arithmetic(self, app, connected):
        sign_in(connected)
        seed(app)

        preview = plan(connected)

        # budget_after is the planner's single subtraction, returned untouched. A route that
        # recomputed it would eventually disagree with the planner by one skipped chunk.
        assert (
            preview["budget_after"]
            == preview["budget_remaining_today"] - preview["requests_estimated"]
        )
        assert preview["budget_used_today"] == 0
        assert preview["budget_allowance"] == preview["budget_remaining_today"]
        assert preview["exceeds_budget"] is False

    def test_a_covered_sheet_reports_the_chunks_it_skipped(self, app, connected):
        sign_in(connected)
        contracts = seed(app)
        for contract_id in contracts:
            cover(
                app.state.services.duck,
                contract_id,
                RES_ID_ONE_MINUTE,
                date(2024, 1, 1),
                EXPIRY,
            )

        preview = plan(connected)

        assert preview["chunks_skipped_covered"] > 0
        assert preview["requests_estimated"] < plan(connected, force_refresh=True)[
            "requests_estimated"
        ]

    def test_an_undiscovered_expiry_is_refused_with_the_remedy(self, app, connected):
        sign_in(connected)
        register_underlying(app.state.services.engine)
        seed_catalog(app.state.services.duck, discovered=False)

        response = connected.post(
            "/api/v1/downloads/plan", json=SHEET, headers=csrf(connected)
        )

        assert response.status_code == 400
        body = response.json()["error"]
        assert body["code"] == "no_contracts_discovered"
        assert body["detail"]["discovery_tasks"] == 1

    def test_planning_without_a_broker_token_raises_the_reconnect_banner(self, raw_client):
        sign_in(raw_client)

        response = raw_client.post(
            "/api/v1/downloads/plan", json=SHEET, headers=csrf(raw_client)
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "needs_reauth"


# ---------------------------------------------------------------------------
# Committing
# ---------------------------------------------------------------------------


class TestCommit:
    def test_a_confirmed_plan_writes_the_job_and_every_task(self, app, connected):
        sign_in(connected)
        seed(app)
        preview = plan(connected)

        response = start(connected, preview["requests_estimated"])

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "queued"
        assert body["est_requests"] == preview["requests_estimated"]
        assert job_count(app) == 1
        assert task_count(app, body["job_id"]) == body["total_tasks"] > 0

    def test_a_stale_estimate_is_refused_and_writes_nothing(self, app, connected):
        sign_in(connected)
        seed(app)
        preview = plan(connected)

        response = start(connected, preview["requests_estimated"] + 1)

        assert response.status_code == 409
        body = response.json()["error"]
        assert body["code"] == "plan_changed"
        assert body["detail"]["requests_estimated"] == preview["requests_estimated"]
        # The gate is the point: nothing was committed behind the refusal.
        assert job_count(app) == 0
        assert task_count(app) == 0

    def test_a_commit_with_no_confirmed_estimate_is_refused_and_writes_nothing(
        self, app, connected
    ):
        sign_in(connected)
        seed(app)

        response = start(connected, None)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "confirm_requests_required"
        assert job_count(app) == 0

    def test_a_sheet_that_is_already_downloaded_is_refused_rather_than_queued_empty(
        self, app, connected
    ):
        sign_in(connected)
        contracts = seed(app)
        for contract_id in contracts:
            cover(
                app.state.services.duck,
                contract_id,
                RES_ID_ONE_MINUTE,
                date(2020, 1, 1),
                EXPIRY,
            )
        preview = plan(connected)

        response = start(connected, preview["requests_estimated"])

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "nothing_to_download"
        assert job_count(app) == 0


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@pytest.fixture
def job(app, connected):
    """One committed job, and the client that committed it."""
    sign_in(connected)
    seed(app)
    preview = plan(connected)
    response = start(connected, preview["requests_estimated"])
    assert response.status_code == 202, response.text
    return response.json()


class TestJobList:
    def test_it_lists_the_committed_job_with_counts_read_from_the_task_table(
        self, app, connected, job
    ):
        response = connected.get("/api/v1/jobs")

        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        row = items[0]
        assert row["job_id"] == job["job_id"]
        assert row["pending_tasks"] == job["total_tasks"]
        assert row["open_tasks"] == job["total_tasks"]
        assert row["state_group"] == "active"
        assert row["can_cancel"] is True
        assert row["can_pause"] is True

    def test_a_status_filter_narrows_the_list(self, connected, job):
        assert connected.get("/api/v1/jobs?status=completed").json()["items"] == []
        assert len(connected.get("/api/v1/jobs?status=queued").json()["items"]) == 1

    def test_the_page_cursor_walks_every_job_exactly_once(self, app, connected, job):
        seed_more = seed_catalog(
            app.state.services.duck, expiry=date(2025, 4, 24), strikes=(23200,)
        )
        assert seed_more
        second = plan(connected, expiry_dates=[date(2025, 4, 24).isoformat()])
        assert (
            start(
                connected,
                second["requests_estimated"],
                expiry_dates=[date(2025, 4, 24).isoformat()],
            ).status_code
            == 202
        )

        seen: list[str] = []
        cursor = None
        for _ in range(5):
            url = "/api/v1/jobs?limit=1" + (f"&cursor={cursor}" if cursor else "")
            page = connected.get(url).json()
            seen.extend(item["job_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break

        assert len(seen) == 2
        assert len(set(seen)) == 2

    def test_a_cursor_from_another_collection_is_rejected(self, connected, job):
        # A task cursor, offered to the job list.
        response = connected.get("/api/v1/jobs?cursor=eyJ0YXNrX2lkIjoxfQ")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_cursor"


class TestJobDetail:
    def test_it_returns_the_live_task_breakdown_and_the_family(self, connected, job):
        response = connected.get(f"/api/v1/jobs/{job['job_id']}")

        assert response.status_code == 200
        body = response.json()
        assert sum(body["task_states"].values()) == job["total_tasks"]
        assert body["task_states"]["pending"] == job["total_tasks"]
        assert body["child_job_ids"] == []
        assert body["eta_seconds"] is not None and body["eta_seconds"] > 0

    def test_an_unknown_job_is_a_404(self, connected, job):
        response = connected.get("/api/v1/jobs/not-a-job")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


class TestParkedJobs:
    def test_a_job_awaiting_authentication_is_not_reported_as_failed(
        self, app, connected, job
    ):
        set_status(app, job["job_id"], "blocked_auth", reason="the broker session ended")

        body = connected.get(f"/api/v1/jobs/{job['job_id']}").json()

        assert body["status"] == "blocked_auth"
        assert body["state_group"] == "blocked"
        assert body["needs_reauth"] is True
        assert body["blocked_reason"] == "authentication"
        assert body["is_failed"] is False
        assert body["has_failures"] is False
        # The one action that actually clears it, and not the one that would duplicate work.
        assert body["can_resume"] is True
        assert body["can_retry_failed"] is False
        assert body["reason"] == "the broker session ended"

    def test_a_genuinely_failed_job_is_told_apart_from_a_parked_one(
        self, app, connected, job
    ):
        set_status(app, job["job_id"], "failed", reason=None)

        body = connected.get(f"/api/v1/jobs/{job['job_id']}").json()

        assert body["state_group"] == "terminal"
        assert body["is_failed"] is True
        assert body["needs_reauth"] is False
        assert body["can_resume"] is False
        assert body["can_cancel"] is False

    def test_a_budget_deferred_job_names_its_own_remedy(self, app, connected, job):
        set_status(app, job["job_id"], "deferred_budget")

        body = connected.get(f"/api/v1/jobs/{job['job_id']}").json()

        assert body["state_group"] == "deferred"
        assert body["blocked_reason"] == "budget"
        assert body["needs_reauth"] is False
        assert body["can_resume"] is True


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def fail_one_task(app, job_id: str) -> int:
    """Mark the first task failed, with a raw body path and a token-shaped error."""
    with app.state.services.engine.begin() as connection:
        task_id = int(
            connection.execute(
                text("SELECT task_id FROM task WHERE job_id = :id ORDER BY seq LIMIT 1"),
                {"id": job_id},
            ).scalar_one()
        )
        connection.execute(
            text(
                "UPDATE task SET state = 'failed', http_status = 500, fyers_code = -99,"
                " last_error_text = :error, raw_body_path = 'raw/2025/03/27/body.json',"
                " attempt = 4, finished_at = '2025-03-27T10:00:00.000000+00:00'"
                " WHERE task_id = :task_id"
            ),
            {"error": FAKE_TOKEN_IN_ERROR, "task_id": task_id},
        )
    return task_id


class TestTasks:
    def test_it_pages_the_task_rows_in_dispatch_order(self, connected, job):
        page = connected.get(f"/api/v1/jobs/{job['job_id']}/tasks?limit=2").json()

        assert len(page["items"]) == 2
        assert page["next_cursor"] is not None
        ids = [item["task_id"] for item in page["items"]]
        assert ids == sorted(ids)

        second = connected.get(
            f"/api/v1/jobs/{job['job_id']}/tasks?limit=2&cursor={page['next_cursor']}"
        ).json()
        assert all(item["task_id"] > ids[-1] for item in second["items"])

    def test_a_state_filter_finds_exactly_the_failed_task(self, app, connected, job):
        task_id = fail_one_task(app, job["job_id"])

        page = connected.get(f"/api/v1/jobs/{job['job_id']}/tasks?state=failed").json()

        assert [item["task_id"] for item in page["items"]] == [task_id]

    def test_an_unknown_state_is_refused_rather_than_returning_an_empty_page(
        self, connected, job
    ):
        response = connected.get(f"/api/v1/jobs/{job['job_id']}/tasks?state=exploded")

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_task_state"

    def test_the_raw_body_path_never_crosses_and_the_error_is_redacted(
        self, app, connected, job
    ):
        fail_one_task(app, job["job_id"])

        response = connected.get(f"/api/v1/jobs/{job['job_id']}/tasks?state=failed")
        body = response.text
        item = response.json()["items"][0]

        assert item["has_raw_body"] is True
        assert "raw_body_path" not in item
        assert "raw/2025/03/27/body.json" not in body
        assert "eyJhbGciOiJIUzI1NiJ9" not in body
        assert item["error_code"] == "-99"
        assert item["http_status"] == 500

    def test_tasks_of_an_unknown_job_are_a_404_not_an_empty_page(self, connected, job):
        response = connected.get("/api/v1/jobs/not-a-job/tasks")

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_pause_then_resume_moves_the_job_and_rewrites_no_task(self, app, connected, job):
        before = task_count(app, job["job_id"])

        paused = connected.post(
            f"/api/v1/jobs/{job['job_id']}/pause", headers=csrf(connected)
        )
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
        assert connected.get(f"/api/v1/jobs/{job['job_id']}").json()["state_group"] == "paused"

        resumed = connected.post(
            f"/api/v1/jobs/{job['job_id']}/resume", headers=csrf(connected)
        )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "queued"
        assert task_count(app, job["job_id"]) == before

    def test_cancel_marks_the_job_and_cancels_the_pending_tasks(self, app, connected, job):
        response = connected.post(
            f"/api/v1/jobs/{job['job_id']}/cancel", headers=csrf(connected)
        )

        assert response.status_code == 200
        with app.state.services.engine.connect() as connection:
            states = dict(
                connection.execute(
                    text(
                        "SELECT state, count(*) FROM task WHERE job_id = :id GROUP BY state"
                    ),
                    {"id": job["job_id"]},
                ).all()
            )
        assert states.get("pending", 0) == 0
        assert states.get("cancelled", 0) == job["total_tasks"]

    def test_pausing_a_finished_job_is_refused(self, app, connected, job):
        set_status(app, job["job_id"], "completed")

        response = connected.post(
            f"/api/v1/jobs/{job['job_id']}/pause", headers=csrf(connected)
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "job_finished"

    def test_a_lifecycle_command_without_the_csrf_header_is_rejected(self, connected, job):
        response = connected.post(f"/api/v1/jobs/{job['job_id']}/cancel")

        assert response.status_code == 403


class TestRetryFailed:
    def test_it_creates_a_child_holding_exactly_the_failed_tasks(self, app, connected, job):
        fail_one_task(app, job["job_id"])

        response = connected.post(
            f"/api/v1/jobs/{job['job_id']}/retry-failed", headers=csrf(connected)
        )

        assert response.status_code == 202
        body = response.json()
        assert body["parent_job_id"] == job["job_id"]
        assert body["total_tasks"] == 1
        assert task_count(app, body["job_id"]) == 1
        # The parent's record is never rewritten: its failure stays visible.
        assert task_count(app, job["job_id"]) == job["total_tasks"]
        parent = connected.get(f"/api/v1/jobs/{job['job_id']}").json()
        assert parent["failed_tasks"] == 1
        assert parent["child_job_ids"] == [body["job_id"]]

    def test_a_job_with_no_failures_is_refused(self, connected, job):
        response = connected.post(
            f"/api/v1/jobs/{job['job_id']}/retry-failed", headers=csrf(connected)
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "no_failed_tasks"


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


class TestCoverage:
    def test_the_grid_reports_what_is_held_and_what_is_missing(self, app, connected):
        sign_in(connected)
        contracts = seed(app)
        cover(
            app.state.services.duck,
            contracts[0],
            RES_ID_ONE_MINUTE,
            date(2025, 1, 2),
            date(2025, 1, 31),
            row_count=4200,
        )

        body = connected.get("/api/v1/coverage/grid?underlying_id=1").json()

        assert [item["res_id"] for item in body["resolutions"]] == [RES_ID_ONE_MINUTE]
        assert body["resolutions"][0]["fyers_code"] == "1"
        assert len(body["cells"]) == 1
        cell = body["cells"][0]
        assert cell["expiry_date"] == EXPIRY.isoformat()
        assert cell["contracts_with_data"] == 1
        assert cell["contracts_total"] == 6
        assert cell["contracts_missing"] == 5
        assert cell["chunks_ok"] == 1
        assert cell["rows"] == 4200
        assert cell["have_from"] == "2025-01-02"

    def test_the_grid_filters_by_resolution(self, app, connected):
        sign_in(connected)
        contracts = seed(app)
        cover(
            app.state.services.duck,
            contracts[0],
            RES_ID_ONE_MINUTE,
            date(2025, 1, 2),
            date(2025, 1, 31),
        )

        body = connected.get("/api/v1/coverage/grid?underlying_id=1&res_id=5").json()

        assert body["cells"] == []

    def test_the_gaps_route_reports_the_missing_days_between_two_chunks(
        self, app, connected
    ):
        sign_in(connected)
        contracts = seed(app)
        cover(
            app.state.services.duck,
            contracts[0],
            RES_ID_ONE_MINUTE,
            date(2025, 1, 2),
            date(2025, 1, 10),
        )
        cover(
            app.state.services.duck,
            contracts[0],
            RES_ID_ONE_MINUTE,
            date(2025, 1, 20),
            date(2025, 1, 31),
        )

        body = connected.get("/api/v1/coverage/gaps?underlying_id=1").json()

        assert len(body["items"]) == 1
        gap = body["items"][0]
        assert gap["contract_id"] == contracts[0]
        assert gap["held_to"] == "2025-01-10"
        assert gap["held_from"] == "2025-01-20"
        assert gap["gap_from"] == "2025-01-11"
        assert gap["gap_to"] == "2025-01-19"
        assert gap["missing_days"] == 9

    def test_touching_chunks_leave_no_gap(self, app, connected):
        sign_in(connected)
        contracts = seed(app)
        cover(
            app.state.services.duck,
            contracts[0],
            RES_ID_ONE_MINUTE,
            date(2025, 1, 2),
            date(2025, 1, 10),
        )
        cover(
            app.state.services.duck,
            contracts[0],
            RES_ID_ONE_MINUTE,
            date(2025, 1, 11),
            date(2025, 1, 31),
        )

        assert connected.get("/api/v1/coverage/gaps?underlying_id=1").json()["items"] == []


# ---------------------------------------------------------------------------
# The surface itself
# ---------------------------------------------------------------------------


class TestSurface:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/jobs",
            "/api/v1/jobs/any",
            "/api/v1/jobs/any/tasks",
            "/api/v1/coverage/grid?underlying_id=1",
            "/api/v1/coverage/gaps",
        ],
    )
    def test_every_read_requires_a_session(self, connected, path):
        response = connected.get(path)

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "not_authenticated"

    @pytest.mark.parametrize(
        "path",
        ["/api/v1/downloads/plan", "/api/v1/downloads", "/api/v1/jobs/any/cancel"],
    )
    def test_every_write_is_refused_before_it_is_read(self, connected, path):
        """No session means no synchroniser token, and CSRF rejects above the route.

        Asserted rather than assumed: the body must never be parsed, and the planner must never
        be reached, by a request that carries no session at all.
        """
        response = connected.post(path, json={})

        assert response.status_code == 403
        assert response.json()["error"]["code"] in {"csrf_invalid", "cross_origin_rejected"}

    def test_every_response_carries_no_store(self, connected, job):
        response = connected.get("/api/v1/jobs")

        assert response.headers["cache-control"] == "no-store"
