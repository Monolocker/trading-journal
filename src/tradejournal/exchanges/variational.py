"""Read-only file-import adapter for Variational Omni CSV exports.

Variational's trading API is officially "still in development, and is not
yet available to any users" (docs.variational.io, Technical Documentation
-> API, as of 2026-07-31). This adapter therefore reads the CSV files
that the Omni web app exports from the portfolio pages, and makes no
network request of any kind. It cannot place, cancel or modify an order,
change leverage, move funds, or sign anything, because it never talks to
the venue at all. 

**Potential to add Onchain reader; decodes settlement-pool events from 
Arbitrum One**

Source of truth for the format
------------------------------
docs.variational.io -> Technical Documentation -> Trade and Transfer
History documents two exports, both downloadable from the portfolio UI:

    trades CSV     columns: id, created_at, side, instrument_type,
                   underlying, price, qty, trade_type, status,
                   liquidation_trigger_price

    transfers CSV  columns: id, created_at, qty, asset, transfer_type,
                   status, underlying, instrument_type, fee_type,
                   funding_rate
                   (transfer_type is one of: deposit, withdrawal,
                   realized_pnl, funding, fee)

Documented export limits worth knowing: a 365-day window, at most 10,000
rows per export, and pending items excluded. Overlapping exports are safe
to import repeatedly; the database's unique constraints make ingestion
idempotent.

Where the files go
------------------
    <import_dir>/trades/*.csv       trades exports
    <import_dir>/transfers/*.csv    funding / realized PnL / transfer exports

The downloaded files can keep whatever name the browser gave them; the
subdirectory, not the filename, declares what a file contains. Any number
of files may be present in each subdirectory.

Sign conventions, applied before construction
---------------------------------------------
NormalizedCashFlow amounts are from the account's perspective: negative
means money left the account.

    deposit         stored as +qty       (qty must be positive)
    withdrawal      stored as -qty       (qty must be positive)
    fee             stored as -qty       (qty must be positive; Omni fees
                                          are deposit/withdrawal fees, as
                                          trading itself is zero-fee)
    realized_pnl    passed through signed
    funding         passed through signed -- see the caveat below

Trading fees are ZERO on this venue, and the trades CSV has no fee
column, so every fill is normalised with fee = 0. Unlike Hyperliquid,
where fees ride on the fill, any Variational fee arrives as a separate
'fee' transfer event. PnL code must respect both conventions and count
each venue's fees exactly once.

Liquidity role: Omni is an RFQ venue whose documentation describes the
user as the taker and the OLP as the maker in every trade, so fills are 
normalised as TAKER by documentation rather than by guesswork.

Unverified assumptions, checked against a real export in this milestone
-----------------------------------------------------------------------
1.  created_at format. The docs say only "timestamp". This module accepts
    a timezone-aware ISO-8601 string, or an all-digit Unix epoch in
    seconds (10 digits) or milliseconds (13 digits). A timestamp WITHOUT
    a timezone is skipped, not assumed to be UTC; if a real export turns
    out to use naive timestamps, that rule gets amended deliberately.
2.  Funding sign. Whether funding qty is signed (positive = received) is
    undocumented. It is passed through as-is; verifying one funding row
    against the UI is part of this milestone's acceptance.

Rows that cannot be handled confidently are skipped and recorded as
SkippedEvent entries rather than guessed at or silently dropped. A file
whose header is wrong, by contrast, fails the whole load: that is a
misconfiguration to fix, not a data anomaly to step around.
"""

from __future__ import annotations

import csv
import logging
import re
from collections.abc import Iterator, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from tradejournal.domain.enums import (
    CashFlowType,
    LiquidityRole,
    Side,
    Venue,
)
from tradejournal.exchanges.normalized import (
    EventParsingError,
    NormalizedCashFlow,
    NormalizedFill,
    NormalizedPosition,
    SkippedEvent,
    parse_decimal,
    parse_epoch_ms,
    parse_iso8601,
    parse_optional_decimal,
)
from tradejournal.exchanges.symbols import (
    SymbolNormalizationError,
    normalize_variational_symbol,
)

