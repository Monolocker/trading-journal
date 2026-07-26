

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from tradejournal.domain.enums import (
    Venue,
    Direction,
    Side,
    LiquidityRole,
    LegStatus,
    TradeStatus,
    ReconciliationStatus,
    CashFlowType,
    SyncDataType,
)

@dataclass(frozen=True, slots=True)
class Fill:
    """Single immutable execution reported by a venue

    venue_symbol holds the venue's own market name
    symbol holds the normalized name
    leg_id is None until reconstruction assigns the fill,
    and stays None when assignment is not confident
    """

    venue: Venue
    venue_fill_id: str
    venue_symbol: str
    symbol: str
    timestamp: datetime
    side: Side
    price: Decimal 
    quantity: Decimal
    fee: Decimal
    fee_asset: str
    liquidity_role: LiquidityRole
    raw_payload: dict[str, Any]
    venue_order_id: str | None = None
    leg_id: int | None = None
    ingested_at: datetime | None = None
    id: int | None = None

@dataclass(frozen=True, slots=True)
class CashFlow:
    """Immutable account-level cash movement.
    
    Sign convention: amounts are from the account's perspective.
    Funding received and rebates are positive, funding 
    paid and fees are negative
    
    venue_event_id is None when the venue provides no stable identifier.
    funding_rate is populated only for funding events 
    """

    venue: Venue
    timestamp: datetime
    type: CashFlowType
    amount: Decimal
    asset: str
    raw_payload: dict[str, Any]
    funding_rate: Decimal | None = None
    trade_id: int | None = None
    venue_event_id: str | None = None
    venue_symbol: str | None = None
    symbol: str | None = None
    leg_id: int | None = None
    ingested_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class Leg:
    """One venue side of a delta-neutral trade.

    - Quantity is the absolute size of the position
    - Direction carries the sign
    - Average prices are None until fills have been assigned
    - trade_id is None while the leg is unpaired.
    """

    venue: Venue
    symbol: str
    direction: Direction
    quantity: Decimal
    status: LegStatus
    trade_id: int | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    average_entry_price: Decimal | None = None
    average_exit_price: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class Trade:
    """A complete delta-neutral strategy position across two venues.

    Every monetary field here is derived and must be reproducible from the
    immutable fills and cash flows. They are cached for convenience, never
    treated as the source of truth.

    Sign convention: trading_pnl, actual_funding_pnl, slippage_cost and
    net_pnl are signed from the account's perspective. fees is a positive
    aggregate cost.
    """

    symbol: str
    status: TradeStatus
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.OK
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    reasoning: str | None = None
    actual_funding_pnl: Decimal | None = None
    trading_pnl: Decimal | None = None
    fees: Decimal | None = None
    slippage_cost: Decimal | None = None
    net_pnl: Decimal | None = None
    alert: bool = False
    alert_type: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    id: int | None = None
    legs: list[Leg] = field(default_factory=list)


@dataclass(slots=True)
class SyncState:
    """A resumable synchronisation cursor for one venue and data type."""

    venue: Venue
    data_type: SyncDataType
    last_timestamp: datetime | None = None
    last_external_id: str | None = None
    updated_at: datetime | None = None