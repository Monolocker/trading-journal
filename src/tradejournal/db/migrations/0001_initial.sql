-- Initial schema for the read-only trade journal.
--
-- Storage conventions:
--   * Money and quantities are TEXT in fixed-point decimal notation.
--     Never REAL. Do not SUM() or ORDER BY these columns; they sort
--     lexicographically. Aggregate with Decimal in Python instead.
--   * Timestamps are INTEGER milliseconds since the Unix epoch, UTC.
--   * Raw payloads are TEXT containing canonical JSON.
--   * Enum columns are lowercase TEXT guarded by CHECK constraints that
--     mirror the StrEnum members in domain/enums.py.
--
-- This file wraps itself in a transaction so that a partial failure leaves
-- the database untouched.

BEGIN TRANSACTION;


-- trades: the complete delta-neutral strategy position
CREATE TABLE trades (
    id                    INTEGER PRIMARY KEY,
    symbol                TEXT    NOT NULL,
    status                TEXT    NOT NULL,
    reconciliation_status TEXT    NOT NULL DEFAULT 'ok',
    opened_at             INTEGER,
    closed_at             INTEGER,
    reasoning             TEXT,
    actual_funding_pnl    TEXT,
    trading_pnl           TEXT,
    fees                  TEXT,
    slippage_cost         TEXT,
    net_pnl               TEXT,
    alert                 INTEGER NOT NULL DEFAULT 0,
    alert_type            TEXT,
    created_at            INTEGER NOT NULL,
    updated_at            INTEGER NOT NULL,

    CHECK (status IN ('open', 'closed', 'unresolved')),
    CHECK (reconciliation_status IN ('ok', 'review_required', 'unresolved')),
    CHECK (alert IN (0, 1))
);


-- legs: one record per venue side
--
-- trade_id is nullable on purpose. A leg that has not been confidently
-- paired with its hedge must still be storable, because an unpaired leg is
-- itself a reconciliation finding.
CREATE TABLE legs (
    id                  INTEGER PRIMARY KEY,
    trade_id            INTEGER REFERENCES trades (id) ON DELETE CASCADE,
    venue               TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    direction           TEXT    NOT NULL,
    quantity            TEXT    NOT NULL,
    opened_at           INTEGER,
    closed_at           INTEGER,
    average_entry_price TEXT,
    average_exit_price  TEXT,
    status              TEXT    NOT NULL,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,

    CHECK (venue IN ('hyperliquid', 'variational')),
    CHECK (direction IN ('long', 'short')),
    CHECK (status IN ('open', 'closed'))
);


-- fills: immutable execution records
--
-- UNIQUE (venue, venue_fill_id) is what makes repeated synchronisation
-- idempotent: re-importing an overlapping window cannot duplicate a fill.
--
-- leg_id uses ON DELETE SET NULL rather than CASCADE. Fills are immutable
-- facts reported by the venue, rebuilding legs must never destroy them.
CREATE TABLE fills (
    id             INTEGER PRIMARY KEY,
    leg_id         INTEGER REFERENCES legs (id) ON DELETE SET NULL,
    venue          TEXT    NOT NULL,
    venue_fill_id  TEXT    NOT NULL,
    venue_order_id TEXT,
    venue_symbol   TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    timestamp      INTEGER NOT NULL,
    side           TEXT    NOT NULL,
    price          TEXT    NOT NULL,
    quantity       TEXT    NOT NULL,
    fee            TEXT    NOT NULL,
    fee_asset      TEXT    NOT NULL,
    liquidity_role TEXT    NOT NULL,
    raw_payload    TEXT    NOT NULL,
    ingested_at    INTEGER NOT NULL,

    UNIQUE (venue, venue_fill_id),
    CHECK (venue IN ('hyperliquid', 'variational')),
    CHECK (side IN ('buy', 'sell')),
    CHECK (liquidity_role IN ('maker', 'taker', 'unknown'))
);


-- cash_flows: funding, fees, rebates, refunds and similar events
--
-- Sign convention: amounts are from the account's perspective. Funding
-- received and rebates are positive; funding paid and fees are negative.
--
-- venue_event_id is nullable because not every venue supplies a stable
-- identifier. For example, HL funding entries carry an all-zero
-- hash. The uniqueness rule is therefore a partial index that applies only
-- when an identifier exists.
CREATE TABLE cash_flows (
    id             INTEGER PRIMARY KEY,
    trade_id       INTEGER REFERENCES trades (id) ON DELETE SET NULL,
    leg_id         INTEGER REFERENCES legs (id) ON DELETE SET NULL,
    venue          TEXT    NOT NULL,
    venue_event_id TEXT,
    venue_symbol   TEXT,
    symbol         TEXT,
    timestamp      INTEGER NOT NULL,
    type           TEXT    NOT NULL,
    amount         TEXT    NOT NULL,
    asset          TEXT    NOT NULL,
    funding_rate   TEXT,
    raw_payload    TEXT    NOT NULL,
    ingested_at    INTEGER NOT NULL,

    CHECK (venue IN ('hyperliquid', 'variational')),
    CHECK (type IN (
        'funding', 'fee', 'rebate', 'refund', 'realized_pnl',
        'deposit', 'withdrawal', 'transfer', 'liquidation', 'other'
    ))
);

CREATE UNIQUE INDEX idx_cash_flows_venue_event
    ON cash_flows (venue, venue_event_id)
    WHERE venue_event_id IS NOT NULL;


-- sync_state: resumable synchronisation cursors
-- potential to move away from cursors to another alternative 
-- such as set-based operation
CREATE TABLE sync_state (
    venue            TEXT    NOT NULL,
    data_type        TEXT    NOT NULL,
    last_timestamp   INTEGER,
    last_external_id TEXT,
    updated_at       INTEGER NOT NULL,

    PRIMARY KEY (venue, data_type),
    CHECK (venue IN ('hyperliquid', 'variational')),
    CHECK (data_type IN ('fills', 'cash_flows'))
);


-- Indexes:
-- Each index below serves a query the application actually performs.
-- The UNIQUE constraints above already provide their own indexes.
-- 
-- Incremental sync: fetch a venue's events after a cursor timestamp.
CREATE INDEX idx_fills_venue_timestamp
    ON fills (venue, timestamp);

CREATE INDEX idx_cash_flows_venue_timestamp
    ON cash_flows (venue, timestamp);

-- Leg reconstruction and PnL: gather every fill belonging to a leg, and
-- find fills that could not be assigned (leg_id IS NULL).
CREATE INDEX idx_fills_leg_id
    ON fills (leg_id);

CREATE INDEX idx_cash_flows_leg_id
    ON cash_flows (leg_id);

-- Trade inspection: gather both legs of a trade.
CREATE INDEX idx_legs_trade_id
    ON legs (trade_id);

COMMIT;