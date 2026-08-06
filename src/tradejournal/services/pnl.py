"""Cash-flow attribution and trade valuation.

This service answers "what did each trade actually earn?" It attaches
cash flows to the leg they belong to, then computes each trade's
monetary fields from the immutable facts beneath it. Like leg
reconstruction, it recomputes wholesale rather than editing in place:
every number it writes is reproducible from fills and cash flows, and
running it twice produces identical values, making this an idempotent
procedure.

Attribution
-----------
A cash flow belongs to a leg when all three hold: same venue, same
canonical symbol, and a timestamp inside the leg's window
[opened_at, closed_at] (inclusive at both ends; an open leg's window
runs to the far future). Legs on one market never overlap in time, so
this is unambiguous — with one exception, the instant of a position
flip, where one leg closes and the next opens at the same timestamp. A
flow landing exactly there is attributed to the CLOSING leg, because
the funding or fee accrued over the period that just ended, and the
case is recorded as an ambiguous_attribution finding rather than
resolved silently.

A cash flow carrying no symbol is account-level by nature, operations
such as: deposits, withdrawals, and transfers (all between accounts). 
These are never attributed to a trade. Leaving it unattributed is the 
correct answer, not a gap.

A symbol-bearing flow that matches no leg IS a finding: funding accrued
on a market where reconstruction saw no open position means either a
missing export window or a normalisation mismatch, and it is reported
as unattributed_cash_flow.

Valuation
---------
For each trade, from its legs' fills and attributed flows:

- actual_funding_pnl: sum of attributed FUNDING amounts across both
  legs. Signed from the account's perspective: received is positive,
  paid is negative. This is the number the strategy exists to earn.
- trading_pnl: realised price PnL, summed per leg over the quantity
  that both entered and exited. For a long leg that is
  (exit_vwap - entry_vwap) * matched_quantity; for a short leg the
  sign reverses. A leg still open contributes nothing: unrealised PnL
  needs a mark price, which a journal built only from executions does
  not have.
- fees: a positive aggregate cost — the absolute value of fill fees on
  both legs plus attributed FEE cash flows. Fills carry fees on
  Hyperliquid; Variational is zero-fee at the fill and reports fees as
  separate events instead (e.g., in the event of liquidation -> liquidation
  fees). Thus, both sources must be summed for the number to be comparable 
  across venues.
- net_pnl: trading_pnl + actual_funding_pnl - fees.

Why slippage_cost stays None
----------------------------
Slippage is execution price measured against a benchmark — the
intended price, or the mid at the moment of execution. Neither venue's
fill record carries one, and this journal never invents a number it
cannot derive. Note also that for a hedged pair the two legs' combined
trading_pnl already equals the exit spread minus the entry spread
between venues, so an independently-computed "slippage" would
double-count that same quantity. The field stays None until a real
benchmark exists to measure against. A real schema to be potentially
added is mark price at fill time.

Partial trades and honesty
--------------------------
A trade whose legs are not both closed still gets a valuation, but of
realized components only, and it is marked review_required with the
reason recorded: funding earned so far is real, while price PnL is
incomplete until the position is closed. Any review flag written by
leg reconstruction (quantity mismatch, position flip) is preserved —
this service adds reasons, never erases them.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from tradejournal.db.connection import transaction
from tradejournal.db.repository import Repository
from tradejournal.domain.enums import (
    CashFlowType,
    Direction,
    LegStatus,
    ReconciliationStatus,
    Side,
)
from tradejournal.domain.models import CashFlow, Fill, Leg
from tradejournal.services.reconciliation import Finding

LOGGER = logging.getLogger(__name__)

_FAR_FUTURE = datetime(9999, 1, 1, tzinfo=UTC)
_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class ValuationReport:
    """What one valuation pass did, in numbers."""

    cash_flows_total: int
    cash_flows_attributed: int
    cash_flows_account_level: int
    cash_flows_unattributed: int
    trades_valued: int
    trades_requiring_review: int
    total_funding_pnl: Decimal
    total_trading_pnl: Decimal
    total_fees: Decimal
    total_net_pnl: Decimal
    findings: tuple[Finding, ...]


def _window_end(leg: Leg) -> datetime:
    return leg.closed_at or _FAR_FUTURE


def _matches(flow: CashFlow, leg: Leg) -> bool:
    if flow.symbol is None or flow.venue is not leg.venue:
        return False
    if flow.symbol != leg.symbol:
        return False
    if leg.opened_at is None:
        return False
    return leg.opened_at <= flow.timestamp <= _window_end(leg)


def _realized_trading_pnl(leg: Leg, fills: list[Fill]) -> Decimal:
    """Realized price PnL for one leg, over the quantity that round-tripped."""
    entry_quantity = _ZERO
    entry_notional = _ZERO
    exit_quantity = _ZERO
    exit_notional = _ZERO
    for fill in fills:
        is_entry = (fill.side is Side.BUY) == (leg.direction is Direction.LONG)
        if is_entry:
            entry_quantity += fill.quantity
            entry_notional += fill.quantity * fill.price
        else:
            exit_quantity += fill.quantity
            exit_notional += fill.quantity * fill.price

    matched = min(entry_quantity, exit_quantity)
    if matched <= _ZERO:
        return _ZERO

    entry_vwap = entry_notional / entry_quantity
    exit_vwap = exit_notional / exit_quantity
    move = exit_vwap - entry_vwap
    if leg.direction is Direction.SHORT:
        move = -move
    return move * matched


class PnLService:
    """Attributes cash flows and computes every trade's money fields."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._repository = Repository(connection)

    def recompute(self) -> ValuationReport:
        findings: list[Finding] = []
        with transaction(self._connection):
            attributed, account_level, unattributed = self._attribute(
                findings
            )
            (
                trades_valued,
                trades_requiring_review,
                totals,
            ) = self._value_trades()

            report = ValuationReport(
                cash_flows_total=(
                    attributed + account_level + unattributed
                ),
                cash_flows_attributed=attributed,
                cash_flows_account_level=account_level,
                cash_flows_unattributed=unattributed,
                trades_valued=trades_valued,
                trades_requiring_review=trades_requiring_review,
                total_funding_pnl=totals["funding"],
                total_trading_pnl=totals["trading"],
                total_fees=totals["fees"],
                total_net_pnl=totals["net"],
                findings=tuple(findings),
            )

        self._log(report)
        return report

    # ------------------------------------------------------------------
    # Attribution
    # ------------------------------------------------------------------

    def _attribute(
        self, findings: list[Finding]
    ) -> tuple[int, int, int]:
        self._repository.clear_cash_flow_assignments()
        legs = self._repository.all_legs()
        flows = self._repository.all_cash_flows_ordered()

        attributed = 0
        account_level = 0
        unattributed = 0

        for flow in flows:
            if flow.symbol is None:
                account_level += 1
                continue

            candidates = [leg for leg in legs if _matches(flow, leg)]
            if not candidates:
                unattributed += 1
                findings.append(
                    Finding(
                        kind="unattributed_cash_flow",
                        venue=flow.venue,
                        symbol=flow.symbol,
                        detail=(
                            f"{flow.type} at {flow.timestamp.isoformat()} "
                            f"matches no reconstructed leg on this market"
                        ),
                    )
                )
                continue

            if len(candidates) > 1:
                findings.append(
                    Finding(
                        kind="ambiguous_attribution",
                        venue=flow.venue,
                        symbol=flow.symbol,
                        detail=(
                            f"{flow.type} at {flow.timestamp.isoformat()} "
                            f"falls in {len(candidates)} leg windows; "
                            f"attributed to the leg that closed then"
                        ),
                    )
                )
            # Earliest-opened wins: at a flip instant that is the leg
            # whose period the flow accrued over.
            chosen = min(
                candidates,
                key=lambda leg: (leg.opened_at or _FAR_FUTURE, leg.id or 0),
            )
            if flow.id is not None and chosen.id is not None:
                self._repository.assign_cash_flow(
                    flow.id, leg_id=chosen.id, trade_id=chosen.trade_id
                )
                attributed += 1

        return attributed, account_level, unattributed

    # ------------------------------------------------------------------
    # Valuation
    # ------------------------------------------------------------------

    def _value_trades(self) -> tuple[int, int, dict[str, Decimal]]:
        totals = {
            "funding": _ZERO,
            "trading": _ZERO,
            "fees": _ZERO,
            "net": _ZERO,
        }
        valued = 0
        requiring_review = 0

        for trade in self._repository.all_trades():
            if trade.id is None:
                continue
            legs = self._repository.legs_for_trade(trade.id)
            flows = self._repository.cash_flows_for_trade(trade.id)

            funding = sum(
                (
                    flow.amount
                    for flow in flows
                    if flow.type is CashFlowType.FUNDING
                ),
                _ZERO,
            )
            fee_events = sum(
                (
                    abs(flow.amount)
                    for flow in flows
                    if flow.type is CashFlowType.FEE
                ),
                _ZERO,
            )

            trading = _ZERO
            fill_fees = _ZERO
            for leg in legs:
                if leg.id is None:
                    continue
                fills = self._repository.fills_for_leg(leg.id)
                trading += _realized_trading_pnl(leg, fills)
                fill_fees += sum((abs(f.fee) for f in fills), _ZERO)

            fees = fill_fees + fee_events
            net = trading + funding - fees

            reasons = [trade.reasoning] if trade.reasoning else []
            status = trade.reconciliation_status
            if any(leg.status is LegStatus.OPEN for leg in legs):
                reasons.append(
                    "trade is still open; funding is realised to date but "
                    "price PnL is incomplete until both legs close"
                )
                status = ReconciliationStatus.REVIEW_REQUIRED

            self._repository.update_trade_financials(
                trade.id,
                actual_funding_pnl=funding,
                trading_pnl=trading,
                fees=fees,
                slippage_cost=None,
                net_pnl=net,
                reconciliation_status=status,
                reasoning="; ".join(reasons) if reasons else None,
            )

            valued += 1
            if status is not ReconciliationStatus.OK:
                requiring_review += 1
            totals["funding"] += funding
            totals["trading"] += trading
            totals["fees"] += fees
            totals["net"] += net

        return valued, requiring_review, totals

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    @staticmethod
    def _log(report: ValuationReport) -> None:
        LOGGER.info(
            "valuation: flows=%d attributed=%d account_level=%d "
            "unattributed=%d trades=%d review=%d",
            report.cash_flows_total,
            report.cash_flows_attributed,
            report.cash_flows_account_level,
            report.cash_flows_unattributed,
            report.trades_valued,
            report.trades_requiring_review,
        )
        for finding in report.findings:
            LOGGER.warning(
                "finding %s: %s %s: %s",
                finding.kind,
                finding.venue if finding.venue else "cross-venue",
                finding.symbol,
                finding.detail,
            )