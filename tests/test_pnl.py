"""Tests for cash-flow attribution and trade valuation.

Fills and cash flows go in through the repository, legs and trades come
from a real reconciliation rebuild, and every assertion runs against a
real SQLite database. Under test: window-based attribution, the
account-level and unattributed cases, funding/trading/fee arithmetic
with exact Decimal values, the open-trade honesty rule, preservation of
reconstruction's review reasons, and recompute idempotency.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradejournal.db.repository import Repository
from tradejournal.domain.enums import (
    CashFlowType,
    LiquidityRole,
    ReconciliationStatus,
    Side,
    Venue,
)
from tradejournal.domain.models import CashFlow, Fill
from tradejournal.services.pnl import PnLService, ValuationReport
from tradejournal.services.reconciliation import ReconciliationService

BASE_TIME = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

_COUNTER = iter(range(1, 10_000))


def make_fill(
    *,
    venue: Venue = Venue.HYPERLIQUID,
    symbol: str = "BTC-PERP",
    side: Side,
    price: str,
    quantity: str,
    fee: str = "0",
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
        fee=Decimal(fee),
        fee_asset="USDC",
        liquidity_role=LiquidityRole.TAKER,
        raw_payload={"id": fill_id},
    )


def make_flow(
    *,
    venue: Venue = Venue.HYPERLIQUID,
    symbol: str | None = "BTC-PERP",
    type: CashFlowType = CashFlowType.FUNDING,
    amount: str,
    minutes: int = 0,
) -> CashFlow:
    event_id = f"flow-{next(_COUNTER)}"
    return CashFlow(
        venue=venue,
        venue_event_id=event_id,
        venue_symbol=None if symbol is None else symbol.removesuffix("-PERP"),
        symbol=symbol,
        timestamp=BASE_TIME + timedelta(minutes=minutes),
        type=type,
        amount=Decimal(amount),
        asset="USDC",
        raw_payload={"id": event_id},
    )


def insert_fills(repository: Repository, fills: list[Fill]) -> None:
    for fill in fills:
        assert repository.insert_fill(fill) is not None


def insert_flows(repository: Repository, flows: list[CashFlow]) -> None:
    for flow in flows:
        assert repository.insert_cash_flow(flow) is not None


def hedged_pair(
    *, hl_fee: str = "0", var_fee: str = "0", close: bool = True
) -> list[Fill]:
    fills = [
        make_fill(
            venue=Venue.HYPERLIQUID,
            side=Side.BUY,
            price="100",
            quantity="1",
            fee=hl_fee,
            minutes=0,
        ),
        make_fill(
            venue=Venue.VARIATIONAL,
            side=Side.SELL,
            price="100",
            quantity="1",
            fee=var_fee,
            minutes=1,
        ),
    ]
    if close:
        fills += [
            make_fill(
                venue=Venue.HYPERLIQUID,
                side=Side.SELL,
                price="104",
                quantity="1",
                fee=hl_fee,
                minutes=600,
            ),
            make_fill(
                venue=Venue.VARIATIONAL,
                side=Side.BUY,
                price="103",
                quantity="1",
                fee=var_fee,
                minutes=601,
            ),
        ]
    return fills


def rebuild_and_value(
    connection: sqlite3.Connection,
) -> ValuationReport:
    ReconciliationService(connection).rebuild()
    return PnLService(connection).recompute()


# ----------------------------------------------------------------------
# Attribution
# ----------------------------------------------------------------------


def test_funding_is_attributed_to_the_leg_whose_window_contains_it(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_fills(repository, hedged_pair())
    insert_flows(
        repository,
        [
            make_flow(amount="0.5", minutes=100),
            make_flow(
                venue=Venue.VARIATIONAL, amount="0.25", minutes=200
            ),
        ],
    )
    report = rebuild_and_value(connection)

    assert report.cash_flows_attributed == 2
    assert report.cash_flows_unattributed == 0
    assert repository.unattributed_cash_flows() == []

    legs = repository.all_legs()
    for leg in legs:
        assert leg.id is not None
        for flow in repository.cash_flows_for_leg(leg.id):
            assert flow.venue is leg.venue
            assert flow.trade_id == leg.trade_id


def test_account_level_flows_are_never_attributed(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_fills(repository, hedged_pair())
    insert_flows(
        repository,
        [
            make_flow(
                symbol=None,
                type=CashFlowType.DEPOSIT,
                amount="1000",
                minutes=50,
            ),
            make_flow(
                symbol=None,
                type=CashFlowType.WITHDRAWAL,
                amount="-200",
                minutes=60,
            ),
        ],
    )
    report = rebuild_and_value(connection)

    assert report.cash_flows_account_level == 2
    assert report.cash_flows_attributed == 0
    assert report.cash_flows_unattributed == 0
    # Account-level money is not a finding: it simply is not trade money.
    assert report.findings == ()


def test_flow_outside_every_leg_window_is_a_finding(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_fills(repository, hedged_pair())
    insert_flows(
        repository,
        [make_flow(amount="0.5", minutes=5000)],  # long after both closed
    )
    report = rebuild_and_value(connection)

    assert report.cash_flows_unattributed == 1
    assert [f.kind for f in report.findings] == ["unattributed_cash_flow"]
    assert len(repository.unattributed_cash_flows()) == 1


def test_flow_on_an_unknown_market_is_a_finding(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_fills(repository, hedged_pair())
    insert_flows(
        repository, [make_flow(symbol="ETH-PERP", amount="0.5", minutes=100)]
    )
    report = rebuild_and_value(connection)

    assert report.cash_flows_unattributed == 1
    assert report.findings[0].symbol == "ETH-PERP"


def test_window_endpoints_are_inclusive(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_fills(repository, hedged_pair())
    insert_flows(
        repository,
        [
            make_flow(amount="0.1", minutes=0),  # exactly at open
            make_flow(amount="0.1", minutes=600),  # exactly at close
        ],
    )
    report = rebuild_and_value(connection)

    assert report.cash_flows_attributed == 2
    assert report.cash_flows_unattributed == 0


# ----------------------------------------------------------------------
# Valuation arithmetic
# ----------------------------------------------------------------------


def test_funding_pnl_is_the_signed_sum(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_fills(repository, hedged_pair())
    insert_flows(
        repository,
        [
            make_flow(amount="1.50", minutes=100),
            make_flow(amount="-0.25", minutes=200),  # a period they paid
            make_flow(
                venue=Venue.VARIATIONAL, amount="0.75", minutes=300
            ),
        ],
    )
    report = rebuild_and_value(connection)

    (trade,) = repository.all_trades()
    assert trade.actual_funding_pnl == Decimal("2.00")
    assert report.total_funding_pnl == Decimal("2.00")


def test_trading_pnl_sums_both_legs_with_correct_signs(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    # HL long 100 -> 104 = +4. Variational short 100 -> 103 = -3.
    insert_fills(repository, hedged_pair())
    rebuild_and_value(connection)

    (trade,) = repository.all_trades()
    assert trade.trading_pnl == Decimal("1")


def test_fees_combine_fill_fees_and_fee_events(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    """Hyperliquid charges at the fill; Variational is zero-fee there and
    reports fees as separate events. Both must land in one number."""
    insert_fills(repository, hedged_pair(hl_fee="-0.05", var_fee="0"))
    insert_flows(
        repository,
        [
            make_flow(
                venue=Venue.VARIATIONAL,
                type=CashFlowType.FEE,
                amount="-0.30",
                minutes=100,
            )
        ],
    )
    rebuild_and_value(connection)

    (trade,) = repository.all_trades()
    # Two HL fills at 0.05 each, plus the 0.30 fee event.
    assert trade.fees == Decimal("0.40")


def test_net_pnl_is_trading_plus_funding_minus_fees(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_fills(repository, hedged_pair(hl_fee="-0.05"))
    insert_flows(repository, [make_flow(amount="2.00", minutes=100)])
    report = rebuild_and_value(connection)

    (trade,) = repository.all_trades()
    assert trade.trading_pnl == Decimal("1")
    assert trade.actual_funding_pnl == Decimal("2.00")
    assert trade.fees == Decimal("0.10")
    assert trade.net_pnl == Decimal("2.90")
    assert report.total_net_pnl == Decimal("2.90")


def test_slippage_stays_none_by_design(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_fills(repository, hedged_pair())
    rebuild_and_value(connection)

    (trade,) = repository.all_trades()
    assert trade.slippage_cost is None


def test_scaled_position_uses_vwaps_over_matched_quantity(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_fills(
        repository,
        [
            make_fill(side=Side.BUY, price="100", quantity="1", minutes=0),
            make_fill(side=Side.BUY, price="110", quantity="1", minutes=5),
            make_fill(side=Side.SELL, price="120", quantity="2", minutes=60),
            make_fill(
                venue=Venue.VARIATIONAL,
                side=Side.SELL,
                price="105",
                quantity="2",
                minutes=1,
            ),
            make_fill(
                venue=Venue.VARIATIONAL,
                side=Side.BUY,
                price="120",
                quantity="2",
                minutes=61,
            ),
        ],
    )
    rebuild_and_value(connection)

    (trade,) = repository.all_trades()
    # Long: (120 - 105) * 2 = +30. Short: (105 - 120) * 2 = -30.
    assert trade.trading_pnl == Decimal("0")


# ----------------------------------------------------------------------
# Honesty rules
# ----------------------------------------------------------------------


def test_open_trade_is_valued_but_flagged_for_review(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_fills(repository, hedged_pair(close=False))
    insert_flows(repository, [make_flow(amount="1.25", minutes=100)])
    report = rebuild_and_value(connection)

    (trade,) = repository.all_trades()
    # Funding earned so far is real ...
    assert trade.actual_funding_pnl == Decimal("1.25")
    # ... but nothing round-tripped, so price PnL is zero, not invented.
    assert trade.trading_pnl == Decimal("0")
    assert trade.reconciliation_status is (
        ReconciliationStatus.REVIEW_REQUIRED
    )
    assert trade.reasoning is not None
    assert "still open" in trade.reasoning
    assert report.trades_requiring_review == 1


def test_reconstruction_review_reasons_are_preserved(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    """A quantity mismatch is flagged by leg reconstruction; valuation
    must add to that reasoning, never overwrite it."""
    insert_fills(
        repository,
        [
            make_fill(side=Side.BUY, price="100", quantity="2", minutes=0),
            make_fill(side=Side.SELL, price="104", quantity="2", minutes=600),
            make_fill(
                venue=Venue.VARIATIONAL,
                side=Side.SELL,
                price="100",
                quantity="1",
                minutes=1,
            ),
            make_fill(
                venue=Venue.VARIATIONAL,
                side=Side.BUY,
                price="103",
                quantity="1",
                minutes=601,
            ),
        ],
    )
    rebuild_and_value(connection)

    (trade,) = repository.all_trades()
    assert trade.reconciliation_status is (
        ReconciliationStatus.REVIEW_REQUIRED
    )
    assert trade.reasoning is not None
    assert "quantities differ" in trade.reasoning


def test_unpaired_legs_produce_no_trade_money(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    """Funding on a leg with no hedge is attributed to that leg, but
    belongs to no trade, so no trade money is invented for it."""
    insert_fills(
        repository,
        [make_fill(side=Side.BUY, price="100", quantity="1", minutes=0)],
    )
    insert_flows(repository, [make_flow(amount="0.75", minutes=100)])
    report = rebuild_and_value(connection)

    assert report.cash_flows_attributed == 1
    assert report.trades_valued == 0
    assert report.total_funding_pnl == Decimal("0")
    (flow,) = repository.all_cash_flows_ordered()
    assert flow.leg_id is not None
    assert flow.trade_id is None


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------


def test_recompute_is_idempotent(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_fills(repository, hedged_pair(hl_fee="-0.05"))
    insert_flows(repository, [make_flow(amount="2.00", minutes=100)])
    ReconciliationService(connection).rebuild()
    service = PnLService(connection)

    first = service.recompute()
    (trade_after_first,) = repository.all_trades()
    second = service.recompute()
    (trade_after_second,) = repository.all_trades()

    assert first.total_net_pnl == second.total_net_pnl
    assert first.cash_flows_attributed == second.cash_flows_attributed
    assert trade_after_first.net_pnl == trade_after_second.net_pnl
    assert trade_after_first.reasoning == trade_after_second.reasoning
    assert repository.count_cash_flows() == 1


def test_totals_match_the_sum_over_trades(
    connection: sqlite3.Connection, repository: Repository
) -> None:
    insert_fills(repository, hedged_pair(hl_fee="-0.05"))
    insert_flows(repository, [make_flow(amount="2.00", minutes=100)])
    report = rebuild_and_value(connection)

    trades = repository.all_trades()
    assert report.trades_valued == len(trades)
    assert report.total_net_pnl == sum(
        (t.net_pnl for t in trades if t.net_pnl is not None), Decimal(0)
    )
    assert report.total_fees == sum(
        (t.fees for t in trades if t.fees is not None), Decimal(0)
    )