"""A Fyers transport that can be driven into the states the real service produces.

`tests/fyers_fake_transport.py` answers one canned envelope per test. That is the right tool for
asserting how one response is parsed, and the wrong tool for asking what a whole download spends,
because a transport that answers the same body to every window cannot tell a test that the second
window was the wrong one.

This module answers from a model instead. A contract is given a life, a span of days on which it
traded, and every request is served from that life: a window inside the life returns the bars that
belong to that window and nothing else, a window entirely before it returns `no_data`, and a window
wider than the measured 100 calendar day limit returns the same hard HTTP 422 the live service
returns rather than a silently truncated success. That is what makes the seam, the convergence and
the spend assertions mean anything: the fake can be wrong about the window, so a test can catch a
caller that asks for the wrong one.

Four measured vendor behaviours are reproduced here, all of them from docs/API-PROBES.md:

1. The historical endpoint uses a different envelope: `s`, `symbol`, `resolution`, `columns` and
   `candles`, with no `data` wrapper and no `code` or `message` on success. Everything else uses
   the standard `s`, `code`, `message`, `data` shape.
2. The 100 calendar day limit is a hard 422 with code -50, not a truncation.
3. MCX is not served by the expired endpoints: every form answers 422.
4. `columns` is authoritative. The fake can be told to reorder it, so a reader that maps candle
   fields by position rather than through the array fails.

Every credential in this module is synthetic. Real credentials live outside the repository and are
never read by application code or by a test.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

__all__ = [
    "CANDLE_COLUMNS",
    "CANDLE_COLUMNS_WITH_OI",
    "MAX_SPAN_DAYS",
    "DEFAULT_BAR_MINUTES",
    "ContractLife",
    "FakeFyers",
    "RecordedCall",
    "epoch_for_ist",
]

# The order the live expired historical endpoint returned in the probe run. Deliberately not
# treated as fixed anywhere: `FakeFyers(column_order=...)` reorders it precisely so a reader that
# addresses candle fields by index instead of through this array is caught.
CANDLE_COLUMNS_WITH_OI = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
)
CANDLE_COLUMNS = CANDLE_COLUMNS_WITH_OI[:-1]

# Measured: a span of 100 days between range_from and range_to answers 200 and 101 answers 422
# with code -50. The span is the difference, not the inclusive day count.
MAX_SPAN_DAYS = 100

# Three bars a day, well inside every Indian session, so a test can count rows exactly without
# modelling a session length. Session hours are not constants and nothing here pretends they are:
# these are simply three moments that have been inside every observed NSE session.
DEFAULT_BAR_MINUTES = (9 * 60 + 15, 12 * 60, 15 * 60)

_IST_OFFSET_SECONDS = 19800

_HISTORICAL_PATH = "/data/history/fno/expired/historical-data"
_EXPIRY_DATES_PATH = "/data/history/fno/expired/expiry-dates"
_UNDERLYING_SYMBOLS_PATH = "/data/history/fno/expired/underlying-symbols"
_HISTORY_PATH = "/data/history"


def epoch_for_ist(moment: datetime) -> int:
    """UTC epoch seconds for a naive IST wall clock moment.

    The vendor sends epoch seconds and the product stores naive IST, so a test that builds its
    expected timestamps any other way is asserting its own arithmetic rather than the product's.
    """
    return int(moment.replace(tzinfo=UTC).timestamp()) - _IST_OFFSET_SECONDS


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One request the transport was handed, already decomposed."""

    endpoint: str
    params: dict[str, str]
    status_code: int

    @property
    def symbol(self) -> str:
        return self.params.get("symbol", "")

    @property
    def range_from(self) -> date | None:
        raw = self.params.get("range_from")
        return date.fromisoformat(raw) if raw else None

    @property
    def range_to(self) -> date | None:
        raw = self.params.get("range_to")
        return date.fromisoformat(raw) if raw else None


@dataclass(slots=True)
class ContractLife:
    """The days one contract actually traded, and how many bars it produced on each of them."""

    symbol: str
    first_day: date
    last_day: date
    bar_minutes: tuple[int, ...] = DEFAULT_BAR_MINUTES
    # Days inside the life on which nothing traded. A holiday, or a Saturday, unless the exchange
    # ran a special session on it.
    closed: frozenset[date] = field(default_factory=frozenset)
    # Days outside the ordinary weekday rule on which the exchange did trade. NSE runs these, and
    # a fake that refuses to serve them would make a real special session untestable.
    extra_sessions: frozenset[date] = field(default_factory=frozenset)

    def traded_on(self, day: date) -> bool:
        if day < self.first_day or day > self.last_day:
            return False
        if day in self.closed:
            return False
        if day in self.extra_sessions:
            return True
        return day.weekday() < 5

    def trading_days(self, start: date, end: date) -> list[date]:
        day = max(start, self.first_day)
        stop = min(end, self.last_day)
        out: list[date] = []
        while day <= stop:
            if self.traded_on(day):
                out.append(day)
            day += timedelta(days=1)
        return out

    def bars_between(self, start: date, end: date) -> list[datetime]:
        out: list[datetime] = []
        for day in self.trading_days(start, end):
            for minute in self.bar_minutes:
                out.append(
                    datetime(day.year, day.month, day.day) + timedelta(minutes=minute)
                )
        return out


