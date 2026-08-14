"""Command-line interface: sync, rebuild, and report.

Five subcommands, each a thin wrapper over a service:

    tradejournal init       create or migrate the database
    tradejournal sync       ingest from one or both venues
    tradejournal rebuild    reconstruct legs, pair trades, value them
    tradejournal report     the summary a human actually reads
    tradejournal trades     one line per trade
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections import Counter
from decimal import Decimal
from typing import Sequence

from tradejournal import __version__
from tradejournal.config import ConfigError, Settings, load_settings
from tradejournal.db.connection import apply_migrations, connect
from tradejournal.db.repository import Repository
from tradejournal.domain.enums import (
    CashFlowType,
    ReconciliationStatus,
    TradeStatus,
)
from tradejournal.exchanges.hyperliquid import HyperliquidClient
from tradejournal.exchanges.variational import VariationalFileClient
from tradejournal.logging_setup import configure_logging
from tradejournal.services.pnl import PnLService
from tradejournal.services.reconciliation import ReconciliationService
from tradejournal.services.sync import SyncService

LOGGER = logging.getLogger(__name__)

_ZERO = Decimal(0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tradejournal",
        description=(
            "Read-only journal for delta-neutral perpetual positions. "
            "Never places, cancels, or modifies orders."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--database",
        help="override TJ_DATABASE_PATH for this run",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress diagnostics on stderr; reports still print",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "init", help="create the database and apply any pending migrations"
    )

    sync_parser = subparsers.add_parser(
        "sync", help="ingest fills and cash flows from the venues"
    )
    sync_parser.add_argument(
        "--venue",
        choices=["hyperliquid", "variational", "all"],
        default="all",
        help="which venue to sync (default: all)",
    )

    subparsers.add_parser(
        "rebuild",
        help="rebuild legs and trades from fills, then value them",
    )

    refresh_parser = subparsers.add_parser(
        "refresh",
        help="sync, rebuild, and report in one pass (the usual command)",
    )
    refresh_parser.add_argument(
        "--venue",
        choices=["hyperliquid", "variational", "all"],
        default="all",
        help="which venue to sync (default: all)",
    )
    refresh_parser.add_argument(
        "--no-report",
        action="store_true",
        help="sync and rebuild without printing the summary",
    )

    subparsers.add_parser(
        "report", help="print the journal summary"
    )

    trades_parser = subparsers.add_parser(
        "trades", help="list trades, one per line"
    )
    trades_parser.add_argument(
        "--status",
        choices=["open", "closed", "all"],
        default="all",
        help="filter by trade status (default: all)",
    )
    trades_parser.add_argument(
        "--review",
        action="store_true",
        help="only trades flagged for review",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    if args.database:
        settings = _with_database(settings, args.database)
    configure_logging(settings.log_level, quiet=args.quiet)

    handlers = {
        "init": _command_init,
        "sync": _command_sync,
        "rebuild": _command_rebuild,
        "refresh": _command_refresh,
        "report": _command_report,
        "trades": _command_trades,
    }
    try:
        return handlers[args.command](args, settings)
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    except FileNotFoundError as error:
        print(f"not found: {error}", file=sys.stderr)
        return 2


def _with_database(settings: Settings, database: str) -> Settings:
    from pathlib import Path

    return Settings(
        database_path=Path(database),
        hyperliquid_account_address=settings.hyperliquid_account_address,
        hyperliquid_info_url=settings.hyperliquid_info_url,
        variational_import_dir=settings.variational_import_dir,
        http_timeout_seconds=settings.http_timeout_seconds,
        http_max_retries=settings.http_max_retries,
        log_level=settings.log_level,
    )


def _open(settings: Settings) -> sqlite3.Connection:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(settings.database_path)
    apply_migrations(connection)
    return connection


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------


def _command_init(args: argparse.Namespace, settings: Settings) -> int:
    connection = _open(settings)
    connection.close()
    print(f"database ready at {settings.database_path}")
    return 0


def _command_sync(args: argparse.Namespace, settings: Settings) -> int:
    connection = _open(settings)
    code = _sync_into(connection, args.venue, settings)
    connection.close()
    return code


def _sync_into(
    connection: sqlite3.Connection, wanted: str, settings: Settings
) -> int:
    """Ingest from the requested venues into an already-open connection.

    Separated from the command so `refresh` can sync and rebuild against
    one connection rather than opening the database twice.
    """
    service = SyncService(connection)

    if wanted in ("variational", "all"):
        directory = settings.variational_import_dir
        if not directory.is_dir():
            message = (
                f"Variational import directory not found: {directory}"
            )
            if wanted == "variational":
                print(message, file=sys.stderr)
                return 2
            LOGGER.warning("%s; skipping Variational", message)
        else:
            client = VariationalFileClient(directory)
            for report in service.sync(client):
                _print_sync(report)

    if wanted in ("hyperliquid", "all"):
        address = settings.hyperliquid_account_address
        if not address:
            if wanted == "hyperliquid":
                settings.require_hyperliquid_address()
            LOGGER.warning(
                "TJ_HYPERLIQUID_ACCOUNT_ADDRESS is not set; "
                "skipping Hyperliquid"
            )
        else:
            client = HyperliquidClient(
                address,
                info_url=settings.hyperliquid_info_url,
                timeout_seconds=settings.http_timeout_seconds,
                max_retries=settings.http_max_retries,
            )
            for report in service.sync(client):
                _print_sync(report)

    return 0


def _print_sync(report: object) -> None:
    print(
        f"{report.venue} {report.data_type}: "  # type: ignore[attr-defined]
        f"fetched={report.fetched} "  # type: ignore[attr-defined]
        f"inserted={report.inserted} "  # type: ignore[attr-defined]
        f"duplicates={report.duplicates} "  # type: ignore[attr-defined]
        f"skipped={len(report.skipped)}"  # type: ignore[attr-defined]
    )


def _command_rebuild(args: argparse.Namespace, settings: Settings) -> int:
    connection = _open(settings)
    code = _rebuild_into(connection)
    connection.close()
    return code


def _rebuild_into(connection: sqlite3.Connection) -> int:
    """Rebuild derived tables and value them on an open connection."""
    rebuild = ReconciliationService(connection).rebuild()
    print(
        f"legs: {rebuild.legs_built} "
        f"(open {rebuild.legs_open}, closed {rebuild.legs_closed})"
    )
    print(
        f"trades: {rebuild.trades_created} "
        f"(paired legs {rebuild.legs_paired}, "
        f"unpaired {rebuild.legs_unpaired})"
    )
    valuation = PnLService(connection).recompute()
    print(
        f"cash flows: {valuation.cash_flows_attributed} attributed, "
        f"{valuation.cash_flows_account_level} account-level, "
        f"{valuation.cash_flows_unattributed} unattributed"
    )
    findings = Counter(
        f.kind for f in (*rebuild.findings, *valuation.findings)
    )
    if findings:
        print("findings:")
        for kind, count in findings.most_common():
            print(f"  {count:>5}  {kind}")
    return 0


def _command_refresh(args: argparse.Namespace, settings: Settings) -> int:
    """Sync, rebuild, and report in one pass.

    This exists because the three-step sequence has a silent failure
    mode: syncing without rebuilding leaves the derived tables stale, so
    `report` prints yesterday's answer with today's confidence and
    nothing on screen says so. One command removes the chance to forget.

    A sync failure stops the run before rebuilding. Reporting figures
    derived from a half-ingested window would be worse than reporting
    nothing, and the previous rebuild's numbers stay untouched and
    still true as of their own sync.
    """
    connection = _open(settings)
    code = _sync_into(connection, args.venue, settings)
    if code != 0:
        connection.close()
        return code
    print()
    code = _rebuild_into(connection)
    connection.close()
    if code != 0:
        return code
    if args.no_report:
        return 0
    print()
    return _command_report(args, settings)


def _command_report(args: argparse.Namespace, settings: Settings) -> int:
    connection = _open(settings)
    repository = Repository(connection)

    trades = repository.all_trades()
    legs = repository.all_legs()
    unattributed = repository.unattributed_cash_flows()

    print("TRADE JOURNAL")
    print("=" * 52)
    print(f"database        {settings.database_path}")
    print(
        f"fills           {repository.count_fills()}   "
        f"cash flows {repository.count_cash_flows()}"
    )
    print(
        f"legs            {len(legs)}   "
        f"unpaired {len(repository.unpaired_legs())}"
    )
    print()

    # A fill with no leg has never been through a rebuild, so every
    # figure below predates it. Saying so is the difference between a
    # stale number and a wrong one: the reader can tell which they have.
    stale = repository.unassigned_fills()
    if stale:
        print("OUT OF DATE")
        print(
            f"  {len(stale)} fills have been synced but not reconciled, "
            f"so the figures below exclude them."
        )
        print("  Run `tradejournal refresh` (or `rebuild`) first.")
        print()

    if not trades:
        print("No trades yet. Run `tradejournal refresh`.")
        connection.close()
        return 0

    closed = [t for t in trades if t.status is TradeStatus.CLOSED]
    review = [
        t
        for t in trades
        if t.reconciliation_status is ReconciliationStatus.REVIEW_REQUIRED
    ]
    funding = _total(t.actual_funding_pnl for t in trades)
    trading = _total(t.trading_pnl for t in trades)
    fees = _total(t.fees for t in trades)
    net = _total(t.net_pnl for t in trades)

    print("TRADES")
    print(f"  total         {len(trades)}")
    print(f"  closed        {len(closed)}")
    print(f"  open          {len(trades) - len(closed)}")
    print(f"  need review   {len(review)}")
    print()
    print("PROFIT AND LOSS (attributed)")
    print(f"  funding       {_money(funding)}")
    print(f"  trading       {_money(trading)}")
    print(f"  fees         -{_money(fees)}")
    print(f"  net           {_money(net)}")
    print()

    orphan_funding = [
        flow for flow in unattributed if flow.type is CashFlowType.FUNDING
    ]
    if orphan_funding:
        missing = _total(flow.amount for flow in orphan_funding)
        print("NOT INCLUDED ABOVE")
        print(
            f"  {len(orphan_funding)} funding events "
            f"({_money(missing)}) belong to no trade, so the funding and "
            f"net figures above are incomplete."
        )
        print(
            "  Run `tradejournal rebuild` after a full sync; if they "
            "persist, inspect them before trusting the totals."
        )
        print()

    print("BY VENUE (legs)")
    per_venue = Counter(str(leg.venue) for leg in legs)
    for venue, count in sorted(per_venue.items()):
        print(f"  {venue:<14}{count}")
    connection.close()
    return 0


def _command_trades(args: argparse.Namespace, settings: Settings) -> int:
    connection = _open(settings)
    repository = Repository(connection)
    trades = repository.all_trades()

    if args.status != "all":
        wanted = (
            TradeStatus.OPEN if args.status == "open" else TradeStatus.CLOSED
        )
        trades = [t for t in trades if t.status is wanted]
    if args.review:
        trades = [
            t
            for t in trades
            if t.reconciliation_status
            is ReconciliationStatus.REVIEW_REQUIRED
        ]

    if not trades:
        print("no trades match")
        connection.close()
        return 0

    print(
        f"{'OPENED':<20} {'SYMBOL':<16} {'STATUS':<7} "
        f"{'FUNDING':>12} {'NET':>12}  REVIEW"
    )
    for trade in trades:
        opened = trade.opened_at.strftime("%Y-%m-%d %H:%M")
        flag = (
            "yes"
            if trade.reconciliation_status
            is ReconciliationStatus.REVIEW_REQUIRED
            else ""
        )
        print(
            f"{opened:<20} {trade.symbol:<16} {str(trade.status):<7} "
            f"{_money(trade.actual_funding_pnl):>12} "
            f"{_money(trade.net_pnl):>12}  {flag}"
        )
    connection.close()
    return 0


# ----------------------------------------------------------------------
# Formatting
# ----------------------------------------------------------------------


def _total(values) -> Decimal:
    return sum((v for v in values if v is not None), _ZERO)


def _money(value: Decimal | None) -> str:
    """Two decimal places, or a dash when the value was never computed.

    A dash and a zero mean different things — "not calculated" versus
    "calculated as nothing" — and the report must not blur them.
    """
    if value is None:
        return "-"
    return f"{value.quantize(Decimal('0.01')):,}"


if __name__ == "__main__":
    raise SystemExit(main())