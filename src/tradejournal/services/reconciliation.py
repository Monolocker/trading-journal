"""Leg reconstruction and cross-venue trade pairing.

This service turns immutable fills into the derived layers above them:
legs (one venue side of a position) and trades (a delta-neutral pair of
legs across two venues). It is a full-rebuild service, not an
incremental one — see "Rebuild, not update" below.

Leg reconstruction
------------------
Fills for one (venue, symbol) market are replayed oldest-first while
tracking the running signed position (buys add, sells subtract):

- A leg OPENS when the position leaves zero; its direction is the sign
  of that first move.
- Every fill encountered while the position is away from zero belongs
  to the current leg.
- A leg CLOSES when the position returns to exactly zero (Decimal
  arithmetic makes "exactly" meaningful; there is no epsilon).
- A leg still away from zero when the fills run out stays OPEN.

Leg quantity is the PEAK absolute position over the leg's life, not the
summed entry volume: for a position that scales in and out, peak
exposure is the number that matters for hedging symmetry.

Average entry price is the volume-weighted price of fills on the leg's
own side (buys for a long, sells for a short); average exit price is
the volume-weighted price of the opposite side. A leg with no exit
fills yet has average_exit_price None.

Position flips
--------------
A fill that pushes the position through zero (long 1, then sell 2)
belongs to two positions at once, but a fill is immutable and carries a
single leg_id, so it cannot be split. The policy: the flipping fill
closes the old leg and stays assigned to it; a NEW leg opens in the
opposite direction with the remainder as its starting position and no
opening fill of its own. Both effects are flagged as a position_flip
finding, and any trade containing a flip-affected leg is marked
review_required — the numbers around a flip are reconstructed, not
certain, and a human should look. For a delta-neutral funding strategy
a flip is an anomaly by definition, which is exactly why it is
surfaced rather than smoothed over.

Trade pairing (the hedge)
-------------------------
Hedging is cross-venue by construction and follows the symbol module's
contract: legs pair on base_asset() equality, never on canonical-symbol
equality, so Hyperliquid's XYZ:AAPL-PERP can hedge another venue's
AAPL-PERP while two same-venue markets can never merge. The full rules:

- different venues, opposite directions, equal base asset;
- time windows must genuinely overlap (an open leg's window extends to
  the far future); touching endpoints is not overlap;
- greedy matching in chronological order of leg opening, choosing the
  candidate with the longest overlap (ties: earliest opened, then
  smallest id). Greedy is deliberate simplicity: entries in this
  strategy open both venue sides within minutes of each other, so the
  longest-overlap candidate is the hedge. A wrong pairing under exotic
  histories surfaces as a quantity mismatch or an unpaired leg, both
  loud, rather than passing silently.

A hedged trade records symbol as the shared base asset's canonical form
(namespaces intentionally dropped — the legs keep the full per-venue
symbols), status CLOSED only when both legs are closed, opened_at as
the earlier leg opening and closed_at as the later leg close. Its
reconciliation_status is review_required when the legs' peak quantities
differ or when either leg was flip-affected, with the reason written to
the trade's reasoning field. Legs that find no counterpart stay stored
with trade_id NULL: an unpaired leg is itself a reconciliation finding,
not an error to hide.

Money fields (funding PnL, trading PnL, fees, slippage, net) stay None
in this milestone; computing them — and attributing cash flows to legs
and trades — is the PnL service's job. Cash flows are untouched here.

Rebuild, not update
-------------------
rebuild() wipes every leg and trade, then reconstructs them from fills
inside one transaction. Derived data is cheap to recompute and
correctness is easier to prove for "recompute everything from facts"
than for incremental edits; foreign keys guarantee the wipe can never
damage fills or cash flows (their references null out). Running
rebuild() twice in a row produces the same legs, the same trades, and
the same report.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import groupby

from tradejournal.db.connection import transaction
from tradejournal.db.repository import Repository
from tradejournal.domain.enums import (
    Direction,
    LegStatus,
    ReconciliationStatus,
    Side,
    TradeStatus,
    Venue,
)
from tradejournal.domain.models import Fill, Leg, Trade
from tradejournal.exchanges.symbols import base_asset, to_canonical

LOGGER = logging.getLogger(__name__)

# Stand-in for "still open" when computing window overlap. Comparisons
# need timezone-aware values, and datetime.max is naive.
_FAR_FUTURE = datetime(9999, 1, 1, tzinfo=UTC)

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Finding:
    """One human-reviewable observation from a rebuild."""

    kind: str  # "position_flip" | "quantity_mismatch" | "unpaired_leg"
    venue: Venue | None
    symbol: str
    detail: str


@dataclass(frozen=True, slots=True)
class RebuildReport:
    """What one rebuild produced, in numbers.

    legs_built = legs_paired + legs_unpaired, and
    legs_built = legs_open + legs_closed, always.
    """

    fills_total: int
    fills_assigned: int
    legs_built: int
    legs_open: int
    legs_closed: int
    trades_created: int
    legs_paired: int
    legs_unpaired: int
    findings: tuple[Finding, ...]


@dataclass(slots=True)
class _LegDraft:
    """A leg under construction, before it has a database id."""

    venue: Venue
    symbol: str
    direction: Direction
    opened_at: datetime
    fills: list[Fill] = field(default_factory=list)
    closed_at: datetime | None = None
    peak_quantity: Decimal = _ZERO
    entry_quantity: Decimal = _ZERO
    entry_notional: Decimal = _ZERO
    exit_quantity: Decimal = _ZERO
    exit_notional: Decimal = _ZERO
    flip_affected: bool = False

    @property
    def is_closed(self) -> bool:
        return self.closed_at is not None

    def absorb(self, fill: Fill) -> None:
        """Assign a fill to this leg and update the price averages."""
        self.fills.append(fill)
        entry_side = (fill.side is Side.BUY) == (
            self.direction is Direction.LONG
        )
        if entry_side:
            self.entry_quantity += fill.quantity
            self.entry_notional += fill.quantity * fill.price
        else:
            self.exit_quantity += fill.quantity
            self.exit_notional += fill.quantity * fill.price

    def to_leg(self) -> Leg:
        entry = (
            self.entry_notional / self.entry_quantity
            if self.entry_quantity > _ZERO
            else None
        )
        exit_ = (
            self.exit_notional / self.exit_quantity
            if self.exit_quantity > _ZERO
            else None
        )
        return Leg(
            venue=self.venue,
            symbol=self.symbol,
            direction=self.direction,
            quantity=self.peak_quantity,
            status=LegStatus.CLOSED if self.is_closed else LegStatus.OPEN,
            opened_at=self.opened_at,
            closed_at=self.closed_at,
            average_entry_price=entry,
            average_exit_price=exit_,
        )


def _direction_of(signed_position: Decimal) -> Direction:
    return Direction.LONG if signed_position > _ZERO else Direction.SHORT


def _reconstruct_market(
    venue: Venue, symbol: str, fills: list[Fill]
) -> tuple[list[_LegDraft], list[Finding]]:
    """Replay one market's fills into a sequence of leg drafts."""
    drafts: list[_LegDraft] = []
    findings: list[Finding] = []
    current: _LegDraft | None = None
    position = _ZERO

    for fill in fills:
        delta = fill.quantity if fill.side is Side.BUY else -fill.quantity
        new_position = position + delta

        if current is None:
            current = _LegDraft(
                venue=venue,
                symbol=symbol,
                direction=_direction_of(delta),
                opened_at=fill.timestamp,
            )
            drafts.append(current)

        current.absorb(fill)

        crossed_zero = (
            position != _ZERO
            and new_position != _ZERO
            and (new_position > _ZERO) != (position > _ZERO)
        )
        if new_position == _ZERO:
            current.closed_at = fill.timestamp
            current = None
        elif crossed_zero:
            # The flipping fill stays with the leg it closed; the new
            # leg starts from the remainder with no opening fill.
            current.closed_at = fill.timestamp
            current.flip_affected = True
            findings.append(
                Finding(
                    kind="position_flip",
                    venue=venue,
                    symbol=symbol,
                    detail=(
                        f"position crossed zero at "
                        f"{fill.timestamp.isoformat()}; the crossing fill "
                        f"is assigned to the closing leg and the new leg "
                        f"opens without an entry fill"
                    ),
                )
            )
            current = _LegDraft(
                venue=venue,
                symbol=symbol,
                direction=_direction_of(new_position),
                opened_at=fill.timestamp,
                peak_quantity=abs(new_position),
                flip_affected=True,
            )
            drafts.append(current)
        else:
            if abs(new_position) > current.peak_quantity:
                current.peak_quantity = abs(new_position)

        position = new_position

    return drafts, findings


