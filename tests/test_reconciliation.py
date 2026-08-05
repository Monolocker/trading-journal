"""Tests for leg reconstruction and cross-venue trade pairing.

Fills are inserted directly through the repository — no adapters and no
network — and every assertion runs against a real SQLite database from
the shared fixtures. Under test are the service's stated policies:
position replay into legs, peak-quantity and VWAP semantics, the
position-flip policy, base-asset pairing across venues (including
namespaced HIP-3 symbols), review flags for quantity mismatches, loud
unpaired legs, and wipe-and-rebuild idempotency.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradejournal.db.repository import Repository
from tradejournal.domain.enums import (
    Direction,
    LegStatus,
    LiquidityRole,
    ReconciliationStatus,
    Side,
    TradeStatus,
    Venue,
)
from tradejournal.domain.models import Fill
from tradejournal.services.reconciliation import (
    RebuildReport,
    ReconciliationService,
)

BASE_TIME = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

_COUNTER = iter(range(1, 10_000))


def make_fill(
    *,
    venue: Venue = Venue.HYPERLIQUID,
    symbol: str = "BTC-PERP",
    side: Side,
    price: str,
    quantity: str,
    minutes: int = 0,
) -> Fill:
    fill_id = f"fill-{next(_COUNTER)}"
    return Fill(
        venue=venue,
        venue_fill_id=fill_id,
        venue_symbol=symbol.removesuffix("-PERP"),
        symbol=symbol,
        timestamp=BASE_TIME + timedelta(minutes=minutes),
        side=side,
        price=Decimal(price),
        quantity=Decimal(quantity),
        fee=Decimal("-0.01"),
        fee_asset="USDC",
        liquidity_role=LiquidityRole.TAKER,
        raw_payload={"id": fill_id},
    )


def insert_all(repository: Repository, fills: list[Fill]) -> None:
    for fill in fills:
        assert repository.insert_fill(fill) is not None


# ----------------------------------------------------------------------
# Leg reconstruction
# ----------------------------------------------------------------------


def test_open_and_close_builds_one_closed_leg(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_all(
        repository,
        [
            make_fill(side=Side.BUY, price="100", quantity="2", minutes=0),
            make_fill(side=Side.BUY, price="110", quantity="1", minutes=5),
            make_fill(side=Side.SELL, price="120", quantity="3", minutes=60),
        ],
    )
    report = ReconciliationService(connection).rebuild()

    assert report.legs_built == 1
    assert report.legs_closed == 1
    assert report.fills_assigned == 3
    assert repository.unassigned_fills() == []

    (leg,) = repository.all_legs()
    assert leg.direction is Direction.LONG
    assert leg.status is LegStatus.CLOSED
    assert leg.quantity == Decimal("3")  # peak position
    assert leg.opened_at == BASE_TIME
    assert leg.closed_at == BASE_TIME + timedelta(minutes=60)
    # Entry VWAP: (2*100 + 1*110) / 3
    assert leg.average_entry_price == Decimal("310") / Decimal("3")
    assert leg.average_exit_price == Decimal("120")
    assert leg.id is not None
    assert len(repository.fills_for_leg(leg.id)) == 3


def test_short_leg_direction_and_prices(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_all(
        repository,
        [
            make_fill(side=Side.SELL, price="200", quantity="1.5"),
            make_fill(
                side=Side.BUY, price="180", quantity="1.5", minutes=30
            ),
        ],
    )
    ReconciliationService(connection).rebuild()

    (leg,) = repository.all_legs()
    assert leg.direction is Direction.SHORT
    assert leg.quantity == Decimal("1.5")
    assert leg.average_entry_price == Decimal("200")  # sells enter a short
    assert leg.average_exit_price == Decimal("180")
    assert leg.status is LegStatus.CLOSED


def test_position_never_closed_stays_open(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_all(
        repository,
        [
            make_fill(side=Side.BUY, price="100", quantity="2"),
            make_fill(side=Side.SELL, price="105", quantity="1", minutes=10),
        ],
    )
    report = ReconciliationService(connection).rebuild()

    assert report.legs_open == 1
    (leg,) = repository.all_legs()
    assert leg.status is LegStatus.OPEN
    assert leg.closed_at is None
    assert leg.quantity == Decimal("2")
    # A partial reduce still produces an exit VWAP.
    assert leg.average_exit_price == Decimal("105")


def test_close_and_reopen_builds_two_legs(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_all(
        repository,
        [
            make_fill(side=Side.BUY, price="100", quantity="1", minutes=0),
            make_fill(side=Side.SELL, price="101", quantity="1", minutes=10),
            make_fill(side=Side.BUY, price="102", quantity="2", minutes=120),
        ],
    )
    report = ReconciliationService(connection).rebuild()

    assert report.legs_built == 2
    first, second = repository.all_legs()
    assert first.status is LegStatus.CLOSED
    assert second.status is LegStatus.OPEN
    assert second.opened_at == BASE_TIME + timedelta(minutes=120)
    assert second.quantity == Decimal("2")


def test_markets_do_not_bleed_into_each_other(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_all(
        repository,
        [
            make_fill(symbol="BTC-PERP", side=Side.BUY, price="100", quantity="1"),
            make_fill(symbol="ETH-PERP", side=Side.SELL, price="50", quantity="4"),
        ],
    )
    report = ReconciliationService(connection).rebuild()

    assert report.legs_built == 2
    symbols = {leg.symbol for leg in repository.all_legs()}
    assert symbols == {"BTC-PERP", "ETH-PERP"}


def test_position_flip_closes_and_reopens_with_finding(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_all(
        repository,
        [
            make_fill(side=Side.BUY, price="100", quantity="1", minutes=0),
            make_fill(side=Side.SELL, price="110", quantity="3", minutes=10),
        ],
    )
    report = ReconciliationService(connection).rebuild()

    assert report.legs_built == 2
    kinds = [finding.kind for finding in report.findings]
    assert "position_flip" in kinds

    first, second = repository.all_legs()
    assert first.direction is Direction.LONG
    assert first.status is LegStatus.CLOSED
    assert second.direction is Direction.SHORT
    assert second.status is LegStatus.OPEN
    assert second.quantity == Decimal("2")  # the remainder
    # The crossing fill stayed with the closing leg; the new leg has no
    # entry fills, so its entry price is honestly unknown.
    assert first.id is not None and second.id is not None
    assert len(repository.fills_for_leg(first.id)) == 2
    assert len(repository.fills_for_leg(second.id)) == 0
    assert second.average_entry_price is None


# ----------------------------------------------------------------------
# Trade pairing
# ----------------------------------------------------------------------


def hedged_pair(
    *,
    hl_symbol: str = "BTC-PERP",
    var_symbol: str = "BTC-PERP",
    hl_quantity: str = "1",
    var_quantity: str = "1",
) -> list[Fill]:
    return [
        make_fill(
            venue=Venue.HYPERLIQUID,
            symbol=hl_symbol,
            side=Side.BUY,
            price="100",
            quantity=hl_quantity,
            minutes=0,
        ),
        make_fill(
            venue=Venue.VARIATIONAL,
            symbol=var_symbol,
            side=Side.SELL,
            price="100",
            quantity=var_quantity,
            minutes=1,
        ),
        make_fill(
            venue=Venue.HYPERLIQUID,
            symbol=hl_symbol,
            side=Side.SELL,
            price="102",
            quantity=hl_quantity,
            minutes=600,
        ),
        make_fill(
            venue=Venue.VARIATIONAL,
            symbol=var_symbol,
            side=Side.BUY,
            price="102",
            quantity=var_quantity,
            minutes=601,
        ),
    ]


def test_hedged_legs_pair_into_a_closed_trade(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_all(repository, hedged_pair())
    report = ReconciliationService(connection).rebuild()

    assert report.trades_created == 1
    assert report.legs_paired == 2
    assert report.legs_unpaired == 0
    assert report.findings == ()

    legs = repository.all_legs()
    trade_ids = {leg.trade_id for leg in legs}
    assert len(trade_ids) == 1
    (trade_id,) = trade_ids
    assert trade_id is not None

    trade = repository.get_trade(trade_id)
    assert trade is not None
    assert trade.symbol == "BTC-PERP"
    assert trade.status is TradeStatus.CLOSED
    assert trade.reconciliation_status is ReconciliationStatus.OK
    assert trade.opened_at == BASE_TIME
    assert trade.closed_at == BASE_TIME + timedelta(minutes=601)
    assert trade.reasoning is None
    # Money fields are the PnL service's job, not pairing's.
    assert trade.net_pnl is None


def test_namespaced_symbol_pairs_on_base_asset(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    """The spec's key rule: XYZ:AAPL-PERP hedges AAPL-PERP because
    pairing compares base_asset(), never canonical symbol equality."""
    insert_all(
        repository,
        hedged_pair(hl_symbol="XYZ:AAPL-PERP", var_symbol="AAPL-PERP"),
    )
    report = ReconciliationService(connection).rebuild()

    assert report.trades_created == 1
    assert report.legs_unpaired == 0
    (trade,) = [
        repository.get_trade(leg.trade_id)
        for leg in repository.all_legs()
        if leg.trade_id is not None
    ][:1]
    assert trade is not None
    assert trade.symbol == "AAPL-PERP"
    # Legs keep their full per-venue canonical symbols.
    symbols = {leg.symbol for leg in repository.all_legs()}
    assert symbols == {"XYZ:AAPL-PERP", "AAPL-PERP"}


def test_quantity_mismatch_pairs_but_requires_review(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_all(
        repository, hedged_pair(hl_quantity="2", var_quantity="1.5")
    )
    report = ReconciliationService(connection).rebuild()

    assert report.trades_created == 1
    kinds = [finding.kind for finding in report.findings]
    assert "quantity_mismatch" in kinds

    (trade_id,) = {
        leg.trade_id
        for leg in repository.all_legs()
        if leg.trade_id is not None
    }
    trade = repository.get_trade(trade_id)
    assert trade is not None
    assert trade.reconciliation_status is (
        ReconciliationStatus.REVIEW_REQUIRED
    )
    assert trade.reasoning is not None
    assert "quantities differ" in trade.reasoning


def test_same_venue_and_same_direction_never_pair(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_all(
        repository,
        [
            # Two longs on different venues: same direction, no pair.
            make_fill(
                venue=Venue.HYPERLIQUID,
                side=Side.BUY,
                price="100",
                quantity="1",
            ),
            make_fill(
                venue=Venue.VARIATIONAL,
                side=Side.BUY,
                price="100",
                quantity="1",
                minutes=1,
            ),
        ],
    )
    report = ReconciliationService(connection).rebuild()

    assert report.trades_created == 0
    assert report.legs_unpaired == 2
    assert {f.kind for f in report.findings} == {"unpaired_leg"}
    assert len(repository.unpaired_legs()) == 2


def test_non_overlapping_legs_do_not_pair(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_all(
        repository,
        [
            # HL long closes at minute 10 ...
            make_fill(
                venue=Venue.HYPERLIQUID,
                side=Side.BUY,
                price="100",
                quantity="1",
                minutes=0,
            ),
            make_fill(
                venue=Venue.HYPERLIQUID,
                side=Side.SELL,
                price="101",
                quantity="1",
                minutes=10,
            ),
            # ... and the Variational short opens at minute 20.
            make_fill(
                venue=Venue.VARIATIONAL,
                side=Side.SELL,
                price="101",
                quantity="1",
                minutes=20,
            ),
        ],
    )
    report = ReconciliationService(connection).rebuild()

    assert report.trades_created == 0
    assert report.legs_unpaired == 2


def test_sequential_positions_pair_one_to_one(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    """Two rounds of the same hedge produce two trades, each pairing the
    legs that actually overlap."""
    first_round = hedged_pair()
    second_round = [
        make_fill(
            venue=Venue.HYPERLIQUID,
            side=Side.BUY,
            price="100",
            quantity="1",
            minutes=1000,
        ),
        make_fill(
            venue=Venue.VARIATIONAL,
            side=Side.SELL,
            price="100",
            quantity="1",
            minutes=1001,
        ),
    ]
    insert_all(repository, first_round + second_round)
    report = ReconciliationService(connection).rebuild()

    assert report.legs_built == 4
    assert report.trades_created == 2
    assert report.legs_unpaired == 0

    open_trades = [
        trade_id
        for leg in repository.all_legs()
        if (trade_id := leg.trade_id) is not None
        and (trade := repository.get_trade(trade_id)) is not None
        and trade.status is TradeStatus.OPEN
    ]
    # The second round is still open; the first is closed.
    assert len(set(open_trades)) == 1


def test_flip_affected_pairing_requires_review(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_all(
        repository,
        [
            # Hyperliquid: long 1, then sell 2 -> flip into a short 1.
            make_fill(
                venue=Venue.HYPERLIQUID,
                side=Side.BUY,
                price="100",
                quantity="1",
                minutes=0,
            ),
            make_fill(
                venue=Venue.HYPERLIQUID,
                side=Side.SELL,
                price="100",
                quantity="2",
                minutes=10,
            ),
            # Variational long 1 overlapping the flip-born short.
            make_fill(
                venue=Venue.VARIATIONAL,
                side=Side.BUY,
                price="100",
                quantity="1",
                minutes=11,
            ),
        ],
    )
    report = ReconciliationService(connection).rebuild()

    assert report.trades_created == 1
    paired = [
        leg for leg in repository.all_legs() if leg.trade_id is not None
    ]
    assert len(paired) == 2
    trade = repository.get_trade(paired[0].trade_id)
    assert trade is not None
    assert trade.reconciliation_status is (
        ReconciliationStatus.REVIEW_REQUIRED
    )
    assert trade.reasoning is not None
    assert "flip" in trade.reasoning


# ----------------------------------------------------------------------
# Rebuild semantics
# ----------------------------------------------------------------------


def test_rebuild_is_idempotent(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_all(repository, hedged_pair())
    service = ReconciliationService(connection)

    first = service.rebuild()
    second = service.rebuild()

    assert isinstance(first, RebuildReport)
    assert first.legs_built == second.legs_built == 2
    assert first.trades_created == second.trades_created == 1
    assert repository.count_legs() == 2
    assert repository.count_trades() == 1
    assert repository.unassigned_fills() == []


def test_rebuild_incorporates_new_fills(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    fills = hedged_pair()
    insert_all(repository, fills[:2])  # only the opens
    service = ReconciliationService(connection)
    first = service.rebuild()
    assert first.trades_created == 1
    assert first.legs_open == 2

    insert_all(repository, fills[2:])  # now the closes arrive
    second = service.rebuild()
    assert second.trades_created == 1
    assert second.legs_closed == 2
    (trade_id,) = {
        leg.trade_id
        for leg in repository.all_legs()
        if leg.trade_id is not None
    }
    trade = repository.get_trade(trade_id)
    assert trade is not None
    assert trade.status is TradeStatus.CLOSED


def test_report_arithmetic_always_reconciles(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_all(
        repository,
        hedged_pair()
        + [
            make_fill(
                venue=Venue.HYPERLIQUID,
                symbol="ETH-PERP",
                side=Side.BUY,
                price="50",
                quantity="1",
            )
        ],
    )
    report = ReconciliationService(connection).rebuild()

    assert report.legs_built == report.legs_paired + report.legs_unpaired
    assert report.legs_built == report.legs_open + report.legs_closed
    assert report.fills_assigned == report.fills_total