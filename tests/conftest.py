"""Shared pytest fixtures.

Every test gets its own SQLite database in a pytest temporary directory, so
tests never share state and never touch a real journal.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tradejournal.db.connection import apply_migrations, connect
from tradejournal.db.repository import Repository
from tradejournal.domain.enums import (
    CashFlowType,
    LiquidityRole,
    Side,
    Venue,
)
from tradejournal.domain.models import CashFlow, Fill

FIXTURES_DIRECTORY = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def load_fixture() -> Callable[[str], Any]:
    """Return a loader for JSON files in tests/fixtures.

    Adapter tests in later milestones use saved venue responses rather than
    live API calls, and this is how they read them.
    """

    def load(name: str) -> Any:
        path = FIXTURES_DIRECTORY / name
        if not path.exists():
            raise FileNotFoundError(f"No such fixture: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    return load


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "tradejournal-test.db"


@pytest.fixture
def connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(database_path)
    apply_migrations(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def repository(connection: sqlite3.Connection) -> Repository:
    return Repository(connection)


@pytest.fixture
def sample_fill() -> Fill:
    """A representative Hyperliquid-shaped fill."""
    return Fill(
        venue=Venue.HYPERLIQUID,
        venue_fill_id="118906512037719",
        venue_order_id="90542681",
        venue_symbol="BTC",
        symbol="BTC-PERP",
        timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        side=Side.BUY,
        price=Decimal("93787.9606019699"),
        quantity=Decimal("0.0353"),
        fee=Decimal("-0.026868"),
        fee_asset="USDC",
        liquidity_role=LiquidityRole.TAKER,
        raw_payload={"coin": "BTC", "px": "93787.9606019699", "sz": "0.0353"},
    )


@pytest.fixture
def sample_cash_flow() -> CashFlow:
    """A representative funding payment, paid by the account."""
    return CashFlow(
        venue=Venue.HYPERLIQUID,
        venue_event_id="funding:BTC:1768478400000",
        venue_symbol="BTC",
        symbol="BTC-PERP",
        timestamp=datetime(2026, 1, 15, 16, 0, 0, tzinfo=UTC),
        type=CashFlowType.FUNDING,
        amount=Decimal("-2.851187"),
        asset="USDC",
        funding_rate=Decimal("0.00005566"),
        raw_payload={"coin": "BTC", "usdc": "-2.851187"},
    )