LOGGER = logging.getLogger(__name__)

TRADES_SUBDIRECTORY = "trades"
TRANSFERS_SUBDIRECTORY = "transfers"

# Documented column sets. A file missing any of these in its header is the
# wrong file or a changed export format, and either way the safe response
# is to stop rather than to import a subset of the data.
TRADES_REQUIRED_COLUMNS = frozenset(
    {
        "id",
        "created_at",
        "side",
        "instrument_type",
        "underlying",
        "price",
        "qty",
        "trade_type",
        "status",
    }
)
TRANSFERS_REQUIRED_COLUMNS = frozenset(
    {
        "id",
        "created_at",
        "qty",
        "asset",
        "transfer_type",
        "status",
    }
)

PERPETUAL_INSTRUMENT_TYPE = "perpetual_future"
CONFIRMED_STATUS = "confirmed"

# Omni settles and denominates in USDC per its documentation, and the
# trades CSV carries no fee or asset column, so fills use this constant.
SETTLEMENT_ASSET = "USDC"

TRANSFER_TYPE_TO_CASH_FLOW_TYPE: dict[str, CashFlowType] = {
    "deposit": CashFlowType.DEPOSIT,
    "withdrawal": CashFlowType.WITHDRAWAL,
    "realized_pnl": CashFlowType.REALIZED_PNL,
    "funding": CashFlowType.FUNDING,
    "fee": CashFlowType.FEE,
}

_EPOCH_SECONDS_PATTERN = re.compile(r"^\d{10}$")
_EPOCH_MILLISECONDS_PATTERN = re.compile(r"^\d{13}$")


class VariationalImportError(Exception):
    """A file-level failure: missing directory, unreadable file, or a CSV
    whose header does not match the documented export format."""


def parse_export_timestamp(
    value: object,
    *,
    field_name: str = "created_at",
    now: datetime | None = None,
) -> datetime:
    """Parse a timestamp from an Omni export without assuming its format.

    Accepted, in order of checking:
      * an all-digit string of 13 digits: Unix epoch milliseconds
      * an all-digit string of 10 digits: Unix epoch seconds
      * a timezone-aware ISO-8601 string (a trailing 'Z' is fine)

    Anything else raises EventParsingError, including a naive ISO-8601
    timestamp: assuming UTC is exactly how funding events get filed hours
    away from where they belong. Both epoch forms are range-checked by
    parse_epoch_ms, so a mislabelled unit still fails loudly.
    """
    if not isinstance(value, str):
        raise EventParsingError(
            f"{field_name}: expected a string, got {type(value).__name__}"
        )
    text = value.strip()
    if _EPOCH_MILLISECONDS_PATTERN.match(text):
        return parse_epoch_ms(int(text), field_name=field_name, now=now)
    if _EPOCH_SECONDS_PATTERN.match(text):
        return parse_epoch_ms(
            int(text) * 1000, field_name=field_name, now=now
        )
    return parse_iso8601(text, field_name=field_name, now=now)


