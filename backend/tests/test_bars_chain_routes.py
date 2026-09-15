"""W19: the bars and option chain routes, over a real application and a real DuckDB store.

Nothing is stubbed. A temporary data directory is filled with one underlying, one expiry, five
strikes of calls and puts and one minute bars for every one of them, then the real application is
started on that directory, a real user is provisioned and every assertion below is made against
the JSON a browser would receive.

The headline test is `test_a_now_relative_window_is_clamped_onto_the_contracts_own_bars`. The
chart library builds its first request as a span counted back from the present, every contract
this application serves expired long before the present, and without the clamp that request
matches nothing and the chart renders its empty state. That test issues the real now relative
request and asserts candles come back.

Every credential in this file is synthetic.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from expirymanager.api.v1.bars import (
    CODE_RANGE_TOO_LARGE,
    CODE_UNKNOWN_CONTRACT,
    CODE_UNKNOWN_RESOLUTION,
    CODE_UNKNOWN_UNDERLYING,
)
from expirymanager.api.v1.chain import CODE_UNKNOWN_EXPIRY
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
RESOLUTION = "1"
STRIKES = (22800, 22900, 23000, 23100, 23200)
SPOT_CLOSE = 23040.0
BARS = 6
UNDERLYING_ID = 1


def utc_epoch_for_ist(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp()) - IST_OFFSET_SECONDS


def bar_time(index: int) -> datetime:
    minute = 15 + index
    return datetime(DAY.year, DAY.month, DAY.day, 9 + minute // 60, minute % 60)


def rows_for(close: float, volume_base: int, oi_base: int) -> list[list[float]]:
    return [
        [
            utc_epoch_for_ist(bar_time(index)),
            close,
            close + 1,
            close - 1,
            close,
            volume_base + index,
            oi_base + index,
        ]
        for index in range(BARS)
    ]


def underlying_row() -> UnderlyingRow:
    return UnderlyingRow(
        underlying_id=UNDERLYING_ID,
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


def option_row(strike: int, right: str) -> ContractRow:
    return ContractRow(
        fyers_symbol=f"NSE:NIFTY25MAR{strike}{right}",
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
        option_type=right,
        expiry_date=EXPIRY,
        lot_size=75,
    )


def coverage_row(task_id: int) -> CoverageRow:
    return CoverageRow(
        status="ok", include_oi=True, columns_json=COLUMNS_JSON, task_id=task_id
    )


async def _load(store: DuckStore) -> dict:
    """Fill the store. The writer is started and stopped inside one loop deliberately.

    The writer task binds to the loop that started it, so stopping it from a second asyncio.run
    cancels the task instead of draining its queue.
    """
    await store.writer.start()
    try:
        writer = store.writer
        registered = await upsert_underlying(writer, underlying_row())
        await upsert_candle_chunk(
            writer,
            contract_id=registered.spot_contract_id,
            res_id=RES_ID,
            range_from=DAY,
            range_to=DAY,
            coverage=coverage_row(1),
            batch=candles_to_arrow(
                rows_for(SPOT_CLOSE, 0, 0), COLUMNS, registered.spot_contract_id, RES_ID
            ),
        )
        contracts = await upsert_contracts(
            writer,
            underlying_id=UNDERLYING_ID,
            expiry_date=EXPIRY,
            rows=[option_row(strike, right) for strike in STRIKES for right in ("CE", "PE")],
        )
        task_id = 2
        for strike in STRIKES:
            for right in ("CE", "PE"):
                symbol = f"NSE:NIFTY25MAR{strike}{right}"
                contract_id = contracts.contract_ids[symbol]
                close = float(abs(strike - 23000) // 100 + 1) * (10 if right == "CE" else 20)
                await upsert_candle_chunk(
                    writer,
                    contract_id=contract_id,
                    res_id=RES_ID,
                    range_from=DAY,
                    range_to=DAY,
                    coverage=coverage_row(task_id),
                    batch=candles_to_arrow(
                        rows_for(close, strike, strike * 10), COLUMNS, contract_id, RES_ID
                    ),
                )
                task_id += 1
        return {
            "spot_contract_id": registered.spot_contract_id,
            "contract_ids": dict(contracts.contract_ids),
        }
    finally:
        await store.writer.stop()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "expirymanager-home"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("expirymanager.paths.default_root", lambda: root)
    yield root
    sqlite_module.dispose_engine()


@pytest.fixture
def store_context(data_dir) -> dict:
    """Fill market.duckdb before the application opens it.

    The store is written and closed here rather than through the running application because
    DuckDB refuses a second handle on a file this process already holds.
    """
    store = DuckStore(data_dir / "market.duckdb", app_version="0.0.0-test")
    store.open()
    try:
        return asyncio.run(_load(store))
    finally:
        store.close()


@pytest.fixture
def client(data_dir, store_context):
    application = create_app(root=data_dir, serve_static=False)
    with TestClient(application, base_url=BASE_URL) as test_client:
        token = test_client.cookies.get(CSRF_COOKIE_NAME)
        response = test_client.post(
            "/api/v1/auth/setup",
            json={"username": USERNAME, "password": PASSCODE},
            headers={"X-CSRF-Token": token} if token else {},
        )
        assert response.status_code in (200, 201), response.text
        yield test_client
    sqlite_module.dispose_engine()


@pytest.fixture
def contract_ids(store_context) -> dict:
    return store_context["contract_ids"]


def atm_call(contract_ids: dict) -> int:
    return contract_ids["NSE:NIFTY25MAR23000CE"]


def get(client: TestClient, path: str, **params) -> dict:
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# -- shape -----------------------------------------------------------------


def test_bars_are_columnar_in_fyers_order_with_utc_epoch_seconds(client, contract_ids):
    body = get(client, "/api/v1/bars", contract_id=atm_call(contract_ids))
    assert body["columns"] == [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
    ]
    assert len(body["candles"]) == BARS
    first = body["candles"][0]
    assert first[0] == utc_epoch_for_ist(bar_time(0))
    # The chart library reads Bar.time as integer UTC seconds. A float or a millisecond value
    # here renders a chart labelled tens of thousands of years out.
    assert isinstance(first[0], int)
    assert first[4] == pytest.approx(10.0)
    assert body["candles"][-1][0] == utc_epoch_for_ist(bar_time(BARS - 1))
    assert body["symbol"] == "NSE:NIFTY25MAR23000CE"
    assert body["resolution"] == RESOLUTION
    assert body["res_id"] == RES_ID


def test_every_candle_timestamp_is_the_stored_ist_wall_clock_less_the_ist_offset(
    client, contract_ids
):
    body = get(client, "/api/v1/bars", contract_id=atm_call(contract_ids))
    stamps = [row[0] for row in body["candles"]]
    assert stamps == [utc_epoch_for_ist(bar_time(index)) for index in range(BARS)]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)


def test_dropping_open_interest_drops_the_column_rather_than_nulling_it(client, contract_ids):
    body = get(
        client, "/api/v1/bars", contract_id=atm_call(contract_ids), include_oi="false"
    )
    assert body["columns"] == ["timestamp", "open", "high", "low", "close", "volume"]
    assert all(len(row) == 6 for row in body["candles"])


def test_a_contract_can_be_named_by_symbol_instead_of_id(client, contract_ids):
    by_id = get(client, "/api/v1/bars", contract_id=atm_call(contract_ids))
    by_symbol = get(client, "/api/v1/bars", symbol="nse:nifty25mar23000ce")
    assert by_symbol["contract_id"] == by_id["contract_id"]
    assert by_symbol["candles"] == by_id["candles"]


# -- the clamp -------------------------------------------------------------


def test_a_now_relative_window_is_clamped_onto_the_contracts_own_bars(client, contract_ids):
    """The request the chart library actually makes on a contract that expired long ago."""
    now = int(time.time())
    span = 500 * 60
    body = get(
        client,
        "/api/v1/bars",
        contract_id=atm_call(contract_ids),
        **{"from": now - span, "to": now},
    )
    assert body["clamped"] is True
    assert len(body["candles"]) == BARS
    assert body["candles"][-1][0] == utc_epoch_for_ist(bar_time(BARS - 1))
    assert body["to_ts"] == utc_epoch_for_ist(bar_time(BARS - 1))
    # The requested span is far longer than this contract lived, so the left edge stops at its
    # first bar rather than running back past it.
    assert body["from_ts"] == body["first_ts"] == utc_epoch_for_ist(bar_time(0))


def test_the_clamp_keeps_the_requested_span_when_it_fits_inside_the_bounds(
    client, contract_ids
):
    now = int(time.time())
    span = 3 * 60
    body = get(
        client,
        "/api/v1/bars",
        contract_id=atm_call(contract_ids),
        **{"from": now - span, "to": now},
    )
    assert body["to_ts"] - body["from_ts"] == span
    # Half open on the left, inclusive of the right edge: four one minute bars span three minutes.
    assert len(body["candles"]) == 4


def test_a_window_inside_the_bounds_is_served_untouched(client, contract_ids):
    body = get(
        client,
        "/api/v1/bars",
        contract_id=atm_call(contract_ids),
        **{
            "from": utc_epoch_for_ist(bar_time(1)),
            "to": utc_epoch_for_ist(bar_time(3)),
        },
    )
    assert body["clamped"] is False
    assert [row[0] for row in body["candles"]] == [
        utc_epoch_for_ist(bar_time(index)) for index in (1, 2, 3)
    ]


def test_a_window_entirely_before_the_first_bar_answers_empty_and_is_not_slid_forward(
    client, contract_ids
):
    first = utc_epoch_for_ist(bar_time(0))
    body = get(
        client,
        "/api/v1/bars",
        contract_id=atm_call(contract_ids),
        **{"from": first - 86_400, "to": first - 3_600},
    )
    assert body["candles"] == []
    assert body["clamped"] is False


def test_naming_no_window_serves_the_contracts_whole_life(client, contract_ids):
    body = get(client, "/api/v1/bars", contract_id=atm_call(contract_ids))
    assert body["clamped"] is False
    assert body["from_ts"] == body["first_ts"] == utc_epoch_for_ist(bar_time(0))
    assert body["to_ts"] == body["last_ts"] == utc_epoch_for_ist(bar_time(BARS - 1))
    assert len(body["candles"]) == BARS


def test_a_contract_with_no_bars_at_that_resolution_answers_empty_not_an_error(
    client, contract_ids
):
    body = get(
        client, "/api/v1/bars", contract_id=atm_call(contract_ids), resolution="15"
    )
    assert body["candles"] == []
    assert body["first_ts"] is None
    assert body["last_ts"] is None


# -- resolutions -----------------------------------------------------------


def test_a_chart_interval_code_resolves_to_the_same_series_as_the_fyers_code(
    client, contract_ids
):
    by_code = get(client, "/api/v1/bars", contract_id=atm_call(contract_ids), resolution="1")
    by_interval = get(
        client, "/api/v1/bars", contract_id=atm_call(contract_ids), interval="1m"
    )
    assert by_interval["res_id"] == by_code["res_id"] == RES_ID
    assert by_interval["candles"] == by_code["candles"]


def test_a_bare_unit_interval_means_one_of_that_unit(client, contract_ids):
    body = get(client, "/api/v1/bars", contract_id=atm_call(contract_ids), interval="D")
    assert body["res_id"] == 100
    assert body["resolution"] == "D"


def test_upper_case_m_is_rejected_rather_than_read_as_minutes(client, contract_ids):
    response = client.get(
        "/api/v1/bars", params={"contract_id": atm_call(contract_ids), "interval": "M"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == CODE_UNKNOWN_RESOLUTION


def test_an_unknown_resolution_is_a_400_naming_the_code(client, contract_ids):
    response = client.get(
        "/api/v1/bars", params={"contract_id": atm_call(contract_ids), "resolution": "7m"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == CODE_UNKNOWN_RESOLUTION


def test_a_window_holding_more_candles_than_one_response_carries_is_a_400(
    client, contract_ids, monkeypatch
):
    """The ceiling is lowered rather than a million bars being written, but the path is real.

    The route catches the query layer's own RangeTooLarge and turns it into the documented code
    with the maximum in the detail, so the caller can narrow the window without guessing.
    """
    monkeypatch.setattr(
        "expirymanager.db.queries.bars.MAX_CANDLES_PER_RESPONSE", BARS - 1
    )
    response = client.get("/api/v1/bars", params={"contract_id": atm_call(contract_ids)})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == CODE_RANGE_TOO_LARGE
    assert error["detail"] == {"available": BARS, "maximum": BARS - 1}


# -- not found -------------------------------------------------------------


def test_an_unknown_contract_id_is_a_404_unknown_contract(client):
    response = client.get("/api/v1/bars", params={"contract_id": 999_999_999})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == CODE_UNKNOWN_CONTRACT


def test_an_unknown_symbol_is_a_404_unknown_contract(client):
    response = client.get("/api/v1/bars", params={"symbol": "NSE:NOSUCH25MAR1CE"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == CODE_UNKNOWN_CONTRACT


def test_naming_neither_a_contract_nor_a_symbol_is_a_422(client):
    response = client.get("/api/v1/bars")
    assert response.status_code == 422


def test_the_routes_need_a_session(store_context, data_dir):
    application = create_app(root=data_dir, serve_static=False)
    with TestClient(application, base_url=BASE_URL) as anonymous:
        assert anonymous.get("/api/v1/bars", params={"contract_id": 1}).status_code == 401
        assert (
            anonymous.get(
                "/api/v1/chain",
                params={"underlying_id": UNDERLYING_ID, "expiry_date": EXPIRY.isoformat()},
            ).status_code
            == 401
        )
    sqlite_module.dispose_engine()


# -- paging ----------------------------------------------------------------


def test_bars_before_pages_left_and_comes_back_ascending(client, contract_ids):
    body = get(
        client,
        "/api/v1/bars/before",
        contract_id=atm_call(contract_ids),
        before=utc_epoch_for_ist(bar_time(4)),
        count=2,
    )
    assert [row[0] for row in body["candles"]] == [
        utc_epoch_for_ist(bar_time(2)),
        utc_epoch_for_ist(bar_time(3)),
    ]
    assert body["clamped"] is False


def test_bars_before_is_exclusive_of_its_own_boundary(client, contract_ids):
    body = get(
        client,
        "/api/v1/bars/before",
        contract_id=atm_call(contract_ids),
        before=utc_epoch_for_ist(bar_time(0)),
    )
    assert body["candles"] == []


# -- open interest ---------------------------------------------------------


def test_the_open_interest_series_lines_up_bar_for_bar_with_the_candles(client, contract_ids):
    now = int(time.time())
    params = {"contract_id": atm_call(contract_ids), "from": now - 500 * 60, "to": now}
    candles = get(client, "/api/v1/bars", **params)
    points = get(client, "/api/v1/bars/oi", **params)
    assert points["clamped"] is True
    assert [point[0] for point in points["points"]] == [row[0] for row in candles["candles"]]
    assert [point[1] for point in points["points"]] == [row[6] for row in candles["candles"]]
    assert points["points"][0][1] == 23000 * 10


# -- spot ------------------------------------------------------------------


def test_spot_bars_serve_the_underlying_series_from_the_same_candle_table(client):
    body = get(client, "/api/v1/spot/bars", underlying_id=UNDERLYING_ID)
    assert len(body["candles"]) == BARS
    assert body["symbol"] == "NSE:NIFTY50-INDEX"
    assert body["columns"] == ["timestamp", "open", "high", "low", "close", "volume"]
    assert body["candles"][0][4] == pytest.approx(SPOT_CLOSE)


def test_spot_bars_clamp_a_now_relative_window_too(client):
    now = int(time.time())
    body = get(
        client,
        "/api/v1/spot/bars",
        underlying_id=UNDERLYING_ID,
        **{"from": now - 500 * 60, "to": now},
    )
    assert body["clamped"] is True
    assert len(body["candles"]) == BARS


def test_an_unregistered_underlying_is_a_404(client):
    response = client.get("/api/v1/spot/bars", params={"underlying_id": 4242})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == CODE_UNKNOWN_UNDERLYING


# -- the chain -------------------------------------------------------------


def chain_at(client: TestClient, **params) -> dict:
    return get(
        client,
        "/api/v1/chain",
        underlying_id=UNDERLYING_ID,
        expiry_date=EXPIRY.isoformat(),
        **params,
    )


def test_the_chain_carries_every_strike_with_both_rights(client, contract_ids):
    body = chain_at(client)
    assert [row["strike"] for row in body["rows"]] == [float(s) for s in STRIKES]
    for row in body["rows"]:
        assert row["ce"] is not None and row["pe"] is not None
        assert row["lot_size"] == 75
        assert row["ce"]["contract_id"] == contract_ids[
            f"NSE:NIFTY25MAR{int(row['strike'])}CE"
        ]
        assert row["pe"]["contract_id"] == contract_ids[
            f"NSE:NIFTY25MAR{int(row['strike'])}PE"
        ]


def test_exactly_one_chain_row_is_flagged_at_the_money_and_it_is_nearest_spot(client):
    body = chain_at(client)
    flagged = [row for row in body["rows"] if row["is_atm"]]
    assert len(flagged) == 1
    assert body["spot"] == pytest.approx(SPOT_CLOSE)
    # Spot closes at 23040, so 23000 is nearer than 23100.
    assert flagged[0]["strike"] == pytest.approx(23000.0)
    assert body["atm_strike"] == pytest.approx(23000.0)


def test_omitting_the_instant_means_the_last_bar_the_expiry_holds(client):
    body = chain_at(client)
    assert body["ts"] == utc_epoch_for_ist(bar_time(BARS - 1))
    assert body["requested_ts"] is None


def test_an_instant_between_bars_snaps_back_instead_of_answering_empty(client):
    requested = utc_epoch_for_ist(bar_time(2)) + 30
    body = chain_at(client, ts=requested)
    assert body["requested_ts"] == requested
    assert body["ts"] == utc_epoch_for_ist(bar_time(2))
    assert len(body["rows"]) == len(STRIKES)


def test_an_instant_before_every_bar_answers_an_empty_chain(client):
    body = chain_at(client, ts=utc_epoch_for_ist(bar_time(0)) - 3_600)
    assert body["rows"] == []
    assert body["ts"] is None
    assert body["atm_strike"] is None


def test_the_chain_legs_carry_the_whole_bar_not_just_the_close(client):
    body = chain_at(client, ts=utc_epoch_for_ist(bar_time(0)))
    row = next(row for row in body["rows"] if row["is_atm"])
    assert row["ce"]["close"] == pytest.approx(10.0)
    assert row["ce"]["high"] == pytest.approx(11.0)
    assert row["ce"]["low"] == pytest.approx(9.0)
    assert row["ce"]["oi"] == 23000 * 10
    assert row["ce"]["fyers_symbol"] == "NSE:NIFTY25MAR23000CE"


def test_an_unknown_expiry_is_a_404_unknown_expiry(client):
    response = client.get(
        "/api/v1/chain",
        params={"underlying_id": UNDERLYING_ID, "expiry_date": "2025-04-24"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == CODE_UNKNOWN_EXPIRY


def test_atm_agrees_with_the_row_the_chain_flags(client, contract_ids):
    chain = chain_at(client)
    pick = get(
        client,
        "/api/v1/chain/atm",
        underlying_id=UNDERLYING_ID,
        expiry_date=EXPIRY.isoformat(),
    )
    flagged = next(row for row in chain["rows"] if row["is_atm"])
    assert pick["atm_strike"] == pytest.approx(flagged["strike"])
    assert pick["ce_contract_id"] == flagged["ce"]["contract_id"]
    assert pick["pe_contract_id"] == flagged["pe"]["contract_id"]
    assert pick["ce_contract_id"] == contract_ids["NSE:NIFTY25MAR23000CE"]
    assert pick["ts"] == chain["ts"]
    assert pick["spot"] == pytest.approx(SPOT_CLOSE)


def test_atm_snaps_the_same_way_the_chain_does(client):
    requested = utc_epoch_for_ist(bar_time(3)) + 45
    pick = get(
        client,
        "/api/v1/chain/atm",
        underlying_id=UNDERLYING_ID,
        expiry_date=EXPIRY.isoformat(),
        ts=requested,
    )
    assert pick["ts"] == utc_epoch_for_ist(bar_time(3))
    assert pick["requested_ts"] == requested


# -- wiring ----------------------------------------------------------------


def test_both_route_modules_are_mounted_on_the_application(client):
    schema = client.get("/api/v1/openapi.json")
    assert schema.status_code == 200
    paths = set(schema.json()["paths"])
    assert {
        "/api/v1/bars",
        "/api/v1/bars/before",
        "/api/v1/bars/oi",
        "/api/v1/spot/bars",
        "/api/v1/chain",
        "/api/v1/chain/atm",
    } <= paths


def test_the_route_include_list_no_longer_reports_these_modules_as_missing():
    from expirymanager.api import v1 as api_v1

    missing = api_v1.missing_modules()
    assert "bars" not in missing
    assert "chain" not in missing