class FakeFyers:
    """An httpx transport that serves from a model of what each contract traded.

    Construct one, give it lives, hand `transport` to a real `FyersClient`, and then assert on
    `request_count`, on `windows` and on what landed in the database. Nothing here is a mock: no
    test asserts that a function was called, because every one of the five defects this suite
    exists for would have passed such an assertion.
    """

    def __init__(
        self,
        *,
        resolution: str = "1",
        include_oi: bool = True,
        column_order: Iterable[str] | None = None,
        enforce_span_limit: bool = True,
        schema_version: int = 1,
    ) -> None:
        self.resolution = resolution
        self.include_oi = include_oi
        self.columns: tuple[str, ...] = tuple(
            column_order
            if column_order is not None
            else (CANDLE_COLUMNS_WITH_OI if include_oi else CANDLE_COLUMNS)
        )
        self.enforce_span_limit = enforce_span_limit
        self.schema_version = schema_version

        self.lives: dict[str, ContractLife] = {}
        self.calls: list[RecordedCall] = []
        self.by_endpoint: Counter[str] = Counter()
        # Standard envelope payloads, keyed by endpoint name.
        self.standard_payloads: dict[str, Any] = {}
        # An override consulted before anything else. Given the request and the one-based index of
        # that request, it returns a response to send instead, or None to let the model answer.
        self.inject: Callable[[httpx.Request, int], httpx.Response | None] | None = None
        # Bars a window is allowed to return beyond the ones the life says exist. Used by the
        # shrinking correction test, where a first response is longer than the truth.
        self.surplus_bars: dict[str, list[datetime]] = {}

        self.transport = httpx.MockTransport(self._handle)

    # -- building the world -------------------------------------------------

    def give_life(
        self,
        symbol: str,
        *,
        first_day: date,
        last_day: date,
        bar_minutes: Iterable[int] = DEFAULT_BAR_MINUTES,
        closed: Iterable[date] = (),
        extra_sessions: Iterable[date] = (),
    ) -> ContractLife:
        """Declare what this contract traded. Anything outside it answers no_data."""
        life = ContractLife(
            symbol=symbol,
            first_day=first_day,
            last_day=last_day,
            bar_minutes=tuple(bar_minutes),
            closed=frozenset(closed),
            extra_sessions=frozenset(extra_sessions),
        )
        self.lives[symbol] = life
        return life

    def give_life_to_all(
        self, symbols: Iterable[str], *, first_day: date, last_day: date, **kwargs: Any
    ) -> None:
        for symbol in symbols:
            self.give_life(symbol, first_day=first_day, last_day=last_day, **kwargs)

    def set_standard_payload(self, endpoint: str, payload: Any) -> None:
        self.standard_payloads[endpoint] = payload

    # -- what the test asserts on -------------------------------------------

    @property
    def request_count(self) -> int:
        """Every request that reached the transport, whatever it answered.

        This is the number a confirm gate is a promise about. A request that answered 422 still
        cost a slot of the daily budget, so a test that counted only successes would report four
        while a hundred and seven left the process.
        """
        return len(self.calls)

    def count_for(self, endpoint: str) -> int:
        return self.by_endpoint[endpoint]

    @property
    def windows(self) -> list[tuple[str, date, date]]:
        """Every candle window that was actually requested, in order."""
        out: list[tuple[str, date, date]] = []
        for call in self.calls:
            if call.endpoint != "expired-historical-data":
                continue
            start, end = call.range_from, call.range_to
            if start is not None and end is not None:
                out.append((call.symbol, start, end))
        return out

    def windows_for(self, symbol: str) -> list[tuple[date, date]]:
        return [(start, end) for sym, start, end in self.windows if sym == symbol]

    def expected_bars(self, symbol: str, start: date, end: date) -> list[datetime]:
        life = self.lives.get(symbol)
        return [] if life is None else life.bars_between(start, end)

    def reset_calls(self) -> None:
        self.calls.clear()
        self.by_endpoint.clear()

    # -- the transport itself -----------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        params = {key: value for key, value in request.url.params.items()}
        endpoint = self._endpoint_name(request.url.path)
        index = len(self.calls) + 1

        response: httpx.Response | None = None
        if self.inject is not None:
            response = self.inject(request, index)
        if response is None:
            response = self._answer(endpoint, params)

        self.calls.append(
            RecordedCall(endpoint=endpoint, params=params, status_code=response.status_code)
        )
        self.by_endpoint[endpoint] += 1
        return response

    @staticmethod
    def _endpoint_name(path: str) -> str:
        if path == _HISTORICAL_PATH:
            return "expired-historical-data"
        if path == _EXPIRY_DATES_PATH:
            return "expiry-dates"
        if path == _UNDERLYING_SYMBOLS_PATH:
            return "underlying-symbols"
        if path == _HISTORY_PATH:
            return "history"
        return path.rsplit("/", 1)[-1] or path

    def _answer(self, endpoint: str, params: dict[str, str]) -> httpx.Response:
        if endpoint in ("expired-historical-data", "history"):
            return self._answer_candles(params)
        payload = self.standard_payloads.get(endpoint)
        if payload is None:
            return json_response(
                200, {"s": "error", "code": -50, "message": "Invalid input"}
            )
        return json_response(200, {"s": "ok", "code": 200, "message": "", "data": payload})

    def _answer_candles(self, params: dict[str, str]) -> httpx.Response:
        symbol = params.get("symbol", "")
        if symbol.strip().upper().startswith("MCX:"):
            # Measured: seven MCX forms answered 422 in the run in which BSE:SENSEX-INDEX
            # answered 200, so this is a property of the endpoint and not of one symbol.
            return json_response(422, {"s": "error", "code": -50, "message": "Invalid input"})

        try:
            range_from = date.fromisoformat(params["range_from"])
            range_to = date.fromisoformat(params["range_to"])
        except (KeyError, ValueError):
            return json_response(422, {"s": "error", "code": -50, "message": "Invalid input"})

        if range_to < range_from:
            return json_response(422, {"s": "error", "code": -50, "message": "Invalid input"})

        if self.enforce_span_limit and (range_to - range_from).days > MAX_SPAN_DAYS:
            # A hard error, not a truncation. A caller that quietly asks for 120 days must fail
            # here rather than receive 100 and believe it holds 120.
            return json_response(422, {"s": "error", "code": -50, "message": "Invalid input"})

        moments = self.expected_bars(symbol, range_from, range_to)
        moments.extend(
            moment
            for moment in self.surplus_bars.get(symbol, ())
            if range_from <= moment.date() <= range_to
        )
        moments.sort()
        if not moments:
            return json_response(
                200,
                {
                    "s": "no_data",
                    "symbol": symbol,
                    "resolution": params.get("resolution", self.resolution),
                    "schema_version": self.schema_version,
                },
            )

        return json_response(
            200,
            {
                "s": "ok",
                "symbol": symbol,
                "resolution": params.get("resolution", self.resolution),
                "schema_version": self.schema_version,
                "columns": list(self.columns),
                "candles": [self._candle(moment) for moment in moments],
            },
        )

    def _candle(self, moment: datetime) -> list[Any]:
        """One candle, with every field addressed by name through `self.columns`.

        The values are derived from the moment so a test can predict any one of them, and so a
        row that landed under the wrong column is visible as a wrong number rather than as a
        plausible one.
        """
        minute_of_day = moment.hour * 60 + moment.minute
        values = {
            "timestamp": epoch_for_ist(moment),
            "open": 100.0 + minute_of_day / 100.0,
            "high": 101.0 + minute_of_day / 100.0,
            "low": 99.0 + minute_of_day / 100.0,
            "close": 100.5 + minute_of_day / 100.0,
            "volume": 1000 + minute_of_day,
            "open_interest": 50_000 + minute_of_day,
        }
        return [values[name] for name in self.columns]