@dataclass(slots=True)
class _PairableLeg:
    """A stored leg plus the draft context pairing needs."""

    leg_id: int
    draft: _LegDraft
    base: str
    trade_id: int | None = None

    @property
    def window_end(self) -> datetime:
        return self.draft.closed_at or _FAR_FUTURE


def _overlap(a: _PairableLeg, b: _PairableLeg) -> timedelta:
    start = max(a.draft.opened_at, b.draft.opened_at)
    end = min(a.window_end, b.window_end)
    return end - start


def _eligible(a: _PairableLeg, b: _PairableLeg) -> bool:
    return (
        a.draft.venue is not b.draft.venue
        and a.draft.direction is not b.draft.direction
        and a.base == b.base
        and _overlap(a, b) > timedelta(0)
    )


class ReconciliationService:
    """Rebuilds legs and trades from the immutable fills."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._repository = Repository(connection)

    def rebuild(self) -> RebuildReport:
        findings: list[Finding] = []
        with transaction(self._connection):
            self._repository.wipe_derived_tables()

            fills = self._repository.all_fills_ordered()
            pairable: list[_PairableLeg] = []
            fills_assigned = 0

            for (venue, symbol), group in groupby(
                fills, key=lambda f: (f.venue, f.symbol)
            ):
                drafts, market_findings = _reconstruct_market(
                    venue, symbol, list(group)
                )
                findings.extend(market_findings)
                for draft in drafts:
                    leg_id = self._repository.insert_leg(draft.to_leg())
                    fill_ids = [
                        fill.id for fill in draft.fills if fill.id is not None
                    ]
                    self._repository.assign_fills_to_leg(leg_id, fill_ids)
                    fills_assigned += len(fill_ids)
                    pairable.append(
                        _PairableLeg(
                            leg_id=leg_id,
                            draft=draft,
                            base=base_asset(draft.symbol),
                        )
                    )

            trades_created = self._pair(pairable, findings)

            for record in pairable:
                if record.trade_id is None:
                    findings.append(
                        Finding(
                            kind="unpaired_leg",
                            venue=record.draft.venue,
                            symbol=record.draft.symbol,
                            detail=(
                                f"{record.draft.direction} leg opened "
                                f"{record.draft.opened_at.isoformat()} has "
                                f"no counterpart on the other venue"
                            ),
                        )
                    )

            report = RebuildReport(
                fills_total=len(fills),
                fills_assigned=fills_assigned,
                legs_built=len(pairable),
                legs_open=sum(
                    1 for r in pairable if not r.draft.is_closed
                ),
                legs_closed=sum(1 for r in pairable if r.draft.is_closed),
                trades_created=trades_created,
                legs_paired=sum(
                    1 for r in pairable if r.trade_id is not None
                ),
                legs_unpaired=sum(
                    1 for r in pairable if r.trade_id is None
                ),
                findings=tuple(findings),
            )

        self._log(report)
        return report

    # ------------------------------------------------------------------
    # Pairing
    # ------------------------------------------------------------------

    def _pair(
        self, pairable: list[_PairableLeg], findings: list[Finding]
    ) -> int:
        trades_created = 0
        ordered = sorted(
            pairable, key=lambda r: (r.draft.opened_at, r.leg_id)
        )
        for record in ordered:
            if record.trade_id is not None:
                continue
            candidates = [
                other
                for other in ordered
                if other.trade_id is None
                and other.leg_id != record.leg_id
                and _eligible(record, other)
            ]
            if not candidates:
                continue
            counterpart = max(
                candidates,
                key=lambda other: (
                    _overlap(record, other),
                    -other.draft.opened_at.timestamp(),
                    -other.leg_id,
                ),
            )
            trade_id = self._create_trade(record, counterpart, findings)
            record.trade_id = trade_id
            counterpart.trade_id = trade_id
            self._repository.set_leg_trade(record.leg_id, trade_id)
            self._repository.set_leg_trade(counterpart.leg_id, trade_id)
            trades_created += 1
        return trades_created

    def _create_trade(
        self,
        a: _PairableLeg,
        b: _PairableLeg,
        findings: list[Finding],
    ) -> int:
        reasons: list[str] = []
        if a.draft.peak_quantity != b.draft.peak_quantity:
            detail = (
                f"leg quantities differ: {a.draft.venue} "
                f"{a.draft.peak_quantity} vs {b.draft.venue} "
                f"{b.draft.peak_quantity}"
            )
            reasons.append(detail)
            findings.append(
                Finding(
                    kind="quantity_mismatch",
                    venue=None,
                    symbol=a.base,
                    detail=detail,
                )
            )
        if a.draft.flip_affected or b.draft.flip_affected:
            reasons.append(
                "a leg was affected by a position flip; reconstructed "
                "numbers need review"
            )

        both_closed = a.draft.is_closed and b.draft.is_closed
        closed_at: datetime | None = None
        if both_closed:
            assert a.draft.closed_at is not None
            assert b.draft.closed_at is not None
            closed_at = max(a.draft.closed_at, b.draft.closed_at)

        trade = Trade(
            symbol=to_canonical(a.base),
            status=TradeStatus.CLOSED if both_closed else TradeStatus.OPEN,
            reconciliation_status=(
                ReconciliationStatus.REVIEW_REQUIRED
                if reasons
                else ReconciliationStatus.OK
            ),
            opened_at=min(a.draft.opened_at, b.draft.opened_at),
            closed_at=closed_at,
            reasoning="; ".join(reasons) if reasons else None,
        )
        return self._repository.insert_trade(trade)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    @staticmethod
    def _log(report: RebuildReport) -> None:
        LOGGER.info(
            "rebuild: fills=%d assigned=%d legs=%d (open=%d closed=%d) "
            "trades=%d paired=%d unpaired=%d findings=%d",
            report.fills_total,
            report.fills_assigned,
            report.legs_built,
            report.legs_open,
            report.legs_closed,
            report.trades_created,
            report.legs_paired,
            report.legs_unpaired,
            len(report.findings),
        )
        for finding in report.findings:
            LOGGER.warning(
                "finding %s: %s %s: %s",
                finding.kind,
                finding.venue if finding.venue else "cross-venue",
                finding.symbol,
                finding.detail,
            )