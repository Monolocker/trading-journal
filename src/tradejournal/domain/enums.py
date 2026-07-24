"""Enumerations for the trade journal domain.

Every enum is a StrEnum, so class members are able to serialize to plain lowercase text
in SQLite and db stays readable in any SQL client 

The CHECK constraints in the schema mirror these values exactly. If you add a member here,
add it to the matching CHECK constraint in a new migration
"""


from __future__ import annotations
from enum import StrEnum

class Venue(StrEnum):
    HYPERLIQUID = "hyperliquid"
    VARIATIONAL = "variational"

class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"

class Side(StrEnum):
    """Side describes the fill itself, not whether it opened or closed a position."""

    BUY = "buy"
    SELL = "sell"

class LiquidityRole(StrEnum):
    """A particular venue likely does not report UNKNOWN liquidity roles. However,
    guessing such would corrupt fee and slippage analysis"""

    MAKER = "maker"
    TAKER = "taker"
    UNKNOWN = "unknown"

class LegStatus(StrEnum):
    """Lifecycle state of a single leg on a particular venue"""

    OPEN = "open"
    CLOSED = "closed"

class TradeStatus(StrEnum):
    """Lifecycle state of an overall delta-neutral trade"""

    OPEN = "open"
    CLOSED = "closed"
    UNRESOLVED = "unresolved"

class ReconciliationStatus(StrEnum):
    """Confidence in the correctness of a trade's reconstructed data

    A trade can be open and simultaneously require review, thus, 
    making the relationship between ReconciliationStatus and TradeStatus
    orthogonal
    """
    OK = "ok"
    REVIEW_REQUIRED = "review_required"
    UNRESOLVED = "unresolved"

class CashFlowType(StrEnum):
    """Category of an account-level cash movement"""

    FUNDING = "funding"
    FEE = "fee"
    # REBATE = "rebate" , REFUND = "refund" both not necessary right now.
    # Spread rebates coming soon to variational. Maker rebates exist on HL.
    # Incorporate later
    REALIZED_PNL = "realized_pnl"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    TRANSFER = "transfer"
    LIQUIDATION = "liquidation"
    OTHER = "other"

class SyncDataType(StrEnum):
    """The kind of data a synchronization cursor tracks.
    However, cursors are notoriously slow to update data.
    Alternative: set-based UPDATE operation
    """
    FILLS = "fills"
    CASH_FLOWS = "cash_flows"
    