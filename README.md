# tradejournal
A read-only journal for delta-neutral perp future positions across Hyperliquid and Variational Omni. It ingests execution history from both venues, reconstructs positions from fills, pairs two sides of each hedge, and reports what strategy actually earned (or lost). It is a record-keeper, not a trading system.

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

### Variational Omni
- Access to Variational Omni via code 

## Setup
```
git clone https://github.com/Monolocker/trading-journal.git
cd trading-journal
python3 -3 venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```
Then:
- Edit `.env` and set `TJ_HYPERLIQUID_ACCOUNT_ADDRESS` to your public master address. The placeholder shipped in the example file is treated as unset, so an unedited `.env` fails loudly rather than reporting an empty account as though it were real.
- Drop your Variational exports into `data/variational/trades/` and `data/variational/transfers/`
- Verify the install: `python3 -m pytest -q`
- Run it: `tradejournal refesh`

## Commands 
| Command | What It Does | 
| --- | --- |
| `tradejournal init` | Create the database and apply migrations |
| `tradejournal sync [--venue X]` | Ingest fills and cash flows | 
| `tradejournal rebuild` | Rebuild legs and trades from fills, then value them |
| `tradejournal refresh [--venue X] [--no-report]` | Sync, rebuild, and report in one pass | 
| `tradejournal report` | Print the journal summary |
| `tradejournal trades [--status open/closed] [--review]` | One line per trade |

## How it Works
venue adapters -> normalized events -> SyncService -> SQLite (fills, cash_flows)
                                                            |
                                                            v
                                              ReconciliationService
                                                  (legs, trades)
                                                            |
                                                            v
                                                       PnLService
                                            (attribution, valuation)
                                                            |
                                                            v
                                                       CLI report