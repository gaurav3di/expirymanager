"""Keep the DuckDB catalog mirrors in step with the SQLite registry.

underlying_registry in SQLite is the authoritative record of what the user asked for, and
dim_underlying is the DuckDB mirror nine analytic queries join against: the spot series, the ATM
pick, the option chain, the catalog listing, the denormalised export, two maintenance assertions,
the spot id allocator and the backward walk's bounds query.

The two are seeded by different mechanisms and only one of them is a migration. Migration 0004
writes four builtin underlyings into SQLite, and a SQL migration cannot reach DuckDB, so on a
fresh install the registry has rows and the mirror has none. Every one of those nine joins then
answers empty while the application looks perfectly configured. Measured on 2026-09-10: a real six
contract NIFTY download landed 52,873 candles correctly, and then the backward walk found no
mirror row, returned None, and gave up without sealing the contract or planning the next chunk. No
error was raised anywhere.

Contract discovery repairs a single underlying lazily, which is precisely why this hid for so
long: the first download fixed it as a side effect, so it only bit an install that read anything
before downloading. This module does the whole set at startup so an install is consistent before
anything reads it, and because the write is an upsert it doubles as the repair path for drift.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import Engine, text

from expirymanager.db.writes import UnderlyingRow, upsert_underlying

__all__ = ["mirror_underlyings"]

log = logging.getLogger(__name__)

# Mirrors the codes in brokers.fyers.symbology. Duplicated rather than imported because this
# module sits in db/ and must not drag the broker package into the lifespan's import path.
_EXCHANGE_CODES = {"NSE": 10, "MCX": 11, "BSE": 12}
_SEGMENT_CODES = {"CM": 10, "FO": 11, "CD": 12, "COM": 20}

_SELECT = """
SELECT underlying_id, fyers_symbol, root, exchange, segment, instrument_kind, display_name,
       data_from, spot_contract_id, is_active
  FROM underlying_registry
 ORDER BY underlying_id
"""


async def mirror_underlyings(engine: Engine, writer) -> int:  # type: ignore[no-untyped-def]
    """Copy every registry row into dim_underlying. Idempotent, returns the count written.

    One row failing does not stop the rest. A registry row carrying an exchange or segment this
    build has no code for is a configuration problem with one underlying, and refusing to mirror
    the other three because of it would turn a small problem into an unusable install.
    """
    with engine.connect() as connection:
        rows = connection.execute(text(_SELECT)).fetchall()

    written = 0
    for row in rows:
        exchange = str(row[3]).upper()
        segment = str(row[4]).upper()
        exchange_code = _EXCHANGE_CODES.get(exchange)
        segment_code = _SEGMENT_CODES.get(segment)
        if exchange_code is None or segment_code is None:
            log.warning(
                "registry row has an exchange or segment this build cannot code, not mirrored",
                extra={
                    "underlying_id": int(row[0]),
                    "exchange": exchange,
                    "segment": segment,
                },
            )
            continue

        await upsert_underlying(
            writer,
            UnderlyingRow(
                underlying_id=int(row[0]),
                fyers_symbol=str(row[1]),
                root=str(row[2]),
                exchange=exchange,
                exchange_code=exchange_code,
                segment=segment,
                segment_code=segment_code,
                instrument_kind=str(row[5]),
                display_name=str(row[6]),
                data_from=(
                    row[7] if isinstance(row[7], date) else date.fromisoformat(str(row[7]))
                ),
                # The registry already holds a reserved spot id. Passing it through is what keeps
                # the two tables agreeing on one number instead of allocating a second.
                spot_contract_id=int(row[8]),
                is_active=bool(row[9]),
            ),
        )
        written += 1

    return written
