"""Database foundation tests.

Covers schema creation, foreign key enforcement, the uniqueness constraints
that make synchronisation idempotent, and the Decimal, timestamp and JSON
storage conversions.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tradejournal.db.connection import (
    applied_migration_versions,
    apply_migrations,
    connect,
    datetime_to_epoch_ms,
    decimal_to_text,
    epoch_ms_to_datetime,
    from_canonical_json,
    initialize_database,
    reset_database,
    text_to_decimal,
    to_canonical_json,
    transaction,
)
from tradejournal.db.repository import Repository
from tradejournal.domain.enums import (
    Direction,
    LegStatus,
    SyncDataType,
    TradeStatus,
    Venue,
)
from tradejournal.domain.models import Leg, SyncState, Trade

EXPECTED_TABLES = {
    "trades",
    "legs",
    "fills",
    "cash_flows",
    "sync_state",
    "schema_migrations",
}



# Initialization and migrations

def test_migrations_create_every_table(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    names = {row["name"] for row in rows}
    assert EXPECTED_TABLES <= names


def test_migrations_are_recorded(connection: sqlite3.Connection) -> None:
    assert applied_migration_versions(connection) == {1}


def test_migrations_are_idempotent(connection: sqlite3.Connection) -> None:
    assert apply_migrations(connection) == []


def test_initialize_database_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "data" / "nested" / "journal.db"
    connection = initialize_database(nested)
    try:
        assert nested.exists()
    finally:
        connection.close()


def test_reset_database_requires_confirmation(database_path: Path) -> None:
    connection = initialize_database(database_path)
    connection.close()
    with pytest.raises(ValueError):
        reset_database(database_path)
    assert database_path.exists()

    reset_database(database_path, confirm=True)
    assert not database_path.exists()



# Foreign keys

def test_foreign_keys_are_enabled(connection: sqlite3.Connection) -> None:
    (enabled,) = connection.execute("PRAGMA foreign_keys;").fetchone()
    assert enabled == 1


def test_foreign_key_violation_is_rejected(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO legs (
                trade_id, venue, symbol, direction, quantity, status,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (999999, "hyperliquid", "BTC-PERP", "long", "1", "open", 0, 0),
        )


def test_deleting_a_leg_preserves_its_fills(
    repository: Repository, connection: sqlite3.Connection, sample_fill
) -> None:
    leg_id = repository.insert_leg(
        Leg(
            venue=Venue.HYPERLIQUID,
            symbol="BTC-PERP",
            direction=Direction.LONG,
            quantity=Decimal("0.0353"),
            status=LegStatus.OPEN,
        )
    )
    repository.insert_fill(replace(sample_fill, leg_id=leg_id))

    connection.execute("DELETE FROM legs WHERE id = ?", (leg_id,))

    assert repository.count_fills() == 1
    assert len(repository.unassigned_fills()) == 1



# Uniqueness and idempotency

def test_duplicate_fill_is_ignored(repository: Repository, sample_fill) -> None:
    first = repository.insert_fill(sample_fill)
    second = repository.insert_fill(sample_fill)

    assert first is not None
    assert second is None
    assert repository.count_fills() == 1


def test_same_fill_id_on_different_venues_is_allowed(
    repository: Repository, sample_fill
) -> None:
    repository.insert_fill(sample_fill)
    repository.insert_fill(
        replace(sample_fill, venue=Venue.VARIATIONAL, venue_symbol="BTC")
    )
    assert repository.count_fills() == 2


def test_duplicate_cash_flow_event_is_ignored(
    repository: Repository, sample_cash_flow
) -> None:
    first = repository.insert_cash_flow(sample_cash_flow)
    second = repository.insert_cash_flow(sample_cash_flow)

    assert first is not None
    assert second is None
    assert repository.count_cash_flows() == 1


def test_cash_flows_without_event_id_are_not_deduplicated(
    repository: Repository, sample_cash_flow
) -> None:
    """The partial index only applies when the venue supplied an id.

    Without one there is no honest way to recognise a repeat, so both rows
    are stored and reconciliation deals with it later.
    """
    anonymous = replace(sample_cash_flow, venue_event_id=None)
    assert repository.insert_cash_flow(anonymous) is not None
    assert repository.insert_cash_flow(anonymous) is not None
    assert repository.count_cash_flows() == 2


def test_enum_check_constraint_rejects_unknown_value(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO legs (
                venue, symbol, direction, quantity, status,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("binance", "BTC-PERP", "long", "1", "open", 0, 0),
        )



# Decimal storage

@pytest.mark.parametrize(
    "value",
    [
        Decimal("0"),
        Decimal("0.00000001"),
        Decimal("-2851.187296"),
        Decimal("93787.9606019699"),
        Decimal("1E+3"),
        Decimal("-0.000000000000000001"),
    ],
)
def test_decimal_round_trips_exactly(value: Decimal) -> None:
    assert text_to_decimal(decimal_to_text(value)) == value


def test_decimal_text_never_uses_scientific_notation() -> None:
    assert decimal_to_text(Decimal("1E+3")) == "1000"
    assert "E" not in decimal_to_text(Decimal("1E-10"))


def test_float_is_refused() -> None:
    with pytest.raises(TypeError):
        decimal_to_text(1.5)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity")])
def test_non_finite_decimal_is_refused(value: Decimal) -> None:
    with pytest.raises(ValueError):
        decimal_to_text(value)


def test_malformed_decimal_text_is_refused() -> None:
    with pytest.raises(ValueError):
        text_to_decimal("not-a-number")


def test_decimal_precision_survives_the_database(
    repository: Repository, sample_fill
) -> None:
    precise = Decimal("93787.9606019699")
    repository.insert_fill(replace(sample_fill, price=precise))

    stored = repository.get_fill_by_venue_id(
        Venue.HYPERLIQUID, sample_fill.venue_fill_id
    )
    assert stored is not None
    assert stored.price == precise
    assert isinstance(stored.price, Decimal)



# Timestamp storage

def test_timestamp_round_trips() -> None:
    moment = datetime(2026, 1, 15, 12, 30, 45, 123000, tzinfo=UTC)
    assert epoch_ms_to_datetime(datetime_to_epoch_ms(moment)) == moment


def test_non_utc_timestamp_is_normalised() -> None:
    stockholm = timezone(timedelta(hours=1))
    local = datetime(2026, 1, 15, 13, 0, 0, tzinfo=stockholm)
    expected = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    assert datetime_to_epoch_ms(local) == datetime_to_epoch_ms(expected)


def test_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError):
        datetime_to_epoch_ms(datetime(2026, 1, 15, 12, 0, 0))


def test_stored_timestamps_come_back_as_utc(
    repository: Repository, sample_fill
) -> None:
    stored = repository.get_fill_by_venue_id(
        Venue.HYPERLIQUID, sample_fill.venue_fill_id
    ) if repository.insert_fill(sample_fill) else None

    assert stored is not None
    assert stored.timestamp.tzinfo is not None
    assert stored.timestamp == sample_fill.timestamp




# JSON payload storage

def test_canonical_json_is_key_order_independent() -> None:
    assert to_canonical_json({"b": 1, "a": 2}) == to_canonical_json(
        {"a": 2, "b": 1}
    )


def test_canonical_json_refuses_non_finite_numbers() -> None:
    with pytest.raises(ValueError):
        to_canonical_json({"value": float("nan")})


def test_raw_payload_round_trips(repository: Repository, sample_fill) -> None:
    repository.insert_fill(sample_fill)
    stored = repository.get_fill_by_venue_id(
        Venue.HYPERLIQUID, sample_fill.venue_fill_id
    )
    assert stored is not None
    assert stored.raw_payload == sample_fill.raw_payload


def test_from_canonical_json_parses_plain_json() -> None:
    assert from_canonical_json('{"a":1}') == {"a": 1}



# Transactions

def test_transaction_rolls_back_on_error(
    repository: Repository, connection: sqlite3.Connection, sample_fill
) -> None:
    with pytest.raises(RuntimeError):
        with transaction(connection):
            repository.insert_fill(sample_fill)
            raise RuntimeError("simulated failure")

    assert repository.count_fills() == 0


def test_transaction_commits_on_success(
    repository: Repository, connection: sqlite3.Connection, sample_fill
) -> None:
    with transaction(connection):
        repository.insert_fill(sample_fill)
    assert repository.count_fills() == 1



# Unassigned events and sync cursors

def test_fill_may_be_stored_without_a_leg(
    repository: Repository, sample_fill
) -> None:
    """An unassignable fill must have a resting place, not be forced or lost."""
    assert repository.insert_fill(sample_fill) is not None
    assert len(repository.unassigned_fills()) == 1


def test_leg_may_be_stored_without_a_trade(repository: Repository) -> None:
    leg_id = repository.insert_leg(
        Leg(
            venue=Venue.VARIATIONAL,
            symbol="BTC-PERP",
            direction=Direction.SHORT,
            quantity=Decimal("0.0353"),
            status=LegStatus.OPEN,
        )
    )
    leg = repository.get_leg(leg_id)
    assert leg is not None
    assert leg.trade_id is None


def test_sync_cursor_advances(repository: Repository) -> None:
    first = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    later = datetime(2026, 1, 16, 12, 0, 0, tzinfo=UTC)

    repository.upsert_sync_state(
        SyncState(
            venue=Venue.HYPERLIQUID,
            data_type=SyncDataType.FILLS,
            last_timestamp=first,
            last_external_id="1",
        )
    )
    repository.upsert_sync_state(
        SyncState(
            venue=Venue.HYPERLIQUID,
            data_type=SyncDataType.FILLS,
            last_timestamp=later,
            last_external_id="2",
        )
    )

    state = repository.get_sync_state(Venue.HYPERLIQUID, SyncDataType.FILLS)
    assert state is not None
    assert state.last_timestamp == later
    assert state.last_external_id == "2"


def test_sync_cursors_are_independent_per_data_type(
    repository: Repository,
) -> None:
    repository.upsert_sync_state(
        SyncState(
            venue=Venue.HYPERLIQUID,
            data_type=SyncDataType.FILLS,
            last_external_id="fills-cursor",
        )
    )
    repository.upsert_sync_state(
        SyncState(
            venue=Venue.HYPERLIQUID,
            data_type=SyncDataType.CASH_FLOWS,
            last_external_id="cash-cursor",
        )
    )

    fills_state = repository.get_sync_state(
        Venue.HYPERLIQUID, SyncDataType.FILLS
    )
    cash_state = repository.get_sync_state(
        Venue.HYPERLIQUID, SyncDataType.CASH_FLOWS
    )
    assert fills_state is not None and cash_state is not None
    assert fills_state.last_external_id == "fills-cursor"
    assert cash_state.last_external_id == "cash-cursor"



# Trades

def test_trade_round_trips(repository: Repository) -> None:
    trade_id = repository.insert_trade(
        Trade(
            symbol="BTC-PERP",
            status=TradeStatus.OPEN,
            reasoning="Funding spread favoured a long on Variational.",
            net_pnl=Decimal("12.345678"),
        )
    )
    trade = repository.get_trade(trade_id)

    assert trade is not None
    assert trade.symbol == "BTC-PERP"
    assert trade.status is TradeStatus.OPEN
    assert trade.net_pnl == Decimal("12.345678")
    assert trade.alert is False
    assert trade.created_at is not None