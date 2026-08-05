"""Tests for the synchronisation service.

No network and no real adapter: a FakeClient satisfying the
ReadOnlyExchangeClient protocol hands the service scripted normalized
events, and every assertion is made against a real SQLite database from
the shared connection fixture. What is under test here is exactly the
service's own promises: idempotent ingestion, inclusive cursor resume,
atomic insert-plus-cursor transactions, refusal of un-deduplicatable
cash flows, and skip attribution.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradejournal.db.repository import Repository
from tradejournal.domain.enums import (
    CashFlowType,
    LiquidityRole,
    Side,
    SyncDataType,
    Venue,
)
from tradejournal.exchanges.base import ReadOnlyExchangeClient
from tradejournal.exchanges.normalized import (
    NormalizedCashFlow,
    NormalizedFill,
    NormalizedPosition,
    SkippedEvent,
)
from tradejournal.services.sync import SyncReport, SyncService

BASE_TIME = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def make_fill(
    fill_id: str, *, minutes: int = 0, symbol: str = "BTC-PERP"
) -> NormalizedFill:
    return NormalizedFill(
        venue=Venue.HYPERLIQUID,
        venue_fill_id=fill_id,
        venue_order_id=None,
        venue_symbol=symbol.removesuffix("-PERP"),
        symbol=symbol,
        timestamp=BASE_TIME + timedelta(minutes=minutes),
        side=Side.BUY,
        price=Decimal("100"),
        quantity=Decimal("1"),
        fee=Decimal("-0.01"),
        fee_asset="USDC",
        liquidity_role=LiquidityRole.TAKER,
        raw_payload={"id": fill_id},
    )


def make_flow(
    event_id: str | None, *, minutes: int = 0
) -> NormalizedCashFlow:
    return NormalizedCashFlow(
        venue=Venue.HYPERLIQUID,
        venue_event_id=event_id,
        venue_symbol="BTC",
        symbol="BTC-PERP",
        timestamp=BASE_TIME + timedelta(minutes=minutes),
        type=CashFlowType.FUNDING,
        amount=Decimal("0.5"),
        asset="USDC",
        funding_rate=Decimal("0.0001"),
        raw_payload={"id": event_id},
    )


class FakeClient:
    """Scripted adapter: returns pre-set events at or after `since`,
    records what `since` it was called with, and can add skipped events
    during a fetch the way a real adapter does."""

    venue: Venue = Venue.HYPERLIQUID

    def __init__(
        self,
        fills: list[NormalizedFill] | None = None,
        flows: list[NormalizedCashFlow] | None = None,
    ) -> None:
        self.fills = fills or []
        self.flows = flows or []
        self.skipped_events: list[SkippedEvent] = []
        self.skip_on_next_fetch: list[SkippedEvent] = []
        self.fills_since_calls: list[datetime | None] = []
        self.flows_since_calls: list[datetime | None] = []

    @property
    def supports_positions(self) -> bool:
        return False

    def fetch_open_positions(self) -> list[NormalizedPosition]:
        return []

    def fetch_fills(
        self, since: datetime | None = None
    ) -> list[NormalizedFill]:
        self.fills_since_calls.append(since)
        self.skipped_events.extend(self.skip_on_next_fetch)
        self.skip_on_next_fetch = []
        return [
            fill
            for fill in sorted(self.fills, key=lambda f: f.timestamp)
            if since is None or fill.timestamp >= since
        ]

    def fetch_cash_flows(
        self, since: datetime | None = None
    ) -> list[NormalizedCashFlow]:
        self.flows_since_calls.append(since)
        self.skipped_events.extend(self.skip_on_next_fetch)
        self.skip_on_next_fetch = []
        return [
            flow
            for flow in sorted(self.flows, key=lambda f: f.timestamp)
            if since is None or flow.timestamp >= since
        ]


def test_fake_client_satisfies_protocol() -> None:
    assert isinstance(FakeClient(), ReadOnlyExchangeClient)


# ----------------------------------------------------------------------
# First sync: everything lands, cursor set
# ----------------------------------------------------------------------


def test_first_sync_inserts_everything_and_sets_cursor(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    client = FakeClient(
        fills=[make_fill("f-1"), make_fill("f-2", minutes=5)],
        flows=[make_flow("x-1"), make_flow("x-2", minutes=10)],
    )
    service = SyncService(connection)

    fill_report, flow_report = service.sync(client)

    assert fill_report.fetched == 2
    assert fill_report.inserted == 2
    assert fill_report.duplicates == 0
    assert repository.count_fills() == 2
    assert flow_report.inserted == 2
    assert repository.count_cash_flows() == 2

    # First sync starts from the beginning of history.
    assert client.fills_since_calls == [None]
    assert fill_report.cursor_before is None
    assert fill_report.cursor_after == BASE_TIME + timedelta(minutes=5)

    fills_state = repository.get_sync_state(
        Venue.HYPERLIQUID, SyncDataType.FILLS
    )
    assert fills_state is not None
    assert fills_state.last_timestamp == BASE_TIME + timedelta(minutes=5)
    assert fills_state.last_external_id == "f-2"

    flows_state = repository.get_sync_state(
        Venue.HYPERLIQUID, SyncDataType.CASH_FLOWS
    )
    assert flows_state is not None
    assert flows_state.last_timestamp == BASE_TIME + timedelta(minutes=10)
    assert flows_state.last_external_id == "x-2"


# ----------------------------------------------------------------------
# Second sync: idempotent, resumes inclusively
# ----------------------------------------------------------------------


def test_second_sync_is_idempotent_and_resumes_from_cursor(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    client = FakeClient(
        fills=[make_fill("f-1"), make_fill("f-2", minutes=5)],
        flows=[make_flow("x-1")],
    )
    service = SyncService(connection)
    service.sync(client)

    fill_report, flow_report = service.sync(client)

    # Resume passes the stored cursor timestamp itself: inclusive, so the
    # boundary event is re-fetched and deduplicated, never missed.
    assert client.fills_since_calls[1] == BASE_TIME + timedelta(minutes=5)
    assert fill_report.fetched == 1
    assert fill_report.inserted == 0
    assert fill_report.duplicates == 1
    assert flow_report.duplicates == 1
    assert repository.count_fills() == 2
    assert repository.count_cash_flows() == 1

    # The cursor did not move: nothing newer arrived.
    state = repository.get_sync_state(Venue.HYPERLIQUID, SyncDataType.FILLS)
    assert state is not None
    assert state.last_timestamp == BASE_TIME + timedelta(minutes=5)


def test_new_events_after_cursor_are_ingested(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    client = FakeClient(fills=[make_fill("f-1")])
    service = SyncService(connection)
    service.sync_fills(client)

    client.fills.append(make_fill("f-2", minutes=30))
    report = service.sync_fills(client)

    assert report.inserted == 1
    assert repository.count_fills() == 2
    state = repository.get_sync_state(Venue.HYPERLIQUID, SyncDataType.FILLS)
    assert state is not None
    assert state.last_timestamp == BASE_TIME + timedelta(minutes=30)
    assert state.last_external_id == "f-2"


# ----------------------------------------------------------------------
# Empty fetch: cursor untouched
# ----------------------------------------------------------------------


def test_empty_fetch_leaves_cursor_untouched(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    service = SyncService(connection)
    report = service.sync_fills(FakeClient())

    assert report.fetched == 0
    assert report.cursor_before is None
    assert report.cursor_after is None
    assert (
        repository.get_sync_state(Venue.HYPERLIQUID, SyncDataType.FILLS)
        is None
    )


# ----------------------------------------------------------------------
# Atomicity: inserts and cursor commit or roll back together
# ----------------------------------------------------------------------


def test_failure_rolls_back_inserts_and_cursor_together(
    connection: sqlite3.Connection,
    repository: Repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(fills=[make_fill("f-1"), make_fill("f-2", minutes=5)])
    service = SyncService(connection)

    def explode(state: object) -> None:
        raise RuntimeError("simulated failure after inserts")

    monkeypatch.setattr(
        service._repository, "upsert_sync_state", explode
    )
    with pytest.raises(RuntimeError, match="simulated failure"):
        service.sync_fills(client)

    # Neither the fills nor the cursor survived: the next sync starts
    # exactly where this one did, and re-ingests safely.
    assert repository.count_fills() == 0
    assert (
        repository.get_sync_state(Venue.HYPERLIQUID, SyncDataType.FILLS)
        is None
    )


def test_cash_flow_failure_does_not_undo_committed_fills(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    class ExplodingFlowsClient(FakeClient):
        def fetch_cash_flows(
            self, since: datetime | None = None
        ) -> list[NormalizedCashFlow]:
            raise RuntimeError("venue fell over between streams")

    client = ExplodingFlowsClient(fills=[make_fill("f-1")])
    service = SyncService(connection)

    with pytest.raises(RuntimeError, match="fell over"):
        service.sync(client)

    # Fills committed in their own transaction before cash flows began.
    assert repository.count_fills() == 1
    assert (
        repository.get_sync_state(Venue.HYPERLIQUID, SyncDataType.FILLS)
        is not None
    )
    assert (
        repository.get_sync_state(
            Venue.HYPERLIQUID, SyncDataType.CASH_FLOWS
        )
        is None
    )


# ----------------------------------------------------------------------
# Refusal of un-deduplicatable cash flows
# ----------------------------------------------------------------------


def test_cash_flow_without_event_id_is_refused_not_inserted(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    client = FakeClient(
        flows=[make_flow(None), make_flow("x-1", minutes=5)]
    )
    service = SyncService(connection)

    report = service.sync_cash_flows(client)

    assert report.fetched == 2
    assert report.inserted == 1
    assert report.refused == 1
    assert report.duplicates == 0
    assert repository.count_cash_flows() == 1

    # Re-running changes nothing: this is the duplication the refusal
    # exists to prevent.
    report_again = service.sync_cash_flows(client)
    assert report_again.inserted == 0
    assert repository.count_cash_flows() == 1


# ----------------------------------------------------------------------
# Skip attribution
# ----------------------------------------------------------------------


def test_skips_are_attributed_to_the_fetch_that_produced_them(
    connection: sqlite3.Connection,
) -> None:
    client = FakeClient(fills=[make_fill("f-1")])
    stale = SkippedEvent(data_type="fills", reason="from an earlier fetch")
    client.skipped_events.append(stale)
    fresh = SkippedEvent(data_type="fills", reason="bad row this fetch")
    client.skip_on_next_fetch = [fresh]
    service = SyncService(connection)

    report = service.sync_fills(client)

    assert report.skipped == (fresh,)
    assert stale not in report.skipped


def test_skipped_rows_do_not_block_cursor_advancement(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    client = FakeClient(fills=[make_fill("f-1", minutes=5)])
    client.skip_on_next_fetch = [
        SkippedEvent(data_type="fills", reason="unparseable row")
    ]
    service = SyncService(connection)

    report = service.sync_fills(client)

    assert len(report.skipped) == 1
    state = repository.get_sync_state(Venue.HYPERLIQUID, SyncDataType.FILLS)
    assert state is not None
    assert state.last_timestamp == BASE_TIME + timedelta(minutes=5)


# ----------------------------------------------------------------------
# Report arithmetic
# ----------------------------------------------------------------------


def test_report_counts_always_reconcile(
    connection: sqlite3.Connection,
) -> None:
    client = FakeClient(
        flows=[make_flow(None), make_flow("x-1"), make_flow("x-1")],
    )
    service = SyncService(connection)

    report = service.sync_cash_flows(client)

    assert isinstance(report, SyncReport)
    assert report.fetched == report.inserted + report.duplicates + (
        report.refused
    )