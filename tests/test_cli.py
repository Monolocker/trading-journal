"""Tests for configuration, logging setup, and the command-line interface.

Every command runs against a temporary database via --database, so no
test touches the developer's real journal, and none reaches the network:
the only sync exercised is the file-import venue pointed at a tmp_path.
Assertions are made on captured stdout, on exit codes, and on the
stdout/stderr split the CLI promises.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tradejournal.cli import main
from tradejournal.config import (
    ConfigError,
    PLACEHOLDER_ADDRESS,
    Settings,
    load_settings,
)
from tradejournal.db.connection import apply_migrations, connect
from tradejournal.db.repository import Repository
from tradejournal.domain.enums import (
    CashFlowType,
    LiquidityRole,
    Side,
    Venue,
)
from tradejournal.domain.models import CashFlow, Fill

BASE_TIME = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

_COUNTER = iter(range(1, 10_000))

TJ_VARIABLES = (
    "TJ_DATABASE_PATH",
    "TJ_HYPERLIQUID_ACCOUNT_ADDRESS",
    "TJ_HYPERLIQUID_INFO_URL",
    "TJ_VARIATIONAL_IMPORT_DIR",
    "TJ_HTTP_TIMEOUT_SECONDS",
    "TJ_HTTP_MAX_RETRIES",
    "TJ_LOG_LEVEL",
)

@pytest.fixture(autouse=True)
def isolated_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cut every CLI test off from the developer's real configuration.

    load_settings() seeds from ./.env relative to the working directory,
    so a test that merely deletes an environment variable gets it handed
    straight back by the real .env sitting in the project root — and a
    test meant to assert "no address configured" would instead reach the
    live network using the developer's own account. Running each test in
    an empty temporary directory removes the file from reach, and
    clearing the variables removes anything exported by the shell.

    Without this, the suite passes or fails depending on whose machine
    it runs on, which makes it worthless as a check.
    """
    monkeypatch.chdir(tmp_path)
    for name in TJ_VARIABLES:
        monkeypatch.delenv(name, raising=False)


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------


def test_settings_fall_back_to_documented_defaults() -> None:
    settings = load_settings({})
    assert settings.database_path == Path("./data/tradejournal.db")
    assert settings.hyperliquid_account_address is None
    assert settings.http_max_retries == 3
    assert settings.log_level == "INFO"


def test_placeholder_address_is_treated_as_unset() -> None:
    """The shipped .env.example address must never reach the network."""
    settings = load_settings(
        {"TJ_HYPERLIQUID_ACCOUNT_ADDRESS": PLACEHOLDER_ADDRESS}
    )
    assert settings.hyperliquid_account_address is None
    with pytest.raises(ConfigError, match="PUBLIC master account"):
        settings.require_hyperliquid_address()


def test_malformed_numeric_setting_is_rejected_loudly() -> None:
    with pytest.raises(ConfigError, match="TJ_HTTP_TIMEOUT_SECONDS"):
        load_settings({"TJ_HTTP_TIMEOUT_SECONDS": "soon"})


def test_real_address_is_kept() -> None:
    address = "0x" + "a1" * 20
    settings = load_settings(
        {"TJ_HYPERLIQUID_ACCOUNT_ADDRESS": address}
    )
    assert settings.require_hyperliquid_address() == address


def test_unknown_log_level_falls_back_without_raising() -> None:
    from tradejournal.logging_setup import configure_logging

    configure_logging("NONSENSE")
    assert logging.getLogger().level == logging.INFO


