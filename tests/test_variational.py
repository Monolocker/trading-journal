"""Tests for the Variational Omni file-import adapter.

No test here reaches the network, and neither does the adapter itself:
its only input is CSV files, written into a pytest tmp_path. The fixture
rows follow the officially documented export columns exactly.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tradejournal.domain.enums import (
    CashFlowType,
    LiquidityRole,
    Side,
    Venue,
)
from tradejournal.exchanges.base import ReadOnlyExchangeClient
from tradejournal.exchanges.normalized import (
    EventParsingError,
    to_cash_flow,
    to_fill,
)
from tradejournal.exchanges.variational import (
    TRADES_SUBDIRECTORY,
    TRANSFERS_SUBDIRECTORY,
    VariationalFileClient,
    VariationalImportError,
    parse_export_timestamp,
)

TRADES_HEADER = [
    "id",
    "created_at",
    "side",
    "instrument_type",
    "underlying",
    "price",
    "qty",
    "trade_type",
    "status",
    "liquidation_trigger_price",
]

TRANSFERS_HEADER = [
    "id",
    "created_at",
    "qty",
    "asset",
    "transfer_type",
    "status",
    "underlying",
    "instrument_type",
    "fee_type",
    "funding_rate",
]


def trade_row(**overrides: str) -> dict[str, str]:
    row = {
        "id": "t-001",
        "created_at": "2026-07-30T14:03:22+00:00",
        "side": "buy",
        "instrument_type": "perpetual_future",
        "underlying": "BTC",
        "price": "65000.5",
        "qty": "0.25",
        "trade_type": "trade",
        "status": "confirmed",
        "liquidation_trigger_price": "",
    }
    row.update(overrides)
    return row


def transfer_row(**overrides: str) -> dict[str, str]:
    row = {
        "id": "x-001",
        "created_at": "2026-07-30T16:00:00+00:00",
        "qty": "1.25",
        "asset": "USDC",
        "transfer_type": "funding",
        "status": "confirmed",
        "underlying": "BTC",
        "instrument_type": "perpetual_future",
        "fee_type": "",
        "funding_rate": "0.0001",
    }
    row.update(overrides)
    return row


def write_csv(
    path: Path, header: list[str], rows: list[dict[str, str]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture()
def import_dir(tmp_path: Path) -> Path:
    (tmp_path / TRADES_SUBDIRECTORY).mkdir()
    (tmp_path / TRANSFERS_SUBDIRECTORY).mkdir()
    return tmp_path


def make_client(import_dir: Path) -> VariationalFileClient:
    return VariationalFileClient(import_dir)


# ----------------------------------------------------------------------
# Construction and protocol
# ----------------------------------------------------------------------


def test_satisfies_read_only_protocol(import_dir: Path) -> None:
    client = make_client(import_dir)
    assert isinstance(client, ReadOnlyExchangeClient)
    assert client.venue is Venue.VARIATIONAL


def test_missing_import_dir_fails_at_construction(tmp_path: Path) -> None:
    with pytest.raises(VariationalImportError, match="does not exist"):
        VariationalFileClient(tmp_path / "nope")


def test_no_live_position_view(import_dir: Path) -> None:
    client = make_client(import_dir)
    assert client.supports_positions is False
    assert list(client.fetch_open_positions()) == []


def test_empty_subdirectories_yield_empty_results(import_dir: Path) -> None:
    client = make_client(import_dir)
    assert list(client.fetch_fills()) == []
    assert list(client.fetch_cash_flows()) == []
    assert client.skipped_events == []


# ----------------------------------------------------------------------
# Timestamp parsing
# ----------------------------------------------------------------------


def test_timestamp_accepts_aware_iso_and_z_suffix() -> None:
    expected = datetime(2026, 7, 30, 14, 3, 22, tzinfo=UTC)
    assert parse_export_timestamp("2026-07-30T14:03:22+00:00") == expected
    assert parse_export_timestamp("2026-07-30T14:03:22Z") == expected


def test_timestamp_accepts_epoch_seconds_and_milliseconds() -> None:
    expected = datetime(2026, 7, 30, 14, 3, 22, tzinfo=UTC)
    epoch_seconds = int(expected.timestamp())
    assert parse_export_timestamp(str(epoch_seconds)) == expected
    assert parse_export_timestamp(str(epoch_seconds * 1000)) == expected


def test_timestamp_rejects_naive_iso() -> None:
    with pytest.raises(EventParsingError, match="no timezone"):
        parse_export_timestamp("2026-07-30T14:03:22")


# ----------------------------------------------------------------------
# Fills
# ----------------------------------------------------------------------


def test_fill_happy_path(import_dir: Path) -> None:
    write_csv(
        import_dir / TRADES_SUBDIRECTORY / "trades.csv",
        TRADES_HEADER,
        [trade_row()],
    )
    client = make_client(import_dir)
    fills = list(client.fetch_fills())

    assert client.skipped_events == []
    assert len(fills) == 1
    fill = fills[0]
    assert fill.venue is Venue.VARIATIONAL
    assert fill.venue_fill_id == "t-001"
    assert fill.venue_symbol == "BTC"
    assert fill.symbol == "BTC-PERP"
    assert fill.timestamp == datetime(2026, 7, 30, 14, 3, 22, tzinfo=UTC)
    assert fill.side is Side.BUY
    assert fill.price == Decimal("65000.5")
    assert fill.quantity == Decimal("0.25")
    assert fill.fee == Decimal("0")
    assert fill.fee_asset == "USDC"
    assert fill.liquidity_role is LiquidityRole.TAKER
    assert fill.venue_order_id is None
    assert fill.raw_payload["trade_type"] == "trade"

    # Round-trips into the domain model without a leg assignment.
    domain_fill = to_fill(fill)
    assert domain_fill.leg_id is None


def test_liquidation_is_ingested_as_a_fill(import_dir: Path) -> None:
    write_csv(
        import_dir / TRADES_SUBDIRECTORY / "trades.csv",
        TRADES_HEADER,
        [
            trade_row(
                id="t-liq",
                side="sell",
                trade_type="liquidation",
                liquidation_trigger_price="60000",
            )
        ],
    )
    client = make_client(import_dir)
    fills = list(client.fetch_fills())
    assert len(fills) == 1
    assert fills[0].raw_payload["trade_type"] == "liquidation"
    assert fills[0].side is Side.SELL


def test_failed_and_non_perp_and_bad_rows_are_skipped(
    import_dir: Path,
) -> None:
    write_csv(
        import_dir / TRADES_SUBDIRECTORY / "trades.csv",
        TRADES_HEADER,
        [
            trade_row(id="t-ok"),
            trade_row(id="t-failed", status="failed"),
            trade_row(id="t-option", instrument_type="option"),
            trade_row(id="t-badsym", underlying="BTC USD"),
            trade_row(id="t-badside", side="hold"),
            trade_row(id="t-badprice", price="not-a-number"),
            trade_row(id="t-zeroqty", qty="0"),
            trade_row(id="t-naive", created_at="2026-07-30T14:03:22"),
        ],
    )
    client = make_client(import_dir)
    fills = list(client.fetch_fills())

    assert [fill.venue_fill_id for fill in fills] == ["t-ok"]
    reasons = {
        event.venue_event_id: event.reason
        for event in client.skipped_events
    }
    assert "not a confirmed execution" in reasons["t-failed"]
    assert "out of scope" in reasons["t-option"]
    assert "t-badsym" in reasons
    assert "neither buy nor sell" in reasons["t-badside"]
    assert "not a valid decimal" in reasons["t-badprice"]
    assert "must both be positive" in reasons["t-zeroqty"]
    assert "no timezone" in reasons["t-naive"]


def test_duplicate_and_conflicting_ids(import_dir: Path) -> None:
    original = trade_row(id="t-dup")
    conflicting = trade_row(id="t-dup", price="99999")
    write_csv(
        import_dir / TRADES_SUBDIRECTORY / "a.csv",
        TRADES_HEADER,
        [original],
    )
    write_csv(
        import_dir / TRADES_SUBDIRECTORY / "b.csv",
        TRADES_HEADER,
        [original, conflicting],
    )
    client = make_client(import_dir)
    fills = list(client.fetch_fills())

    assert len(fills) == 1
    assert fills[0].price == Decimal("65000.5")  # first occurrence wins
    reasons = [event.reason for event in client.skipped_events]
    assert any("overlapping export window" in reason for reason in reasons)
    assert any("CONFLICTING" in reason for reason in reasons)


def test_fills_are_sorted_and_since_is_inclusive(import_dir: Path) -> None:
    write_csv(
        import_dir / TRADES_SUBDIRECTORY / "trades.csv",
        TRADES_HEADER,
        [
            trade_row(id="t-late", created_at="2026-07-30T18:00:00+00:00"),
            trade_row(id="t-early", created_at="2026-07-30T10:00:00+00:00"),
            trade_row(id="t-mid", created_at="2026-07-30T14:00:00+00:00"),
        ],
    )
    client = make_client(import_dir)

    ordered = [fill.venue_fill_id for fill in client.fetch_fills()]
    assert ordered == ["t-early", "t-mid", "t-late"]

    since = datetime(2026, 7, 30, 14, 0, 0, tzinfo=UTC)
    filtered = [
        fill.venue_fill_id for fill in client.fetch_fills(since=since)
    ]
    assert filtered == ["t-mid", "t-late"]


def test_wrong_header_fails_the_load(import_dir: Path) -> None:
    write_csv(
        import_dir / TRADES_SUBDIRECTORY / "trades.csv",
        ["id", "created_at", "amount"],
        [{"id": "t-1", "created_at": "2026-07-30T14:00:00Z", "amount": "1"}],
    )
    client = make_client(import_dir)
    with pytest.raises(VariationalImportError, match="missing documented"):
        client.fetch_fills()


def test_byte_order_mark_is_tolerated(import_dir: Path) -> None:
    path = import_dir / TRADES_SUBDIRECTORY / "trades.csv"
    write_csv(path, TRADES_HEADER, [trade_row()])
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    client = make_client(import_dir)
    fills = list(client.fetch_fills())
    assert len(fills) == 1
    assert client.skipped_events == []


# ----------------------------------------------------------------------
# Cash flows
# ----------------------------------------------------------------------


def test_funding_cash_flow_happy_path(import_dir: Path) -> None:
    write_csv(
        import_dir / TRANSFERS_SUBDIRECTORY / "transfers.csv",
        TRANSFERS_HEADER,
        [transfer_row()],
    )
    client = make_client(import_dir)
    flows = list(client.fetch_cash_flows())

    assert client.skipped_events == []
    assert len(flows) == 1
    flow = flows[0]
    assert flow.venue is Venue.VARIATIONAL
    assert flow.venue_event_id == "x-001"
    assert flow.type is CashFlowType.FUNDING
    assert flow.amount == Decimal("1.25")  # signed pass-through
    assert flow.asset == "USDC"
    assert flow.venue_symbol == "BTC"
    assert flow.symbol == "BTC-PERP"
    assert flow.funding_rate == Decimal("0.0001")

    domain_flow = to_cash_flow(flow)
    assert domain_flow.trade_id is None
    assert domain_flow.leg_id is None


def test_negative_funding_passes_through_signed(import_dir: Path) -> None:
    write_csv(
        import_dir / TRANSFERS_SUBDIRECTORY / "transfers.csv",
        TRANSFERS_HEADER,
        [transfer_row(id="x-negfund", qty="-0.75")],
    )
    client = make_client(import_dir)
    flows = list(client.fetch_cash_flows())
    assert flows[0].amount == Decimal("-0.75")


def test_sign_conventions_for_transfers_and_fees(import_dir: Path) -> None:
    write_csv(
        import_dir / TRANSFERS_SUBDIRECTORY / "transfers.csv",
        TRANSFERS_HEADER,
        [
            transfer_row(
                id="x-dep",
                transfer_type="deposit",
                qty="100",
                underlying="",
                funding_rate="",
            ),
            transfer_row(
                id="x-wd",
                transfer_type="withdrawal",
                qty="40",
                underlying="",
                funding_rate="",
            ),
            transfer_row(
                id="x-fee",
                transfer_type="fee",
                qty="0.5",
                underlying="",
                fee_type="withdrawal",
                funding_rate="",
            ),
            transfer_row(
                id="x-pnl",
                transfer_type="realized_pnl",
                qty="-12.5",
                funding_rate="",
            ),
        ],
    )
    client = make_client(import_dir)
    flows = {
        flow.venue_event_id: flow for flow in client.fetch_cash_flows()
    }

    assert client.skipped_events == []
    assert flows["x-dep"].type is CashFlowType.DEPOSIT
    assert flows["x-dep"].amount == Decimal("100")
    assert flows["x-dep"].symbol is None
    assert flows["x-dep"].venue_symbol is None
    assert flows["x-wd"].type is CashFlowType.WITHDRAWAL
    assert flows["x-wd"].amount == Decimal("-40")
    assert flows["x-fee"].type is CashFlowType.FEE
    assert flows["x-fee"].amount == Decimal("-0.5")
    assert flows["x-fee"].funding_rate is None
    assert flows["x-pnl"].type is CashFlowType.REALIZED_PNL
    assert flows["x-pnl"].amount == Decimal("-12.5")


def test_anomalous_and_unknown_transfers_are_skipped(
    import_dir: Path,
) -> None:
    write_csv(
        import_dir / TRANSFERS_SUBDIRECTORY / "transfers.csv",
        TRANSFERS_HEADER,
        [
            transfer_row(
                id="x-negwd",
                transfer_type="withdrawal",
                qty="-40",
                underlying="",
                funding_rate="",
            ),
            transfer_row(
                id="x-mystery",
                transfer_type="airdrop",
                funding_rate="",
            ),
            transfer_row(id="x-pending", status="pending"),
        ],
    )
    client = make_client(import_dir)
    flows = list(client.fetch_cash_flows())

    assert flows == []
    reasons = {
        event.venue_event_id: event.reason
        for event in client.skipped_events
    }
    assert "expected a positive magnitude" in reasons["x-negwd"]
    assert "refusing to guess" in reasons["x-mystery"]
    assert "not confirmed" in reasons["x-pending"]


def test_cash_flows_sorted_and_since_inclusive(import_dir: Path) -> None:
    write_csv(
        import_dir / TRANSFERS_SUBDIRECTORY / "transfers.csv",
        TRANSFERS_HEADER,
        [
            transfer_row(id="x-b", created_at="2026-07-30T16:00:00Z"),
            transfer_row(id="x-a", created_at="2026-07-30T08:00:00Z"),
        ],
    )
    client = make_client(import_dir)
    ordered = [flow.venue_event_id for flow in client.fetch_cash_flows()]
    assert ordered == ["x-a", "x-b"]

    since = datetime(2026, 7, 30, 16, 0, 0, tzinfo=UTC)
    filtered = [
        flow.venue_event_id
        for flow in client.fetch_cash_flows(since=since)
    ]
    assert filtered == ["x-b"]