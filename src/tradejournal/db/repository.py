"""SQLite persistence for the trade journal.

Every statement in this module is parameterised. No SQL is ever built by
string interpolation of caller-supplied values.

Insert methods for immutable event types are idempotent: re-inserting an
event the database already holds returns None instead of raising, which is
what allows overlapping synchronisation windows to be replayed safely.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from tradejournal.db.connection import (
    datetime_to_epoch_ms,
    decimal_to_text,
    from_canonical_json,
    optional_datetime_to_epoch_ms,
    optional_decimal_to_text,
    optional_epoch_ms_to_datetime,
    optional_text_to_decimal,
    text_to_decimal,
    to_canonical_json,
    utc_now,
)
from tradejournal.domain.enums import (
    CashFlowType,
    Direction,
    LegStatus,
    LiquidityRole,
    ReconciliationStatus,
    Side,
    SyncDataType,
    TradeStatus,
    Venue,
)
from tradejournal.domain.models import CashFlow, Fill, Leg, SyncState, Trade


class Repository:
    """Reads and writes domain models to SQLite.

    Takes an open connection rather than a path, so tests can hand it a
    temporary database and services can share one connection and one
    transaction.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection


 
    # Trades
    # ------
    def insert_trade(self, trade: Trade) -> int:
        now = datetime_to_epoch_ms(utc_now())
        cursor = self._connection.execute(
            """
            INSERT INTO trades (
                symbol, status, reconciliation_status, opened_at, closed_at,
                reasoning, actual_funding_pnl, trading_pnl, fees,
                slippage_cost, net_pnl, alert, alert_type,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.symbol,
                str(trade.status),
                str(trade.reconciliation_status),
                optional_datetime_to_epoch_ms(trade.opened_at),
                optional_datetime_to_epoch_ms(trade.closed_at),
                trade.reasoning,
                optional_decimal_to_text(trade.actual_funding_pnl),
                optional_decimal_to_text(trade.trading_pnl),
                optional_decimal_to_text(trade.fees),
                optional_decimal_to_text(trade.slippage_cost),
                optional_decimal_to_text(trade.net_pnl),
                1 if trade.alert else 0,
                trade.alert_type,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def get_trade(self, trade_id: int) -> Trade | None:
        row = self._connection.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        return None if row is None else self._row_to_trade(row)



    # Legs
    # ----
    def insert_leg(self, leg: Leg) -> int:
        now = datetime_to_epoch_ms(utc_now())
        cursor = self._connection.execute(
            """
            INSERT INTO legs (
                trade_id, venue, symbol, direction, quantity,
                opened_at, closed_at, average_entry_price,
                average_exit_price, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                leg.trade_id,
                str(leg.venue),
                leg.symbol,
                str(leg.direction),
                decimal_to_text(leg.quantity),
                optional_datetime_to_epoch_ms(leg.opened_at),
                optional_datetime_to_epoch_ms(leg.closed_at),
                optional_decimal_to_text(leg.average_entry_price),
                optional_decimal_to_text(leg.average_exit_price),
                str(leg.status),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def get_leg(self, leg_id: int) -> Leg | None:
        row = self._connection.execute(
            "SELECT * FROM legs WHERE id = ?", (leg_id,)
        ).fetchone()
        return None if row is None else self._row_to_leg(row)

    def legs_for_trade(self, trade_id: int) -> list[Leg]:
        rows = self._connection.execute(
            "SELECT * FROM legs WHERE trade_id = ? ORDER BY id", (trade_id,)
        ).fetchall()
        return [self._row_to_leg(row) for row in rows]



    # Fills
    # ------
    def insert_fill(self, fill: Fill) -> int | None:
        """Insert a fill, ignoring one the database already holds.

        Returns the new row id, or None when (venue, venue_fill_id) already
        exists. This is the mechanism that makes repeated synchronisation
        of an overlapping time window safe.
        """
        ingested_at = fill.ingested_at or utc_now()
        cursor = self._connection.execute(
            """
            INSERT INTO fills (
                leg_id, venue, venue_fill_id, venue_order_id, venue_symbol,
                symbol, timestamp, side, price, quantity, fee, fee_asset,
                liquidity_role, raw_payload, ingested_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (venue, venue_fill_id) DO NOTHING
            """,
            (
                fill.leg_id,
                str(fill.venue),
                fill.venue_fill_id,
                fill.venue_order_id,
                fill.venue_symbol,
                fill.symbol,
                datetime_to_epoch_ms(fill.timestamp),
                str(fill.side),
                decimal_to_text(fill.price),
                decimal_to_text(fill.quantity),
                decimal_to_text(fill.fee),
                fill.fee_asset,
                str(fill.liquidity_role),
                to_canonical_json(fill.raw_payload),
                datetime_to_epoch_ms(ingested_at),
            ),
        )
        if cursor.rowcount == 0:
            return None
        return int(cursor.lastrowid)

    def get_fill_by_venue_id(
        self, venue: Venue, venue_fill_id: str
    ) -> Fill | None:
        row = self._connection.execute(
            "SELECT * FROM fills WHERE venue = ? AND venue_fill_id = ?",
            (str(venue), venue_fill_id),
        ).fetchone()
        return None if row is None else self._row_to_fill(row)

    def fills_for_leg(self, leg_id: int) -> list[Fill]:
        rows = self._connection.execute(
            "SELECT * FROM fills WHERE leg_id = ? ORDER BY timestamp, id",
            (leg_id,),
        ).fetchall()
        return [self._row_to_fill(row) for row in rows]

    def unassigned_fills(self) -> list[Fill]:
        """Fills that reconciliation could not confidently assign."""
        rows = self._connection.execute(
            "SELECT * FROM fills WHERE leg_id IS NULL ORDER BY timestamp, id"
        ).fetchall()
        return [self._row_to_fill(row) for row in rows]

    def count_fills(self) -> int:
        (count,) = self._connection.execute(
            "SELECT COUNT(*) FROM fills"
        ).fetchone()
        return int(count)


    # Reconstruction Support
    # ----------------------
    # Legs and trades are derived data: every one of their vlaues must be 
    # reproducible from the immutable fills. These methods exist for the 
    # reconciliation service's rebuild cycle, which wipes and rebuilds 
    # them wholesale rather than editing them incrementally

    def wipe_derived_tables(self) -> None:
        """Delete every leg and trade
        
        Deleting legs sets fills.leg_id and cash_flows.leg_id to NULL.
        Deleting trades sets cash_flows.trade_id to NULL. 
        Fills and cash flows are never touched directly;
        they are the fact a rebuild starts from 
        """
        self._connection.execute("DELETE FROM legs")
        self._connection.execute("DELETE FROM trades")

    def all_fills_ordered(self) -> list[Fill]:
        """Every fill, grouped by market and ordered by execution time"""
        rows = self._connection.execute(
            "SELECT * FROM fills ORDER BY venue, symbol, timestamp, id"
        ).fetchall()
        return [self._row_to_fill(row) for row in rows]
    
    def assign_fills_to_leg(
            self, leg_id: int, fill_ids: Sequence[int]
    ) -> None:
        self._connection.executemany(
            "UPDATE fills SET leg_id = ? WHERE id = ?",
            [(leg_id, fill_id) for fill_id in fill_ids],
        )

    def set_leg_trade(self, leg_id: int, trade_id: int) -> None:
        now = datetime_to_epoch_ms(utc_now())
        self._connection_execute(
            "UPDATE legs SET trade_id = ?, updated_at = ?, WHERE id = ?",
            (trade_id, now, leg_id),
        )

    def all_legs(self) -> list[Leg]:
        rows = self._connection.execute(
            "SELECT * FORM legs ORDER BY opened_at, id"
        ).fetchall()
        return [self._row_to_leg(row) for row in rows]
    
    def unpaired_legs(self) -> list[Leg]:
        """Legs w/o a trade: each one is reconciliation finding."""
        rows = self._connection.execute(
            "SELECT * FROM legs WHERE trade_id IS NULL ORDER BY opened_at, id"
        ).fetchall()
        return [self._row_to_leg(row) for row in rows]
    
    def count_legs(self) -> int:
        (count,) = self._connection.execute(
            "SELECT COUNT(*) FROM legs"
        ).fetchone()
        return int(count)
    
    def count_trades(self) -> int:
        (count,) = self._connection.execute(
            "SELECT COUNT(*) FROM trades"
        ).fetchone()
        return int(count)


    # Cash flows
    # ----------
    def insert_cash_flow(self, cash_flow: CashFlow) -> int | None:
        """Insert a cash flow, ignoring a duplicate external event.

        Returns None when venue_event_id is present and already stored.
        Events without a venue_event_id are always inserted, because the
        venue gave us no way to recognise a repeat.
        """
        ingested_at = cash_flow.ingested_at or utc_now()
        cursor = self._connection.execute(
            """
            INSERT INTO cash_flows (
                trade_id, leg_id, venue, venue_event_id, venue_symbol,
                symbol, timestamp, type, amount, asset, funding_rate,
                raw_payload, ingested_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (venue, venue_event_id)
                WHERE venue_event_id IS NOT NULL
                DO NOTHING
            """,
            (
                cash_flow.trade_id,
                cash_flow.leg_id,
                str(cash_flow.venue),
                cash_flow.venue_event_id,
                cash_flow.venue_symbol,
                cash_flow.symbol,
                datetime_to_epoch_ms(cash_flow.timestamp),
                str(cash_flow.type),
                decimal_to_text(cash_flow.amount),
                cash_flow.asset,
                optional_decimal_to_text(cash_flow.funding_rate),
                to_canonical_json(cash_flow.raw_payload),
                datetime_to_epoch_ms(ingested_at),
            ),
        )
        if cursor.rowcount == 0:
            return None
        return int(cursor.lastrowid)

    def get_cash_flow_by_venue_event(
        self, venue: Venue, venue_event_id: str
    ) -> CashFlow | None:
        row = self._connection.execute(
            "SELECT * FROM cash_flows WHERE venue = ? AND venue_event_id = ?",
            (str(venue), venue_event_id),
        ).fetchone()
        return None if row is None else self._row_to_cash_flow(row)

    def cash_flows_for_leg(self, leg_id: int) -> list[CashFlow]:
        rows = self._connection.execute(
            "SELECT * FROM cash_flows WHERE leg_id = ? ORDER BY timestamp, id",
            (leg_id,),
        ).fetchall()
        return [self._row_to_cash_flow(row) for row in rows]

    def count_cash_flows(self) -> int:
        (count,) = self._connection.execute(
            "SELECT COUNT(*) FROM cash_flows"
        ).fetchone()
        return int(count)



    # Sync state
    # ----------
    def get_sync_state(
        self, venue: Venue, data_type: SyncDataType
    ) -> SyncState | None:
        row = self._connection.execute(
            "SELECT * FROM sync_state WHERE venue = ? AND data_type = ?",
            (str(venue), str(data_type)),
        ).fetchone()
        return None if row is None else self._row_to_sync_state(row)

    def upsert_sync_state(self, state: SyncState) -> None:
        """Create or advance a synchronisation cursor."""
        self._connection.execute(
            """
            INSERT INTO sync_state (
                venue, data_type, last_timestamp, last_external_id, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (venue, data_type) DO UPDATE SET
                last_timestamp   = excluded.last_timestamp,
                last_external_id = excluded.last_external_id,
                updated_at       = excluded.updated_at
            """,
            (
                str(state.venue),
                str(state.data_type),
                optional_datetime_to_epoch_ms(state.last_timestamp),
                state.last_external_id,
                datetime_to_epoch_ms(state.updated_at or utc_now()),
            ),
        )


    # Row conversion
    # --------------
    @staticmethod
    def _row_to_trade(row: sqlite3.Row) -> Trade:
        return Trade(
            id=int(row["id"]),
            symbol=row["symbol"],
            status=TradeStatus(row["status"]),
            reconciliation_status=ReconciliationStatus(
                row["reconciliation_status"]
            ),
            opened_at=optional_epoch_ms_to_datetime(row["opened_at"]),
            closed_at=optional_epoch_ms_to_datetime(row["closed_at"]),
            reasoning=row["reasoning"],
            actual_funding_pnl=optional_text_to_decimal(
                row["actual_funding_pnl"]
            ),
            trading_pnl=optional_text_to_decimal(row["trading_pnl"]),
            fees=optional_text_to_decimal(row["fees"]),
            slippage_cost=optional_text_to_decimal(row["slippage_cost"]),
            net_pnl=optional_text_to_decimal(row["net_pnl"]),
            alert=bool(row["alert"]),
            alert_type=row["alert_type"],
            created_at=optional_epoch_ms_to_datetime(row["created_at"]),
            updated_at=optional_epoch_ms_to_datetime(row["updated_at"]),
        )

    @staticmethod
    def _row_to_leg(row: sqlite3.Row) -> Leg:
        return Leg(
            id=int(row["id"]),
            trade_id=row["trade_id"],
            venue=Venue(row["venue"]),
            symbol=row["symbol"],
            direction=Direction(row["direction"]),
            quantity=text_to_decimal(row["quantity"]),
            opened_at=optional_epoch_ms_to_datetime(row["opened_at"]),
            closed_at=optional_epoch_ms_to_datetime(row["closed_at"]),
            average_entry_price=optional_text_to_decimal(
                row["average_entry_price"]
            ),
            average_exit_price=optional_text_to_decimal(
                row["average_exit_price"]
            ),
            status=LegStatus(row["status"]),
            created_at=optional_epoch_ms_to_datetime(row["created_at"]),
            updated_at=optional_epoch_ms_to_datetime(row["updated_at"]),
        )

    @staticmethod
    def _row_to_fill(row: sqlite3.Row) -> Fill:
        return Fill(
            id=int(row["id"]),
            leg_id=row["leg_id"],
            venue=Venue(row["venue"]),
            venue_fill_id=row["venue_fill_id"],
            venue_order_id=row["venue_order_id"],
            venue_symbol=row["venue_symbol"],
            symbol=row["symbol"],
            timestamp=optional_epoch_ms_to_datetime(row["timestamp"]),
            side=Side(row["side"]),
            price=text_to_decimal(row["price"]),
            quantity=text_to_decimal(row["quantity"]),
            fee=text_to_decimal(row["fee"]),
            fee_asset=row["fee_asset"],
            liquidity_role=LiquidityRole(row["liquidity_role"]),
            raw_payload=from_canonical_json(row["raw_payload"]),
            ingested_at=optional_epoch_ms_to_datetime(row["ingested_at"]),
        )

    @staticmethod
    def _row_to_cash_flow(row: sqlite3.Row) -> CashFlow:
        return CashFlow(
            id=int(row["id"]),
            trade_id=row["trade_id"],
            leg_id=row["leg_id"],
            venue=Venue(row["venue"]),
            venue_event_id=row["venue_event_id"],
            venue_symbol=row["venue_symbol"],
            symbol=row["symbol"],
            timestamp=optional_epoch_ms_to_datetime(row["timestamp"]),
            type=CashFlowType(row["type"]),
            amount=text_to_decimal(row["amount"]),
            asset=row["asset"],
            funding_rate=optional_text_to_decimal(row["funding_rate"]),
            raw_payload=from_canonical_json(row["raw_payload"]),
            ingested_at=optional_epoch_ms_to_datetime(row["ingested_at"]),
        )

    @staticmethod
    def _row_to_sync_state(row: sqlite3.Row) -> SyncState:
        return SyncState(
            venue=Venue(row["venue"]),
            data_type=SyncDataType(row["data_type"]),
            last_timestamp=optional_epoch_ms_to_datetime(
                row["last_timestamp"]
            ),
            last_external_id=row["last_external_id"],
            updated_at=optional_epoch_ms_to_datetime(row["updated_at"]),
        )