# ---------------------------------------------------------------------------
# Canned failures, in the shapes the live service produces
# ---------------------------------------------------------------------------


def json_response(status_code: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def auth_error(code: int = -16, message: str = "Invalid token") -> httpx.Response:
    """The documented invalid token answer. HTTP 200 with an error envelope, as measured."""
    return json_response(200, {"s": "error", "code": code, "message": message})


def rate_limited(http_status: int = 429) -> httpx.Response:
    return json_response(
        http_status, {"s": "error", "code": -429, "message": "api rate limit exceeded"}
    )


def fatal_error(code: int = -300, message: str = "invalid symbol") -> httpx.Response:
    return json_response(200, {"s": "error", "code": code, "message": message})


def server_error(http_status: int = 503) -> httpx.Response:
    return json_response(http_status, {"s": "error", "message": "service unavailable"})


def after(count: int, response_factory: Callable[[], httpx.Response]):
    """An `inject` hook that starts answering with something else after N requests."""

    def hook(_request: httpx.Request, index: int) -> httpx.Response | None:
        return response_factory() if index > count else None

    return hook


def always(response_factory: Callable[[], httpx.Response]):
    """An `inject` hook that answers the same failure to every request."""

    def hook(_request: httpx.Request, _index: int) -> httpx.Response | None:
        return response_factory()

    return hook