def _blank_to_none(value: object) -> object:
    """csv.DictReader represents an empty cell as ''. Optional fields
    treat that as absent rather than as an empty value to parse."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


class VariationalFileClient:
    """Read-only source of Variational Omni history, fed by CSV exports.

    Satisfies ReadOnlyExchangeClient. There is no live view of the
    account, so fetch_open_positions returns an empty sequence and
    supports_positions is False; reconciliation must treat this venue's
    positions as unobservable rather than as flat.

    skipped_events accumulates every row this client declined to
    normalise, in file order, so that synchronisation can surface them.
    """

    venue: Venue = Venue.VARIATIONAL

    def __init__(self, import_dir: str | Path) -> None:
        self.import_dir = Path(import_dir)
        if not self.import_dir.is_dir():
            raise VariationalImportError(
                f"Variational import directory does not exist or is not a "
                f"directory: {self.import_dir}"
            )
        self.skipped_events: list[SkippedEvent] = []

    def __repr__(self) -> str:
        return f"VariationalFileClient(import_dir={str(self.import_dir)!r})"

    @property
    def supports_positions(self) -> bool:
        return False

    def fetch_open_positions(self) -> Sequence[NormalizedPosition]:
        """A CSV export has no live position view. Returning [] here means
        "cannot see", not "flat"; supports_positions carries that
        distinction to the reconciliation service."""
        return []

    # ------------------------------------------------------------------
    # Fills (trades CSV)
    # ------------------------------------------------------------------

    def fetch_fills(
        self, since: datetime | None = None
    ) -> Sequence[NormalizedFill]:
        """Return fills at or after `since`, oldest first.

        `since` is inclusive, matching the adapter contract: re-receiving
        an event is harmless because ingestion is idempotent, while
        missing one at a boundary is not.
        """
        fills: dict[str, NormalizedFill] = {}
        seen_rows: dict[str, dict[str, str]] = {}

        for path, row in self._rows(
            TRADES_SUBDIRECTORY, TRADES_REQUIRED_COLUMNS
        ):
            fill = self._to_fill(row, source=path.name)
            if fill is None:
                continue
            if not self._first_occurrence(
                "fills", fill.venue_fill_id, row, seen_rows
            ):
                continue
            fills[fill.venue_fill_id] = fill

        selected = [
            fill
            for fill in fills.values()
            if since is None or fill.timestamp >= since
        ]
        selected.sort(key=lambda fill: (fill.timestamp, fill.venue_fill_id))
        self._warn_if_empty(selected, "fills", TRADES_SUBDIRECTORY)
        return selected

    def _to_fill(
        self, row: dict[str, str], *, source: str
    ) -> NormalizedFill | None:
        event_id = (row.get("id") or "").strip()
        if not event_id:
            self._skip("fills", f"{source}: row has no id")
            return None

        status = (row.get("status") or "").strip().lower()
        if status != CONFIRMED_STATUS:
            self._skip(
                "fills",
                f"{source}: status {status!r} is not a confirmed execution",
                venue_event_id=event_id,
            )
            return None

        instrument = (row.get("instrument_type") or "").strip()
        if instrument != PERPETUAL_INSTRUMENT_TYPE:
            self._skip(
                "fills",
                f"{source}: instrument_type {instrument!r} is out of scope",
                venue_event_id=event_id,
            )
            return None

        underlying = (row.get("underlying") or "").strip()
        try:
            symbol = normalize_variational_symbol(underlying)
        except SymbolNormalizationError as error:
            self._skip(
                "fills",
                f"{source}: {error.reason}",
                venue_symbol=underlying or None,
                venue_event_id=event_id,
            )
            return None

        side_text = (row.get("side") or "").strip().lower()
        if side_text == "buy":
            side = Side.BUY
        elif side_text == "sell":
            side = Side.SELL
        else:
            self._skip(
                "fills",
                f"{source}: side {side_text!r} is neither buy nor sell",
                venue_symbol=underlying,
                venue_event_id=event_id,
            )
            return None

        try:
            timestamp = parse_export_timestamp(row.get("created_at"))
            price = parse_decimal(row.get("price"), field_name="price")
            quantity = parse_decimal(row.get("qty"), field_name="qty")
        except EventParsingError as error:
            self._skip(
                "fills",
                f"{source}: {error}",
                venue_symbol=underlying,
                venue_event_id=event_id,
            )
            return None

        if price <= 0 or quantity <= 0:
            self._skip(
                "fills",
                f"{source}: price and qty must both be positive",
                venue_symbol=underlying,
                venue_event_id=event_id,
            )
            return None

        # trade_type "liquidation" and "settlement_market_delisted" are
        # still executions that change the position, so they are ingested
        # as fills; the raw payload preserves the type for reconciliation
        # to inspect (a liquidation on one venue is exactly how a hedge
        # ends up one-legged).
        return NormalizedFill(
            venue=Venue.VARIATIONAL,
            venue_fill_id=event_id,
            venue_symbol=underlying,
            symbol=symbol,
            timestamp=timestamp,
            side=side,
            price=price,
            quantity=quantity,
            # Zero-fee venue, and the trades CSV carries no fee column.
            fee=Decimal("0"),
            fee_asset=SETTLEMENT_ASSET,
            # Documented RFQ model: the user is always the taker.
            liquidity_role=LiquidityRole.TAKER,
            raw_payload=dict(row),
            venue_order_id=None,
        )

    # ------------------------------------------------------------------
    # Cash flows (transfers CSV)
    # ------------------------------------------------------------------

    def fetch_cash_flows(
        self, since: datetime | None = None
    ) -> Sequence[NormalizedCashFlow]:
        """Return funding, realized PnL, fee and transfer events at or
        after `since`, oldest first."""
        flows: dict[str, NormalizedCashFlow] = {}
        seen_rows: dict[str, dict[str, str]] = {}

        for path, row in self._rows(
            TRANSFERS_SUBDIRECTORY, TRANSFERS_REQUIRED_COLUMNS
        ):
            flow = self._to_cash_flow(row, source=path.name)
            if flow is None:
                continue
            if flow.venue_event_id is None:
                continue
            if not self._first_occurrence(
                "cash_flows", flow.venue_event_id, row, seen_rows
            ):
                continue
            flows[flow.venue_event_id] = flow

        selected = [
            flow
            for flow in flows.values()
            if since is None or flow.timestamp >= since
        ]
        selected.sort(key=lambda flow: (flow.timestamp, flow.venue_event_id))
        self._warn_if_empty(selected, "cash flows", TRANSFERS_SUBDIRECTORY)
        return selected

    def _to_cash_flow(
        self, row: dict[str, str], *, source: str
    ) -> NormalizedCashFlow | None:
        event_id = (row.get("id") or "").strip()
        if not event_id:
            self._skip("cash_flows", f"{source}: row has no id")
            return None

        status = (row.get("status") or "").strip().lower()
        if status != CONFIRMED_STATUS:
            self._skip(
                "cash_flows",
                f"{source}: status {status!r} is not confirmed",
                venue_event_id=event_id,
            )
            return None

        transfer_type = (row.get("transfer_type") or "").strip().lower()
        cash_flow_type = TRANSFER_TYPE_TO_CASH_FLOW_TYPE.get(transfer_type)
        if cash_flow_type is None:
            self._skip(
                "cash_flows",
                f"{source}: unknown transfer_type {transfer_type!r}; "
                f"refusing to guess a category",
                venue_event_id=event_id,
            )
            return None

        asset = (row.get("asset") or "").strip() or SETTLEMENT_ASSET

        try:
            timestamp = parse_export_timestamp(row.get("created_at"))
            quantity = parse_decimal(row.get("qty"), field_name="qty")
            funding_rate = parse_optional_decimal(
                _blank_to_none(row.get("funding_rate")),
                field_name="funding_rate",
            )
        except EventParsingError as error:
            self._skip(
                "cash_flows",
                f"{source}: {error}",
                venue_event_id=event_id,
            )
            return None

        amount = self._account_perspective_amount(
            cash_flow_type, quantity, source=source, event_id=event_id
        )
        if amount is None:
            return None

        # underlying is blank for account-level events such as deposits.
        # A blank symbol is stored as None rather than invented; leg
        # assignment later flags anything it cannot place confidently.
        underlying = (row.get("underlying") or "").strip()
        symbol: str | None = None
        if underlying:
            try:
                symbol = normalize_variational_symbol(underlying)
            except SymbolNormalizationError as error:
                self._skip(
                    "cash_flows",
                    f"{source}: {error.reason}",
                    venue_symbol=underlying,
                    venue_event_id=event_id,
                )
                return None

        return NormalizedCashFlow(
            venue=Venue.VARIATIONAL,
            timestamp=timestamp,
            type=cash_flow_type,
            amount=amount,
            asset=asset,
            raw_payload=dict(row),
            venue_event_id=event_id,
            venue_symbol=underlying or None,
            symbol=symbol,
            funding_rate=funding_rate,
        )

    def _account_perspective_amount(
        self,
        cash_flow_type: CashFlowType,
        quantity: Decimal,
        *,
        source: str,
        event_id: str,
    ) -> Decimal | None:
        """Apply the module's documented sign conventions.

        Deposits, withdrawals and fees are magnitudes with a known
        direction, so a non-positive qty on any of them is an anomaly to
        review rather than a value to trust. Funding and realized PnL are
        genuinely signed and pass through unchanged.
        """
        if cash_flow_type in (
            CashFlowType.DEPOSIT,
            CashFlowType.WITHDRAWAL,
            CashFlowType.FEE,
        ):
            if quantity <= 0:
                self._skip(
                    "cash_flows",
                    f"{source}: {cash_flow_type.value} with non-positive "
                    f"qty {quantity}; expected a positive magnitude",
                    venue_event_id=event_id,
                )
                return None
            if cash_flow_type is CashFlowType.DEPOSIT:
                return quantity
            return -quantity
        return quantity

    # ------------------------------------------------------------------
    # File handling
    # ------------------------------------------------------------------

    def _rows(
        self,
        subdirectory: str,
        required_columns: frozenset[str],
    ) -> Iterator[tuple[Path, dict[str, str]]]:
        """Yield (path, row) for every data row across every CSV in a
        subdirectory, in sorted-filename then file order.

        utf-8-sig tolerates the byte-order mark that spreadsheet tools
        prepend when a CSV has been opened and re-saved.
        """
        directory = self.import_dir / subdirectory
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.csv")):
            try:
                with path.open(newline="", encoding="utf-8-sig") as handle:
                    reader = csv.DictReader(handle)
                    header = set(reader.fieldnames or [])
                    missing = required_columns - header
                    if missing:
                        raise VariationalImportError(
                            f"{path.name}: header is missing documented "
                            f"column(s) {sorted(missing)}; this does not "
                            f"look like an Omni {subdirectory} export"
                        )
                    for row in reader:
                        yield path, {
                            key: (value if isinstance(value, str) else "")
                            for key, value in row.items()
                            if key is not None
                        }
            except OSError as error:
                raise VariationalImportError(
                    f"{path.name}: could not be read: {error}"
                ) from error
            LOGGER.info(
                "variational import read file", extra={"file": path.name}
            )

    def _first_occurrence(
        self,
        data_type: str,
        event_id: str,
        row: dict[str, str],
        seen: dict[str, dict[str, str]],
    ) -> bool:
        """Track ids across the loaded files; True means keep this row.

        Overlapping export windows legitimately repeat rows, so an exact
        duplicate is dropped quietly with a note. Two DIFFERENT rows
        sharing one id are corrupt data: the first is kept, the conflict
        is recorded loudly, and nothing is guessed.
        """
        previous = seen.get(event_id)
        if previous is None:
            seen[event_id] = row
            return True
        if previous == row:
            self._skip(
                data_type,
                "duplicate row from an overlapping export window",
                venue_event_id=event_id,
            )
            return False
        self._skip(
            data_type,
            "CONFLICTING rows share one id; kept the first, review the "
            "export files",
            venue_event_id=event_id,
        )
        return False

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------

    def _skip(
        self,
        data_type: str,
        reason: str,
        *,
        venue_symbol: str | None = None,
        venue_event_id: str | None = None,
    ) -> None:
        """Record a row that could not be normalised confidently."""
        self.skipped_events.append(
            SkippedEvent(
                data_type=data_type,
                reason=reason,
                venue_symbol=venue_symbol,
                venue_event_id=venue_event_id,
            )
        )
        LOGGER.warning(
            "variational row skipped",
            extra={
                "data_type": data_type,
                "reason": reason,
                "venue_symbol": venue_symbol,
            },
        )

    def _warn_if_empty(
        self, collected: Sequence[object], label: str, subdirectory: str
    ) -> None:
        """An empty result usually means the files are not where the
        adapter looks, which is cheaper to say once than to debug."""
        if collected:
            return
        LOGGER.info(
            "variational import produced no %s; expected CSV files under "
            "%s",
            label,
            self.import_dir / subdirectory,
        )