def test_logging_goes_to_stderr_not_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reports are stdout; diagnostics are stderr. Redirecting a report
    must not swallow the findings."""
    from tradejournal.logging_setup import configure_logging

    configure_logging("INFO")
    logging.getLogger("tradejournal.test").warning("a finding")
    captured = capsys.readouterr()
    assert "a finding" in captured.err
    assert captured.out == ""


# ----------------------------------------------------------------------
# Fixtures for CLI runs
# ----------------------------------------------------------------------


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "journal.db"


def make_fill(
    *,
    venue: Venue,
    side: Side,
    price: str,
    quantity: str,
    minutes: int,
    symbol: str = "BTC-PERP",
) -> Fill:
    fill_id = f"cli-fill-{next(_COUNTER)}"
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


def seed_hedged_trade(database: Path, *, orphan_funding: bool = False) -> None:
    connection = connect(database)
    apply_migrations(connection)
    repository = Repository(connection)
    fills = [
        make_fill(
            venue=Venue.HYPERLIQUID, side=Side.BUY, price="100",
            quantity="1", minutes=0,
        ),
        make_fill(
            venue=Venue.VARIATIONAL, side=Side.SELL, price="100",
            quantity="1", minutes=1,
        ),
        make_fill(
            venue=Venue.HYPERLIQUID, side=Side.SELL, price="104",
            quantity="1", minutes=600,
        ),
        make_fill(
            venue=Venue.VARIATIONAL, side=Side.BUY, price="103",
            quantity="1", minutes=601,
        ),
    ]
    for fill in fills:
        repository.insert_fill(fill)

    flows = [
        CashFlow(
            venue=Venue.HYPERLIQUID,
            venue_event_id=f"cli-flow-{next(_COUNTER)}",
            venue_symbol="BTC",
            symbol="BTC-PERP",
            timestamp=BASE_TIME + timedelta(minutes=100),
            type=CashFlowType.FUNDING,
            amount=Decimal("2.00"),
            asset="USDC",
            raw_payload={},
        )
    ]
    if orphan_funding:
        flows.append(
            CashFlow(
                venue=Venue.HYPERLIQUID,
                venue_event_id=f"cli-flow-{next(_COUNTER)}",
                venue_symbol="BTC",
                symbol="BTC-PERP",
                # Long after both legs closed: belongs to no trade.
                timestamp=BASE_TIME + timedelta(days=30),
                type=CashFlowType.FUNDING,
                amount=Decimal("0.75"),
                asset="USDC",
                raw_payload={},
            )
        )
    for flow in flows:
        repository.insert_cash_flow(flow)
    connection.close()


def run(database: Path, *args: str) -> int:
    return main(["--database", str(database), "--quiet", *args])


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------


def test_init_creates_the_database(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(database, "init") == 0
    assert database.exists()
    assert "database ready" in capsys.readouterr().out


def test_init_creates_missing_parent_directories(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "deeper" / "journal.db"
    assert main(["--database", str(nested), "--quiet", "init"]) == 0
    assert nested.exists()


def test_report_on_an_empty_journal_says_so(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(database, "init")
    assert run(database, "report") == 0
    out = capsys.readouterr().out
    assert "No trades yet" in out
    # No invented numbers on an empty journal.
    assert "PROFIT AND LOSS" not in out


def test_rebuild_then_report_shows_the_trade(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_hedged_trade(database)
    assert run(database, "rebuild") == 0
    capsys.readouterr()

    assert run(database, "report") == 0
    out = capsys.readouterr().out
    assert "TRADES" in out
    assert "PROFIT AND LOSS" in out
    # funding 2.00 + trading 1.00 - fees 0.04 = 2.96
    assert "2.96" in out
    assert "NOT INCLUDED ABOVE" not in out


def test_report_names_unattributed_funding_instead_of_hiding_it(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A summary that showed only attributed funding would understate
    the result while looking complete."""
    seed_hedged_trade(database, orphan_funding=True)
    run(database, "rebuild")
    capsys.readouterr()

    assert run(database, "report") == 0
    out = capsys.readouterr().out
    assert "NOT INCLUDED ABOVE" in out
    assert "1 funding events" in out
    assert "0.75" in out


def test_rebuild_prints_findings_summary(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_hedged_trade(database, orphan_funding=True)
    assert run(database, "rebuild") == 0
    out = capsys.readouterr().out
    assert "legs:" in out
    assert "trades:" in out
    assert "unattributed_cash_flow" in out


def test_trades_lists_one_line_per_trade(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_hedged_trade(database)
    run(database, "rebuild")
    capsys.readouterr()

    assert run(database, "trades") == 0
    out = capsys.readouterr().out
    assert "SYMBOL" in out
    assert "BTC-PERP" in out


def test_trades_status_filter_excludes_non_matching(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_hedged_trade(database)
    run(database, "rebuild")
    capsys.readouterr()

    assert run(database, "trades", "--status", "open") == 0
    assert "no trades match" in capsys.readouterr().out

    assert run(database, "trades", "--status", "closed") == 0
    assert "BTC-PERP" in capsys.readouterr().out


def test_trades_review_filter(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_hedged_trade(database)
    run(database, "rebuild")
    capsys.readouterr()
    # This trade is clean, so the review filter must exclude it.
    assert run(database, "trades", "--review") == 0
    assert "no trades match" in capsys.readouterr().out


def test_sync_variational_reads_the_import_directory(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import_dir = tmp_path / "variational"
    (import_dir / "trades").mkdir(parents=True)
    (import_dir / "transfers").mkdir(parents=True)
    (import_dir / "trades" / "t.csv").write_text(
        "id,created_at,side,instrument_type,underlying,price,qty,"
        "trade_type,status,liquidation_trigger_price\n"
        "t-1,2026-07-01T12:00:00Z,buy,perpetual_future,BTC,100,1,"
        "trade,confirmed,\n",
        encoding="utf-8",
    )
    (import_dir / "transfers" / "x.csv").write_text(
        "id,created_at,qty,asset,transfer_type,status,underlying,"
        "instrument_type,fee_type,funding_rate\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TJ_VARIATIONAL_IMPORT_DIR", str(import_dir))
    monkeypatch.delenv("TJ_HYPERLIQUID_ACCOUNT_ADDRESS", raising=False)

    assert run(database, "sync", "--venue", "variational") == 0
    out = capsys.readouterr().out
    assert "inserted=1" in out


def test_sync_variational_missing_directory_fails_clearly(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(
        "TJ_VARIATIONAL_IMPORT_DIR", str(tmp_path / "nope")
    )
    assert run(database, "sync", "--venue", "variational") == 2
    assert "not found" in capsys.readouterr().err


def test_sync_hyperliquid_without_address_is_a_config_error(
    database: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Never guess an address: an empty journal that looks successful is
    worse than a refusal."""
    monkeypatch.delenv("TJ_HYPERLIQUID_ACCOUNT_ADDRESS", raising=False)
    assert run(database, "sync", "--venue", "hyperliquid") == 2
    assert "configuration error" in capsys.readouterr().err


def test_unknown_command_exits_nonzero(database: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--database", str(database), "nonsense"])
    assert excinfo.value.code != 0


def test_money_formatting_distinguishes_none_from_zero() -> None:
    from tradejournal.cli import _money

    assert _money(None) == "-"
    assert _money(Decimal("0")) == "0.00"