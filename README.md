# tradejournal
A read-only journal for delta-neutral perp positions across Hyperliquid and Variational Omni. It ingests execution history from both venues, reconstructs positions from fills, pairs two sides of each hedge, and reports what strategy actually earned (or lost). It is a record-keeper, not a trading system.

## Safety Precautions 
This package never places, cancels, or modifies orders. Additionally, it never changes leverage, never transfers or withdraws funds, and never signs a transaction.
- Hyperliquid is read through the public `info` endpoint using a **public** account address. No private key or API secret is required or read, though this can change.
- Due to the current limitations of Variational's early-stage read-only API, operations on this venue are read from CSV files you export yourself from the Omni UI. No private API is contacted; no on-chain settlement data is decoded. Decoding on-chain settlement data may be added in the future.

## Status
Milestones 0-9 are complete: schema, event normalization, both venue adapters (clients), idempotent sync, leg reconstruction, trade pairing, PnL attribution, and the CLI.
```289 passed, 1 skipped```
The skipped test is a live Hyperliquid integration test, disabled by default and enabled deliberately with `TJ_ENABLE_LIVE_TESTS=1`

One known correctness gap is open and documented below: **daily funding buckets** (_see Known Limitations_). It is understood, diagnosed, and not yet resolved.

## Requirements
- Python 3.12+ 
- pip package manager
- Git (for cloning the repo)
- SQLite. No install needed. Python's bundled sqlite3 module provides it

## Python Dependencies 
```
# Core dependencies 
requests>=2.32          # live HL reads only; imported lazily

# Development / testing
pytest>=8.0
```

## Exchange Requirements
### Hyperliquid
- Public master account address (42-character hex) with access to Hyperliquid, set as `TJ_HYPERLIQUID_ACCOUNT_ADDRESS` 
- Perpetual trading enabled 
- Adequate amount of USDC

### Variational Omni
- Access to Variational Omni via code 
- Adequate amount of USDC

## Setup
```
git clone https://github.com/Monolocker/trading-journal.git
cd trading-journal
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```
Then:
- Edit `.env` and set `TJ_HYPERLIQUID_ACCOUNT_ADDRESS` to your public master address. The placeholder shipped in the example file is treated as unset, so an unedited `.env` fails loudly rather than reporting an empty account as though it were real.
- Drop your Variational exports into `data/variational/trades/` and `data/variational/transfers/`
- Verify the install: `python3 -m pytest -q`
- Run it: `tradejournal refesh`

## Development / Architecture
```
python3 -m pytest -q          # full suite
python3 -m pytest tests/test_pnl.py -q 
```
```
src/tradejournal/
     cli.py               # commands: init, sync, 
     config.py            # environment-backed settings, .env seeding
     logging_setup.py     # stderr diagnostics
trades
     db/                  # connections, migrations, repository
     domain/              # enums and models
     exchanges/           # exchange clients, normalization, symbol rules
     services/            # sync, reconciliation, pnl
tests/                    # one module per unit above
```
Tests never touch the network (the one live test is opt-in) and never touch your real journal. CLI tests run in a temporary directory with `TJ_*` variables cleared, so a stray `.env` cannot leak into a test run.

**Adding a venue** means writing an adapter that satisfies the `ReadOnlyExchangeClient` protocol (_potential modularity for this component to be added_). 

## Commands 
| Command | What It Does | 
| --- | --- |
| `tradejournal init` | Create the database and apply migrations |
| `tradejournal sync [--venue X]` | Ingest fills and cash flows | 
| `tradejournal rebuild` | Rebuild legs and trades from fills, then value them |
| `tradejournal refresh [--venue X] [--no-report]` | Sync, rebuild, and report in one pass | 
| `tradejournal report` | Print the journal summary |
| `tradejournal trades [--status open/closed] [--review]` | One line per trade |

Global flags: `--quiet` (silence diagnostics), `--version`.

## How it Works
```mermaid
flowchart TD
     A[Venue adapters] --> B[Normalized events];
     B --> C[SyncService];
     C --> D[(SQLite fills, cash_flows)];
     D --> E[ReconciliationService];
     E --> F[PnLService];
     F --> G[CLI report]:
```

**Adapters** (`exchanges/`) translate each venue's format into the same normalized events for tradejournal readability. `HyperliquidClient` reads the public `info` endpoint, while `VariationalClient` reads exported CSVs (manual step). Both satisfy the `ReadOnlyExchangeClient` protocol, so everything downstream is venue agnostic.

**Symbol Normalization** (`exchanges/symbols.py`) is the boundary where venue symbols become canonical ones. HIP-3 markets keep their deployer namespace (`xyz: AAPL-PERP`). This makes it safe to pair across venues without merging distinct markets.

**Sync Service** is the only path from adapters into the database. This method is idempotent by the following three mechanisms:
- Unique constraints on `(venue, venue_fill_id)` and `(venue, venue_event_id)`
- An inclusive cursor (the boundary event is re-fetched and deduplicated rather than risked)
- Refusal of any cash flow without a venue event id
Each stream syncs in one transaction covering both inserts and the cursor, so the cursor cannot advance past events that were not confidently stored.
(_potential to move away from cursor resume and employ set-based UPDATE operation_)

**ReconciliationService** replays fills per `(venue, symbol)` tracking the signed position. A leg opens when the position leaves zero and closes when it returns to exactly zero (Decimal - no epsilon). Leg quantity is the peak absolute position; entry and exit prices are side-wise VWAPs. Trades pair cross-venue on `base_asset()` equality, opposite directions, and genuinely overlapping time windows.

**PnLService** attaches cash flows to the leg whose window contains them, then computes each trade's funding PnL, realised trading PnL, fees, and net.

## Findings
When an event or flow cannot be normalized or attributed with confidence, it is recorded and surfaced. The following represent this illustration:
| Finding | Meaning | 
| --- | --- | 
| `position_flip` | A fill crossed through zero; the crossing fill stays with the closing leg and the new leg opens without an entry fill. Affected trades are flagged for review | 
| `quantity_mismatch` | Paired legs have different quantites. The trade still pairs, flagged for review | 
| `unpaired_leg` | A leg with no counterpart on the other venue. Usually represents a directional trade | 
| `unattributed_cash_flow` | A symbol-bearing cash flow that no leg's window covers | 
| `unambiguous_attribution` | A flow fallling in more than one leg window; attributed to the closing leg | 

## Disclaimer
This software is experimental and provided for **educational purposes only**. This is **not financial advice**. Trading cryptocurrency perpetuals carries substantial risk, including the total loss of funds. Use entirely at your own risk. Never invest more than you can afford to lose. 
