"""W17: the underlying, expiry and contract routes.

Every test runs a real application over a temporary data directory, with real SQLite, real
migrations, the real middleware stack and the real DuckDB store. The catalog rows are seeded
straight into DuckDB rather than mocked, because what is being asserted is a row count, an
allocated id, a coverage number and a UTC second, and a mock would satisfy every one of those for
free.

Nothing here touches the network. The one route that spends a Fyers request, the resolve probe,
is driven by a stub client that returns a parsed envelope, and every credential in this file is
synthetic.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from expirymanager.api.v1 import underlyings as underlyings_route
from expirymanager.app import create_app
from expirymanager.brokers.fyers import roots as roots_module
from expirymanager.brokers.fyers.client import FyersResponse
from expirymanager.brokers.fyers.symbology import parse_symbol
from expirymanager.db import sqlite as sqlite_module
from expirymanager.db.arrow import IST_OFFSET_SECONDS
from expirymanager.lifespan import SLOT_PIPELINE_SUPERVISOR
from expirymanager.pipeline.handlers.expiry_discovery import MAX_WINDOW_DAYS, last_served_day
from expirymanager.security.sessions import CSRF_COOKIE_NAME

BASE_URL = "https://127.0.0.1:8000"

USERNAME = "operator"
PASSCODE = "correct-horse-battery-staple"

NSE = 10
BSE = 12
MCX = 11
CM = 10
FO = 11
COM = 20

EXPIRY = date(2025, 3, 27)
RES_ID = 2
FIRST_TS = datetime(2025, 3, 26, 9, 15)
LAST_TS = datetime(2025, 3, 27, 15, 29)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
def pristine_registry():
    """Restore the process wide root registry after a test that registers into it.

    `default_registry()` is a module level singleton by design, so that a user added root is
    known to the symbol parser without threading a registry through every call. That also means a
    test which registers one leaks it into the next test unless it is put back.
    """
    registry = roots_module.default_registry()
    before = {entry.root: entry for entry in registry}
    yield registry
    for root in list(registry.known_roots()):
        if root not in before:
            registry.unregister(root)
    for root, entry in before.items():
        registry.register(entry, replace_existing=True)


@pytest.fixture
def client(app, pristine_registry):
    with TestClient(app, base_url=BASE_URL) as test_client:
        response = test_client.post(
            "/api/v1/auth/setup",
            json={"username": USERNAME, "password": PASSCODE},
            headers={"X-CSRF-Token": test_client.cookies.get(CSRF_COOKIE_NAME) or ""},
        )
        assert response.status_code == 200
        yield test_client


def csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    return {"X-CSRF-Token": token} if token else {}


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


_MASTER_COLUMNS = (
    "fytoken, symbol_ticker, exchange_code, segment_code, ex_instrument_type, under_symbol,"
    " under_fytoken, symbol_details, valid_from, valid_to, row_hash"
)


def cursor(app):
    return app.state.services.duck.cursor()


def seed_master(app) -> None:
    """A small, realistic slice of the symbol master, including the two awkward cases.

    NIFTYNXT50 is an ordinary index that resolves cleanly. 360ONE is the measured root this
    application cannot parse. CRUDEOIL is MCX, which the expired endpoints do not serve at all.
    """
    rows = [
        # Cash rows, the join target for under_fytoken.
        ("CASH-NIFTYNXT50", "NSE:NIFTYNXT50-INDEX", NSE, CM, 10, None, None, "INDEX"),
        ("CASH-360ONE", "NSE:360ONE-EQ", NSE, CM, 0, None, None, "360 ONE WAM"),
        ("CASH-CRUDEOIL", "MCX:CRUDEOIL-COM", MCX, CM, 0, None, None, "CRUDE OIL"),
    ]
    derivative = [
        ("FO-NXT-1", "NSE:NIFTYNXT5025D0430000CE", NSE, FO, 14, "NIFTYNXT50", "CASH-NIFTYNXT50"),
        ("FO-NXT-2", "NSE:NIFTYNXT5025D0430500CE", NSE, FO, 14, "NIFTYNXT50", "CASH-NIFTYNXT50"),
        ("FO-NXT-3", "NSE:NIFTYNXT5025D0430000PE", NSE, FO, 14, "NIFTYNXT50", "CASH-NIFTYNXT50"),
        ("FO-360-1", "NSE:360ONE25DEC1000CE", NSE, FO, 14, "360ONE", "CASH-360ONE"),
        ("FO-CRUDE-1", "MCX:CRUDEOIL25DEC6000CE", MCX, COM, 14, "CRUDEOIL", "CASH-CRUDEOIL"),
    ]
    cur = cursor(app)
    try:
        for fytoken, ticker, exchange, segment, kind, under, under_token, details in rows:
            cur.execute(
                f"INSERT INTO dim_instrument_master ({_MASTER_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                [
                    fytoken, ticker, exchange, segment, kind, under, under_token, details,
                    date(2026, 9, 1), fytoken,
                ],
            )
        for fytoken, ticker, exchange, segment, kind, under, under_token in derivative:
            cur.execute(
                f"INSERT INTO dim_instrument_master ({_MASTER_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                [
                    fytoken, ticker, exchange, segment, kind, under, under_token, None,
                    date(2026, 9, 1), fytoken,
                ],
            )
    finally:
        cur.close()


def seed_nifty_catalog(app, *, strikes=(22900, 23000, 23100)) -> dict[str, int]:
    """One mirrored underlying, one expiry, some options, bounds and a coverage ledger."""
    cur = cursor(app)
    contract_ids: dict[str, int] = {}
    try:
        # The lifespan mirrors the SQLite registry into dim_underlying at startup, and that write
        # also creates the reserved SPOT row in dim_contract, so both already exist by the time a
        # test seeds its own richer versions. Displacing them first is exactly what the production
        # upsert does.
        cur.execute("DELETE FROM dim_contract WHERE contract_id = 1")
        cur.execute("DELETE FROM dim_underlying WHERE underlying_id = 1")
        cur.execute(
            "INSERT INTO dim_underlying (underlying_id, fyers_symbol, root, exchange,"
            " exchange_code, segment, segment_code, instrument_kind, display_name,"
            " spot_contract_id, underlying_fytoken, data_from, first_expiry, last_expiry,"
            " expiry_count, contract_count, is_active, synced_at)"
            " VALUES (1, 'NSE:NIFTY50-INDEX', 'NIFTY', 'NSE', 10, 'CM', 10, 'INDEX',"
            " 'Nifty 50', 1, NULL, ?, ?, ?, 1, ?, TRUE, ?)",
            [date(2022, 1, 3), EXPIRY, EXPIRY, len(strikes) * 2, datetime(2026, 9, 1, 12, 0)],
        )
        cur.execute(
            "INSERT INTO dim_expiry (expiry_id, underlying_id, expiry_date, has_futures,"
            " has_options, futures_count, options_count, contract_count, min_strike, max_strike,"
            " strike_step, contract_id_lo, contract_id_hi, expiry_cycle_derived,"
            " expiry_cycle_source, is_last_of_month, expiry_dow, discovered_at,"
            " contracts_discovered_at)"
            " VALUES (1, 1, ?, FALSE, TRUE, 0, ?, ?, ?, ?, 100, 1024, ?, 'M', 'derived', TRUE,"
            " ?, ?, ?)",
            [
                EXPIRY,
                len(strikes) * 2,
                len(strikes) * 2,
                min(strikes),
                max(strikes),
                1024 + len(strikes) * 2 - 1,
                EXPIRY.isoweekday(),
                datetime(2025, 3, 28, 18, 15, 4),
                datetime(2025, 3, 28, 18, 15, 4),
            ],
        )
        # Spot bars, so the underlying listing has something to report.
        cur.execute(
            "INSERT INTO dim_contract (contract_id, underlying_id, expiry_id, fyers_symbol,"
            " kind, instrument_class, exchange, exchange_code, segment, segment_code, root,"
            " source_endpoint, parse_method, parse_confidence, first_seen_at, last_seen_at)"
            " VALUES (1, 1, NULL, 'NSE:NIFTY50-INDEX', 'SPOT', 'INDEX', 'NSE', 10, 'CM', 10,"
            " 'NIFTY', 'registry', 'registry', 'exact', ?, ?)",
            [datetime(2026, 9, 1, 12, 0), datetime(2026, 9, 1, 12, 0)],
        )
        cur.execute(
            "INSERT INTO contract_bounds (contract_id, res_id, first_ts, last_ts, row_count,"
            " updated_at) VALUES (1, ?, ?, ?, 512, ?)",
            [RES_ID, FIRST_TS, LAST_TS, datetime(2026, 9, 1, 12, 0)],
        )

        contract_id = 1024
        for strike in strikes:
            for right in ("CE", "PE"):
                symbol = f"NSE:NIFTY25MAR{strike}{right}"
                contract_ids[symbol] = contract_id
                cur.execute(
                    "INSERT INTO dim_contract (contract_id, underlying_id, expiry_id,"
                    " fyers_symbol, kind, instrument_class, exchange, exchange_code, segment,"
                    " segment_code, root, expiry_date, strike, strike_raw, option_type,"
                    " lot_size, tick_size, fytoken, symbol_expiry_encoding, expiry_cycle,"
                    " source_endpoint, parse_method, parse_confidence, sealed_at,"
                    " first_seen_at, last_seen_at)"
                    " VALUES (?, 1, 1, ?, 'OPT', 'OPTIDX', 'NSE', 10, 'FO', 11, 'NIFTY', ?,"
                    " ?, ?, ?, 75, 0.05, ?, 'MONTHLY_CODED', 'M', 'expired_contracts',"
                    " 'regex', 'exact', ?, ?, ?)",
                    [
                        contract_id,
                        symbol,
                        EXPIRY,
                        strike,
                        str(strike),
                        right,
                        f"1011{strike}{right}",
                        datetime(2025, 3, 28, 18, 41, 12) if right == "CE" else None,
                        datetime(2025, 3, 28, 18, 0, 0),
                        datetime(2025, 3, 28, 18, 0, 0),
                    ],
                )
                # Only the calls carry data, which is what makes has_data and the coverage
                # counts assertable rather than uniform.
                if right == "CE":
                    cur.execute(
                        "INSERT INTO contract_bounds (contract_id, res_id, first_ts, last_ts,"
                        " row_count, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                        [contract_id, RES_ID, FIRST_TS, LAST_TS, 375, datetime(2026, 9, 1)],
                    )
                    cur.execute(
                        "INSERT INTO candle_coverage (contract_id, res_id, range_from, range_to,"
                        " status, row_count, first_ts, last_ts, include_oi, columns_json,"
                        " task_id, fetched_at) VALUES (?, ?, ?, ?, 'ok', 375, ?, ?, TRUE, '[]',"
                        " 1, ?)",
                        [
                            contract_id, RES_ID, date(2025, 3, 26), EXPIRY, FIRST_TS, LAST_TS,
                            datetime(2026, 9, 1),
                        ],
                    )
                else:
                    cur.execute(
                        "INSERT INTO candle_coverage (contract_id, res_id, range_from, range_to,"
                        " status, row_count, include_oi, columns_json, task_id, fetched_at)"
                        " VALUES (?, ?, ?, ?, 'error', 0, TRUE, '[]', 2, ?)",
                        [contract_id, RES_ID, date(2025, 3, 26), EXPIRY, datetime(2026, 9, 1)],
                    )
                contract_id += 1
    finally:
        cur.close()
    return contract_ids


class StubBroker:
    def __init__(self, valid: bool = True) -> None:
        self._valid = valid
        self.record = None

    def has_valid_token(self) -> bool:
        return self._valid


class StubClient:
    """Answers one expiry-dates call with a parsed envelope. Nothing leaves the process."""

    def __init__(self, *, symbol_echo: str = "NIFTYNXT50", expiries=("2025-03-27",)) -> None:
        self.symbol_echo = symbol_echo
        self.expiries = list(expiries)
        self.calls: list[dict] = []

    async def request_standard(self, endpoint, *, params=None, json_body=None):
        self.calls.append(dict(params or {}))
        return FyersResponse(
            status="ok",
            code=200,
            message="",
            payload={
                "data": {
                    "symbol": self.symbol_echo,
                    "expiry_dates": {"futures": [], "options": self.expiries},
                }
            },
            http_status=200,
            response_bytes=0,
            latency_ms=1,
            payload_sha256="",
            endpoint=endpoint.name,
        )


class RecordingSupervisor:
    def __init__(self) -> None:
        self.notified: list[str] = []

    def notify(self, job_id=None) -> None:
        self.notified.append(job_id)


# ---------------------------------------------------------------------------
# Section 3, the underlying listing
# ---------------------------------------------------------------------------


class TestListUnderlyings:
    def test_it_returns_the_four_seeded_registry_rows(self, client):
        response = client.get("/api/v1/underlyings")

        assert response.status_code == 200
        body = response.json()
        assert [item["fyers_symbol"] for item in body] == [
            "NSE:NIFTY50-INDEX",
            "NSE:NIFTYBANK-INDEX",
            "BSE:SENSEX-INDEX",
            "NSE:RELIANCE-EQ",
        ]
        first = body[0]
        assert first["root"] == "NIFTY"
        assert first["default_resolutions"] == ["1", "5", "15", "60"]
        assert first["is_builtin"] is True
        assert first["spot_contract_id"] == 1

    def test_a_fresh_install_has_every_registry_row_mirrored(self, client):
        # The lifespan mirrors the registry into dim_underlying at startup, so the state this
        # guards against should not exist on a healthy install.
        body = client.get("/api/v1/underlyings").json()
        assert body
        assert all(item["mirrored"] is True for item in body)

    def test_a_registry_row_with_no_mirror_reports_mirrored_false(self, client, app):
        # The failure this guards is the one that actually happened: registry rows present,
        # dim_underlying empty, and nine joins answering empty with no error anywhere. Startup now
        # prevents it, so the drift has to be constructed to prove the reporting still catches it.
        cur = cursor(app)
        try:
            cur.execute("DELETE FROM dim_underlying")
        finally:
            cur.close()

        body = client.get("/api/v1/underlyings").json()
        assert all(item["mirrored"] is False for item in body)
        assert all(item["spot_bars"] == 0 for item in body)

    def test_the_rollups_and_spot_bars_come_from_the_mirror(self, client, app):
        seed_nifty_catalog(app)

        body = client.get("/api/v1/underlyings").json()
        nifty = next(item for item in body if item["underlying_id"] == 1)

        assert nifty["mirrored"] is True
        assert nifty["spot_bars"] == 512
        assert nifty["spot_last_ts"] == LAST_TS.isoformat()
        assert nifty["contract_count"] == 6
        assert nifty["first_expiry"] == EXPIRY.isoformat()

    def test_active_only_filters_the_registry(self, client, app):
        with app.state.services.engine.begin() as connection:
            connection.execute(
                text("UPDATE underlying_registry SET is_active = 0 WHERE underlying_id = 4")
            )

        assert len(client.get("/api/v1/underlyings").json()) == 4
        assert len(client.get("/api/v1/underlyings?active_only=true").json()) == 3

    def test_it_needs_a_session(self, app):
        with TestClient(app, base_url=BASE_URL) as anonymous:
            assert anonymous.get("/api/v1/underlyings").status_code == 401


# ---------------------------------------------------------------------------
# Section 3, resolve
# ---------------------------------------------------------------------------


class TestResolve:
    def test_it_resolves_a_root_to_its_cash_ticker_through_under_fytoken(self, client, app):
        seed_master(app)
        app.state.services.token_broker = StubBroker(valid=False)

        response = client.post(
            "/api/v1/underlyings/resolve", json={"query": "NIFTYNXT50"}, headers=csrf(client)
        )

        assert response.status_code == 200
        body = response.json()
        candidate = body["candidates"][0]
        assert candidate["root"] == "NIFTYNXT50"
        # Nothing in the derivative symbol says this. Only the under_fytoken join knows it.
        assert candidate["fyers_symbol"] == "NSE:NIFTYNXT50-INDEX"
        assert candidate["under_fytoken"] == "CASH-NIFTYNXT50"
        assert candidate["fo_contract_count"] == 3
        assert candidate["instrument_kind"] == "INDEX"

    def test_a_cash_ticker_fragment_finds_the_same_root(self, client, app):
        seed_master(app)
        app.state.services.token_broker = StubBroker(valid=False)

        body = client.post(
            "/api/v1/underlyings/resolve",
            json={"query": "NSE:NIFTYNXT50-INDEX"},
            headers=csrf(client),
        ).json()

        assert [item["root"] for item in body["candidates"]] == ["NIFTYNXT50"]

    def test_mcx_is_refused_with_the_measured_reason_and_spends_nothing(self, client, app):
        seed_master(app)
        stub = StubClient()
        app.state.services.fyers_client = stub
        app.state.services.token_broker = StubBroker(valid=True)

        response = client.post(
            "/api/v1/underlyings/resolve", json={"query": "MCX:CRUDEOIL-COM"}, headers=csrf(client)
        )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "mcx_not_supported"
        assert "422" in error["message"]
        assert stub.calls == []

    def test_an_mcx_root_is_never_offered_even_when_the_master_carries_it(self, client, app):
        seed_master(app)
        app.state.services.token_broker = StubBroker(valid=False)

        response = client.post(
            "/api/v1/underlyings/resolve", json={"query": "CRUDEOIL"}, headers=csrf(client)
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "no_candidates"

    def test_a_digit_leading_root_is_reported_rather_than_accepted(self, client, app):
        seed_master(app)
        app.state.services.token_broker = StubBroker(valid=False)

        response = client.post(
            "/api/v1/underlyings/resolve", json={"query": "360ONE"}, headers=csrf(client)
        )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "unparseable_root"
        assert error["detail"]["rejected"][0]["root"] == "360ONE"
        assert "360ONE" in error["message"]

    def test_nothing_matching_is_a_404(self, client, app):
        seed_master(app)
        app.state.services.token_broker = StubBroker(valid=False)

        response = client.post(
            "/api/v1/underlyings/resolve", json={"query": "NOSUCHROOT"}, headers=csrf(client)
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "no_candidates"

    def test_the_probe_spends_exactly_one_request_and_returns_the_root_echo(self, client, app):
        seed_master(app)
        stub = StubClient(symbol_echo="NIFTYNXT50", expiries=["2025-03-27", "2025-04-24"])
        app.state.services.fyers_client = stub
        app.state.services.token_broker = StubBroker(valid=True)

        body = client.post(
            "/api/v1/underlyings/resolve", json={"query": "NIFTYNXT50"}, headers=csrf(client)
        ).json()

        assert len(stub.calls) == 1
        assert stub.calls[0]["symbol"] == "NSE:NIFTYNXT50-INDEX"
        probe = body["probe"]
        assert probe["attempted"] is True
        assert probe["root_echo"] == "NIFTYNXT50"
        assert probe["expiry_count"] == 2

    def test_the_probe_window_never_exceeds_the_measured_ceiling(self, client, app):
        seed_master(app)
        stub = StubClient()
        app.state.services.fyers_client = stub
        app.state.services.token_broker = StubBroker(valid=True)

        client.post(
            "/api/v1/underlyings/resolve", json={"query": "NIFTYNXT50"}, headers=csrf(client)
        )

        sent = stub.calls[0]
        start = date.fromisoformat(sent["range_from"])
        end = date.fromisoformat(sent["range_to"])
        assert (end - start).days + 1 <= MAX_WINDOW_DAYS
        assert end <= last_served_day(datetime.now().date())

    def test_without_a_token_the_search_still_answers_and_the_probe_says_so(self, client, app):
        seed_master(app)
        stub = StubClient()
        app.state.services.fyers_client = stub
        app.state.services.token_broker = StubBroker(valid=False)

        response = client.post(
            "/api/v1/underlyings/resolve", json={"query": "NIFTYNXT50"}, headers=csrf(client)
        )

        assert response.status_code == 200
        body = response.json()
        assert body["candidates"]
        assert body["probe"]["attempted"] is False
        assert body["probe"]["reason"] == "needs_reauth"
        assert stub.calls == []

    def test_an_already_registered_symbol_is_flagged(self, client, app):
        seed_master(app)
        app.state.services.token_broker = StubBroker(valid=False)
        with app.state.services.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE underlying_registry SET fyers_symbol = 'NSE:NIFTYNXT50-INDEX'"
                    " WHERE underlying_id = 4"
                )
            )

        body = client.post(
            "/api/v1/underlyings/resolve", json={"query": "NIFTYNXT50"}, headers=csrf(client)
        ).json()

        assert body["candidates"][0]["already_registered"] is True


# ---------------------------------------------------------------------------
# Section 3, register, patch and delete
# ---------------------------------------------------------------------------


class TestCreateUnderlying:
    def test_it_writes_the_registry_row_and_its_duckdb_mirror_together(self, client, app):
        seed_master(app)
        app.state.services.token_broker = StubBroker(valid=False)

        response = client.post(
            "/api/v1/underlyings",
            json={"fyers_symbol": "NSE:NIFTYNXT50-INDEX", "default_resolutions": ["1", "5"]},
            headers=csrf(client),
        )

        assert response.status_code == 201
        body = response.json()
        assert body["root"] == "NIFTYNXT50"
        assert body["underlying_id"] == 5
        # Spot ids come from the reserved 1..999 block. The four builtin underlyings are mirrored
        # at startup and hold the first four, so the fifth underlying gets the fifth id. Asserting
        # the exact number only worked while dim_contract started empty.
        assert 1 <= body["spot_contract_id"] <= 999
        assert body["spot_contract_id"] == 5
        assert body["default_resolutions"] == ["1", "5"]
        assert body["mirrored"] is True

        with app.state.services.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT root, spot_contract_id, is_builtin, option_life_days"
                    " FROM underlying_registry WHERE underlying_id = 5"
                )
            ).first()
        assert row[0] == "NIFTYNXT50"
        assert row[1] == 5  # the fifth reserved spot id, see the note above
        assert row[2] == 0
        assert row[3] == 200

        cur = cursor(app)
        try:
            mirror = cur.execute(
                "SELECT root, spot_contract_id, underlying_fytoken FROM dim_underlying"
                " WHERE underlying_id = 5"
            ).fetchone()
            spot = cur.execute(
                "SELECT fyers_symbol, kind FROM dim_contract WHERE contract_id = ?", [row[1]]
            ).fetchone()
        finally:
            cur.close()
        assert mirror == ("NIFTYNXT50", 5, "CASH-NIFTYNXT50")
        assert spot == ("NSE:NIFTYNXT50-INDEX", "SPOT")

    def test_the_new_root_reaches_the_symbol_parser(self, client, app):
        seed_master(app)
        app.state.services.token_broker = StubBroker(valid=False)
        symbol = "NSE:NIFTYNXT5025D0430000CE"
        assert roots_module.default_registry().get("NIFTYNXT50") is None
        # An unregistered root parses, but the parser cannot say what kind of derivative it is,
        # because only the registry knows that NIFTYNXT50 is an index and not a stock.
        assert parse_symbol(symbol).instrument_class is None

        created = client.post(
            "/api/v1/underlyings",
            json={"fyers_symbol": "NSE:NIFTYNXT50-INDEX"},
            headers=csrf(client),
        ).json()

        # The registration is only complete when the parser can read a contract filed under the
        # new root. A registry row on its own would have every discovered symbol classified as an
        # equity option, which is the wrong instrument class on every row of the new underlying.
        assert parse_symbol(symbol).instrument_class == "OPTIDX"
        entry = roots_module.default_registry().get("NIFTYNXT50")
        assert entry is not None
        assert entry.fyers_symbol == "NSE:NIFTYNXT50-INDEX"
        assert entry.underlying_id == created["underlying_id"]
        assert entry.spot_contract_id == created["spot_contract_id"]

    def test_a_second_registration_of_the_same_symbol_is_a_conflict(self, client, app):
        seed_master(app)
        app.state.services.token_broker = StubBroker(valid=False)
        body = {"fyers_symbol": "NSE:NIFTYNXT50-INDEX"}
        assert client.post("/api/v1/underlyings", json=body, headers=csrf(client)).status_code == 201

        response = client.post("/api/v1/underlyings", json=body, headers=csrf(client))

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "already_exists"
        with app.state.services.engine.connect() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM underlying_registry")
            ).scalar()
        assert count == 5

    def test_an_mcx_symbol_is_refused_with_the_measured_reason(self, client, app):
        seed_master(app)

        response = client.post(
            "/api/v1/underlyings",
            json={"fyers_symbol": "MCX:CRUDEOIL-COM"},
            headers=csrf(client),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "mcx_not_supported"
        assert "MCX" in response.json()["error"]["message"]

    def test_a_symbol_the_master_does_not_know_is_refused_before_anything_is_written(
        self, client, app
    ):
        seed_master(app)

        response = client.post(
            "/api/v1/underlyings",
            json={"fyers_symbol": "NSE:MADEUP-EQ"},
            headers=csrf(client),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unresolved_root"
        cur = cursor(app)
        try:
            # The four builtin underlyings are mirrored at startup, so what this asserts is that
            # the refused create added nothing, not that the table is empty.
            assert cur.execute("SELECT count(*) FROM dim_underlying").fetchone()[0] == 4
        finally:
            cur.close()

    def test_a_digit_leading_root_cannot_be_registered(self, client, app):
        seed_master(app)

        response = client.post(
            "/api/v1/underlyings", json={"fyers_symbol": "NSE:360ONE-EQ"}, headers=csrf(client)
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unparseable_root"
        assert response.json()["error"]["detail"]["root"] == "360ONE"


class TestPatchUnderlying:
    def test_it_updates_the_registry_and_refreshes_the_mirror(self, client, app):
        seed_nifty_catalog(app)

        response = client.patch(
            "/api/v1/underlyings/1",
            json={"display_name": "Nifty Fifty", "include_oi": False},
            headers=csrf(client),
        )

        assert response.status_code == 200
        assert response.json()["display_name"] == "Nifty Fifty"
        assert response.json()["include_oi"] is False
        cur = cursor(app)
        try:
            mirrored = cur.execute(
                "SELECT display_name FROM dim_underlying WHERE underlying_id = 1"
            ).fetchone()[0]
        finally:
            cur.close()
        assert mirrored == "Nifty Fifty"

    def test_deactivating_lands_in_both_stores(self, client, app):
        seed_nifty_catalog(app)

        client.patch("/api/v1/underlyings/1", json={"is_active": False}, headers=csrf(client))

        cur = cursor(app)
        try:
            active = cur.execute(
                "SELECT is_active FROM dim_underlying WHERE underlying_id = 1"
            ).fetchone()[0]
        finally:
            cur.close()
        assert active is False
        listed = client.get("/api/v1/underlyings?active_only=true").json()
        assert 1 not in [item["underlying_id"] for item in listed]

    def test_a_patch_creates_the_mirror_when_it_is_missing(self, client, app):
        # The registry row is seeded by migration 0004 and has never been mirrored. This is the
        # drift that made nine read paths answer empty on a fresh install.
        # Startup mirrors every registry row, so the missing mirror this repairs has to be made.
        cur = cursor(app)
        try:
            cur.execute("DELETE FROM dim_underlying WHERE underlying_id = 2")
        finally:
            cur.close()
        assert client.get("/api/v1/underlyings").json()[1]["mirrored"] is False

        response = client.patch(
            "/api/v1/underlyings/2", json={"display_name": "Bank Nifty"}, headers=csrf(client)
        )

        assert response.status_code == 200
        assert response.json()["mirrored"] is True
        cur = cursor(app)
        try:
            row = cur.execute(
                "SELECT display_name, spot_contract_id FROM dim_underlying"
                " WHERE underlying_id = 2"
            ).fetchone()
            spot = cur.execute(
                "SELECT fyers_symbol, kind FROM dim_contract WHERE contract_id = 2"
            ).fetchone()
        finally:
            cur.close()
        assert row == ("Bank Nifty", 2)
        assert spot == ("NSE:NIFTYBANK-INDEX", "SPOT")

    def test_an_unknown_id_is_a_404(self, client):
        response = client.patch(
            "/api/v1/underlyings/99", json={"display_name": "x"}, headers=csrf(client)
        )
        assert response.status_code == 404

    def test_an_empty_patch_is_rejected(self, client):
        response = client.patch("/api/v1/underlyings/1", json={}, headers=csrf(client))
        assert response.status_code == 422


class TestDeleteUnderlying:
    def test_a_builtin_cannot_be_deleted(self, client, app):
        response = client.delete("/api/v1/underlyings/1", headers=csrf(client))

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "builtin_underlying"
        with app.state.services.engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM underlying_registry WHERE underlying_id = 1")
            ).scalar() == 1

    def test_a_live_job_blocks_the_delete(self, client, app):
        seed_master(app)
        app.state.services.token_broker = StubBroker(valid=False)
        created = client.post(
            "/api/v1/underlyings",
            json={"fyers_symbol": "NSE:NIFTYNXT50-INDEX"},
            headers=csrf(client),
        ).json()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with app.state.services.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO job (job_id, kind, status, params_json, created_at)"
                    " VALUES ('j1', 'expiry_discovery', 'running', '{}', :now)"
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO task (job_id, seq, kind, state, underlying_id, not_before,"
                    " created_at) VALUES ('j1', 0, 'expiry_dates', 'pending', :underlying_id,"
                    " :now, :now)"
                ),
                {"underlying_id": created["underlying_id"], "now": now},
            )

        response = client.delete(
            f"/api/v1/underlyings/{created['underlying_id']}", headers=csrf(client)
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "has_running_jobs"

    def test_it_removes_both_stores_and_forgets_the_root(self, client, app):
        seed_master(app)
        app.state.services.token_broker = StubBroker(valid=False)
        created = client.post(
            "/api/v1/underlyings",
            json={"fyers_symbol": "NSE:NIFTYNXT50-INDEX"},
            headers=csrf(client),
        ).json()

        response = client.delete(
            f"/api/v1/underlyings/{created['underlying_id']}", headers=csrf(client)
        )

        assert response.status_code == 204
        with app.state.services.engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM underlying_registry WHERE underlying_id = 5")
            ).scalar() == 0
        cur = cursor(app)
        try:
            assert cur.execute(
                "SELECT count(*) FROM dim_underlying WHERE underlying_id = 5"
            ).fetchone()[0] == 0
        finally:
            cur.close()
        assert roots_module.default_registry().get("NIFTYNXT50") is None


# ---------------------------------------------------------------------------
# Section 4, expiries
# ---------------------------------------------------------------------------


class TestExpiryListing:
    def test_coverage_is_read_from_the_ledger_and_agrees_with_it(self, client, app):
        seed_nifty_catalog(app)

        response = client.get("/api/v1/underlyings/1/expiries")

        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        row = items[0]
        assert row["expiry_date"] == EXPIRY.isoformat()
        assert row["contract_count"] == 6
        assert row["expiry_cycle"] == "M"
        assert row["min_strike"] == 22900.0
        assert row["strike_step"] == 100.0
        coverage = row["coverage"]
        # Three calls carry bounds and an ok chunk; three puts carry an error chunk and no bounds.
        assert coverage["contracts_with_data"] == 3
        assert coverage["contracts_without_data"] == 3
        assert coverage["contracts_sealed"] == 3
        assert coverage["chunks_ok"] == 3
        assert coverage["chunks_error"] == 3
        assert coverage["chunks_empty"] == 0
        assert coverage["rows"] == 1125

    def test_the_numbers_match_a_direct_read_of_the_coverage_tables(self, client, app):
        seed_nifty_catalog(app)

        coverage = client.get("/api/v1/underlyings/1/expiries").json()["items"][0]["coverage"]

        cur = cursor(app)
        try:
            ledger_rows = cur.execute(
                "SELECT coalesce(sum(row_count), 0) FROM candle_coverage cov"
                " JOIN dim_contract c USING (contract_id) WHERE c.expiry_id = 1"
            ).fetchone()[0]
            held = cur.execute(
                "SELECT count(DISTINCT contract_id) FROM contract_bounds b"
                " JOIN dim_contract c USING (contract_id)"
                " WHERE c.expiry_id = 1 AND b.row_count > 0"
            ).fetchone()[0]
        finally:
            cur.close()
        assert coverage["rows"] == ledger_rows
        assert coverage["contracts_with_data"] == held

    def test_the_date_range_filters(self, client, app):
        seed_nifty_catalog(app)

        assert client.get("/api/v1/underlyings/1/expiries?from=2025-04-01").json()["items"] == []
        assert len(client.get("/api/v1/underlyings/1/expiries?to=2025-03-27").json()["items"]) == 1

    def test_paging_uses_an_opaque_cursor(self, client, app):
        seed_nifty_catalog(app)
        cur = cursor(app)
        try:
            cur.execute(
                "INSERT INTO dim_expiry (expiry_id, underlying_id, expiry_date, has_options,"
                " contract_count, expiry_cycle_source, discovered_at)"
                " VALUES (2, 1, ?, TRUE, 0, 'derived', ?)",
                [date(2025, 4, 24), datetime(2025, 4, 25, 18, 0)],
            )
        finally:
            cur.close()

        first = client.get("/api/v1/underlyings/1/expiries?limit=1").json()
        assert len(first["items"]) == 1
        assert first["next_cursor"]
        assert EXPIRY.isoformat() not in first["next_cursor"]

        second = client.get(
            f"/api/v1/underlyings/1/expiries?limit=1&cursor={first['next_cursor']}"
        ).json()
        assert second["items"][0]["expiry_date"] == "2025-04-24"
        assert second["next_cursor"] is None

    def test_a_foreign_cursor_is_rejected(self, client, app):
        seed_nifty_catalog(app)
        from expirymanager.api.schemas.common import encode_cursor

        response = client.get(
            "/api/v1/underlyings/1/expiries?cursor=" + encode_cursor({"contract_id": 5})
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_cursor"

    def test_an_unknown_underlying_is_a_404(self, client):
        assert client.get("/api/v1/underlyings/99/expiries").status_code == 404

    def test_the_res_id_filter_narrows_the_coverage_rollup(self, client, app):
        seed_nifty_catalog(app)

        held = client.get(f"/api/v1/underlyings/1/expiries?res_id={RES_ID}").json()["items"][0]
        other = client.get("/api/v1/underlyings/1/expiries?res_id=8").json()["items"][0]

        assert held["coverage"]["rows"] == 1125
        assert other["coverage"]["rows"] == 0
        assert other["coverage"]["contracts_with_data"] == 0


class TestDiscoverExpiries:
    def _arm(self, app, *, valid: bool = True) -> RecordingSupervisor:
        supervisor = RecordingSupervisor()
        app.state.services.components[SLOT_PIPELINE_SUPERVISOR] = supervisor
        app.state.services.token_broker = StubBroker(valid=valid)
        return supervisor

    def test_it_creates_one_task_per_366_day_window(self, client, app):
        supervisor = self._arm(app)
        end = last_served_day(datetime.now().date())
        start = end - timedelta(days=800)

        response = client.post(
            "/api/v1/underlyings/1/expiries/discover",
            json={"range_from": start.isoformat(), "range_to": end.isoformat()},
            headers=csrf(client),
        )

        assert response.status_code == 202
        body = response.json()
        assert body["total_tasks"] == 3
        assert body["est_requests"] == 3
        assert len(body["windows"]) == 3
        for window in body["windows"]:
            span = (
                date.fromisoformat(window["range_to"])
                - date.fromisoformat(window["range_from"])
            ).days + 1
            assert span <= MAX_WINDOW_DAYS

        with app.state.services.engine.connect() as connection:
            job = connection.execute(
                text("SELECT kind, status, total_tasks, est_requests FROM job WHERE job_id = :id"),
                {"id": body["job_id"]},
            ).first()
            tasks = connection.execute(
                text(
                    "SELECT kind, state, underlying_id, fyers_symbol, range_from, range_to,"
                    " request_params_json FROM task WHERE job_id = :id ORDER BY seq"
                ),
                {"id": body["job_id"]},
            ).all()
        assert job == ("expiry_discovery", "queued", 3, 3)
        assert len(tasks) == 3
        assert {row[0] for row in tasks} == {"expiry_dates"}
        assert {row[1] for row in tasks} == {"pending"}
        assert {row[3] for row in tasks} == {"NSE:NIFTY50-INDEX"}
        assert json.loads(tasks[0][6])["symbol"] == "NSE:NIFTY50-INDEX"
        # The job is useless if nobody is told about it. This is the wiring, asserted.
        assert supervisor.notified == [body["job_id"]]

    def test_a_range_ending_in_the_future_is_clamped_to_the_last_served_day(self, client, app):
        self._arm(app)
        tomorrow = datetime.now().date() + timedelta(days=30)

        body = client.post(
            "/api/v1/underlyings/1/expiries/discover",
            json={"range_from": "2025-01-01", "range_to": tomorrow.isoformat()},
            headers=csrf(client),
        ).json()

        latest = last_served_day(datetime.now().date())
        assert body["clamped"] is True
        assert body["range_to"] == latest.isoformat()
        assert date.fromisoformat(body["windows"][0]["range_to"]) == latest

    def test_a_range_before_the_exchange_floor_is_refused(self, client, app):
        self._arm(app)

        response = client.post(
            "/api/v1/underlyings/1/expiries/discover",
            json={"range_from": "2021-01-01", "range_to": "2022-06-01"},
            headers=csrf(client),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "range_before_floor"
        assert response.json()["error"]["detail"]["floor"] == "2022-01-03"

    def test_the_bse_floor_is_later_than_the_nse_one(self, client, app):
        self._arm(app)

        response = client.post(
            "/api/v1/underlyings/3/expiries/discover",
            json={"range_from": "2022-06-01", "range_to": "2023-06-01"},
            headers=csrf(client),
        )

        assert response.status_code == 400
        assert response.json()["error"]["detail"]["floor"] == "2023-08-07"

    def test_no_token_is_a_409_and_writes_nothing(self, client, app):
        self._arm(app, valid=False)

        response = client.post(
            "/api/v1/underlyings/1/expiries/discover",
            json={"range_from": "2025-01-01", "range_to": "2025-06-01"},
            headers=csrf(client),
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "needs_reauth"
        with app.state.services.engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM job")).scalar() == 0

    def test_an_inverted_range_is_a_422(self, client, app):
        self._arm(app)

        response = client.post(
            "/api/v1/underlyings/1/expiries/discover",
            json={"range_from": "2025-06-01", "range_to": "2025-01-01"},
            headers=csrf(client),
        )

        assert response.status_code == 422


class TestExpiryContracts:
    def test_it_lists_the_contracts_of_one_expiry(self, client, app):
        seed_nifty_catalog(app)

        response = client.get(f"/api/v1/expiries/1/{EXPIRY.isoformat()}/contracts")

        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 6
        assert [item["strike"] for item in items][:2] == [22900.0, 22900.0]

    def test_the_option_type_filter_narrows_it(self, client, app):
        seed_nifty_catalog(app)

        items = client.get(
            f"/api/v1/expiries/1/{EXPIRY.isoformat()}/contracts?option_type=CE"
        ).json()["items"]

        assert len(items) == 3
        assert {item["option_type"] for item in items} == {"CE"}


# ---------------------------------------------------------------------------
# Section 5, contracts
# ---------------------------------------------------------------------------


class TestContractListing:
    def test_it_returns_one_item_per_contract_with_its_resolutions(self, client, app):
        seed_nifty_catalog(app)

        body = client.get("/api/v1/contracts?underlying_id=1").json()

        assert len(body["items"]) == 6
        call = next(item for item in body["items"] if item["fyers_symbol"].endswith("23000CE"))
        assert call["resolutions"] == [
            {
                "res_id": RES_ID,
                "fyers_code": "1",
                "chart_interval": "1m",
                "rows": 375,
                "first_ts": FIRST_TS.isoformat(),
                "last_ts": LAST_TS.isoformat(),
            }
        ]
        assert call["rows"] == 375
        assert call["lot_size"] == 75
        assert call["tick_size"] == 0.05
        put = next(item for item in body["items"] if item["fyers_symbol"].endswith("23000PE"))
        assert put["resolutions"] == []
        assert put["rows"] == 0

    def test_a_contract_with_two_resolutions_appears_once(self, client, app):
        ids = seed_nifty_catalog(app)
        contract_id = ids["NSE:NIFTY25MAR23000CE"]
        cur = cursor(app)
        try:
            cur.execute(
                "INSERT INTO contract_bounds (contract_id, res_id, first_ts, last_ts, row_count,"
                " updated_at) VALUES (?, 4, ?, ?, 75, ?)",
                [contract_id, FIRST_TS, LAST_TS, datetime(2026, 9, 1)],
            )
        finally:
            cur.close()

        body = client.get("/api/v1/contracts?underlying_id=1").json()

        matching = [
            item for item in body["items"] if item["contract_id"] == contract_id
        ]
        assert len(matching) == 1
        assert [entry["res_id"] for entry in matching[0]["resolutions"]] == [RES_ID, 4]
        assert matching[0]["rows"] == 450

    def test_has_data_and_sealed_filter_server_side(self, client, app):
        seed_nifty_catalog(app)

        with_data = client.get("/api/v1/contracts?underlying_id=1&has_data=true").json()["items"]
        sealed = client.get("/api/v1/contracts?underlying_id=1&sealed=true").json()["items"]

        assert {item["option_type"] for item in with_data} == {"CE"}
        assert len(with_data) == 3
        assert len(sealed) == 3

    def test_symbol_contains_and_strike_bounds_filter(self, client, app):
        seed_nifty_catalog(app)

        items = client.get(
            "/api/v1/contracts?underlying_id=1&symbol_contains=23000&strike_min=23000"
        ).json()["items"]

        assert {item["fyers_symbol"] for item in items} == {
            "NSE:NIFTY25MAR23000CE",
            "NSE:NIFTY25MAR23000PE",
        }

    def test_sorting_by_strike_descending(self, client, app):
        seed_nifty_catalog(app)

        items = client.get(
            "/api/v1/contracts?underlying_id=1&sort=strike&dir=desc"
        ).json()["items"]

        assert [item["strike"] for item in items][0] == 23100.0
        assert [item["strike"] for item in items][-1] == 22900.0

    def test_paging_walks_every_contract_exactly_once(self, client, app):
        seed_nifty_catalog(app)

        seen: list[int] = []
        cursor_token = None
        for _ in range(10):
            url = "/api/v1/contracts?underlying_id=1&limit=2"
            if cursor_token:
                url += f"&cursor={cursor_token}"
            page = client.get(url).json()
            seen.extend(item["contract_id"] for item in page["items"])
            cursor_token = page["next_cursor"]
            if cursor_token is None:
                break

        assert sorted(seen) == sorted(set(seen))
        assert len(seen) == 6

    def test_an_unknown_sort_column_names_the_allowed_set(self, client, app):
        seed_nifty_catalog(app)

        response = client.get("/api/v1/contracts?sort=lot_size")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unknown_sort_column"
        assert "expiry_date" in response.json()["error"]["message"]

    def test_a_cursor_that_was_not_minted_here_is_a_400(self, client, app):
        seed_nifty_catalog(app)

        response = client.get("/api/v1/contracts?cursor=not-a-cursor")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_cursor"

    def test_spot_rows_are_never_listed_as_contracts(self, client, app):
        seed_nifty_catalog(app)

        items = client.get("/api/v1/contracts?underlying_id=1&limit=500").json()["items"]

        assert all(item["kind"] != "SPOT" for item in items)


class TestContractDetail:
    def test_it_carries_the_underlying_and_the_coverage_summary(self, client, app):
        ids = seed_nifty_catalog(app)
        contract_id = ids["NSE:NIFTY25MAR23000CE"]

        body = client.get(f"/api/v1/contracts/{contract_id}").json()

        assert body["fyers_symbol"] == "NSE:NIFTY25MAR23000CE"
        assert body["underlying_symbol"] == "NSE:NIFTY50-INDEX"
        assert body["underlying_name"] == "Nifty 50"
        assert body["root"] == "NIFTY"
        assert body["sealed_at"] == "2025-03-28T18:41:12"
        assert body["coverage"] == [
            {
                "res_id": RES_ID,
                "chunks": 1,
                "chunks_ok": 1,
                "chunks_empty": 0,
                "chunks_error": 0,
                "have_from": "2025-03-26",
                "have_to": EXPIRY.isoformat(),
                "rows": 375,
            }
        ]
        assert body["resolutions"][0]["first_ts"] == FIRST_TS.isoformat()

    def test_an_error_chunk_shows_up_in_the_summary(self, client, app):
        ids = seed_nifty_catalog(app)

        body = client.get(f"/api/v1/contracts/{ids['NSE:NIFTY25MAR23000PE']}").json()

        assert body["coverage"][0]["chunks_error"] == 1
        assert body["resolutions"] == []
        assert body["rows"] == 0

    def test_an_unknown_contract_is_a_404(self, client, app):
        seed_nifty_catalog(app)
        assert client.get("/api/v1/contracts/999999").status_code == 404


class TestContractBounds:
    def test_bounds_are_utc_seconds_ready_for_the_chart(self, client, app):
        ids = seed_nifty_catalog(app)
        contract_id = ids["NSE:NIFTY25MAR23000CE"]

        body = client.get(f"/api/v1/contracts/{contract_id}/bounds").json()

        assert body["contract_id"] == contract_id
        assert body["fyers_symbol"] == "NSE:NIFTY25MAR23000CE"
        entry = body["resolutions"][0]
        assert entry["res_id"] == RES_ID
        assert entry["chart_interval"] == "1m"
        assert entry["rows"] == 375
        # The stored timestamp is IST wall clock; the chart wants UTC seconds.
        assert entry["first_ts"] == int(
            FIRST_TS.replace(tzinfo=UTC).timestamp()
        ) - IST_OFFSET_SECONDS
        assert entry["last_ts"] == int(
            LAST_TS.replace(tzinfo=UTC).timestamp()
        ) - IST_OFFSET_SECONDS

    def test_a_contract_with_no_bars_answers_with_an_empty_list_not_a_404(self, client, app):
        ids = seed_nifty_catalog(app)

        body = client.get(f"/api/v1/contracts/{ids['NSE:NIFTY25MAR23000PE']}/bounds").json()

        assert body["resolutions"] == []

    def test_an_unknown_contract_is_a_404(self, client, app):
        seed_nifty_catalog(app)
        assert client.get("/api/v1/contracts/999999/bounds").status_code == 404


class TestRouteRateLimits:
    """The two limits API.md names for this file, which the middleware table does not carry."""

    def test_resolve_is_capped_at_twenty_a_minute(self, client, app):
        seed_master(app)
        app.state.services.token_broker = StubBroker(valid=False)
        body = {"query": "NIFTYNXT50"}

        allowed = 0
        for _ in range(underlyings_route.RESOLVE_LIMIT.limit.amount):
            response = client.post("/api/v1/underlyings/resolve", json=body, headers=csrf(client))
            assert response.status_code == 200
            allowed += 1

        refused = client.post("/api/v1/underlyings/resolve", json=body, headers=csrf(client))

        assert allowed == 20
        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == "rate_limited"
        assert refused.headers["Retry-After"]
        # The route rule reports first; the global fallback appends its own.
        assert refused.headers["RateLimit-Limit"].startswith("20")

    def test_the_route_rule_is_never_claimed_by_the_middleware(self, app):
        from expirymanager.security.ratelimit import RateLimiter

        limiter = app.state.services.rate_limiter or RateLimiter()

        # `rule_for` skips ENFORCED_BY_ROUTE rules, so nothing counts these calls twice.
        assert limiter.rule_for("POST", "/api/v1/underlyings/resolve") is None
        assert limiter.rule_for("POST", "/api/v1/underlyings") is None


def test_every_catalog_route_is_mounted(app):
    spec = app.openapi()["paths"]
    for path, method in (
        ("/api/v1/underlyings", "get"),
        ("/api/v1/underlyings", "post"),
        ("/api/v1/underlyings/resolve", "post"),
        ("/api/v1/underlyings/{underlying_id}", "patch"),
        ("/api/v1/underlyings/{underlying_id}", "delete"),
        ("/api/v1/underlyings/{underlying_id}/expiries", "get"),
        ("/api/v1/underlyings/{underlying_id}/expiries/discover", "post"),
        ("/api/v1/expiries/{underlying_id}/{expiry_date}/contracts", "get"),
        ("/api/v1/contracts", "get"),
        ("/api/v1/contracts/{contract_id}", "get"),
        ("/api/v1/contracts/{contract_id}/bounds", "get"),
    ):
        assert method in spec.get(path, {}), f"{method.upper()} {path} is not mounted"


def test_underlyings_module_exports_a_router():
    assert underlyings_route.router is not None
