"""SQLite connection management, storage conversion, and migrations.

Storage formats used throughout the database:

    Decimal     TEXT, fixed-point notation, produced by format(value, "f").
                Never REAL: IEEE-754 rounding would corrupt funding and PnL.
                Because these sort lexicographically rather than numerically,
                never aggregate or order by them in SQL. Read the rows and
                sum with Decimal in Python.

    datetime    INTEGER milliseconds since the Unix epoch, always UTC.
                Naive datetimes are rejected rather than assumed to be UTC.

    JSON        TEXT produced by json.dumps with sorted keys and no
                separators padding, so identical payloads yield identical
                text. NaN and Infinity are rejected.

    enums       Lowercase TEXT matching the StrEnum values, guarded by CHECK
                constraints in the schema.

Nothing here uses pickle or any other executable serialisation format.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

MIGRATIONS_DIRECTORY = Path(__file__).parent / "migrations"

MEMORY_DATABASE = ":memory:"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_MILLISECOND = timedelta(milliseconds=1)
_MIGRATION_FILENAME_PATTERN = re.compile(r"^(\d+)_.+\.sql$")


# Connections
def connect(database_path: Path | str) -> sqlite3.Connection:
    """Open a connection with foreign keys enforced.

    SQLite disables foreign key enforcement per connection by default, so
    the pragma must be issued every time rather than once at schema
    creation. The result is verified rather than assumed, because a SQLite
    build compiled without foreign key support ignores the pragma silently.

    isolation_level=None puts the driver in autocommit mode so that
    transactions are started explicitly by the transaction() helper, rather
    than being opened implicitly at times that are hard to predict.
    """
    is_memory = str(database_path) == MEMORY_DATABASE
    if not is_memory:
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")

    (enabled,) = connection.execute("PRAGMA foreign_keys;").fetchone()
    if enabled != 1:
        connection.close()
        raise RuntimeError(
            "This SQLite build does not enforce foreign keys, which the "
            "trade journal requires."
        )
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside an explicit transaction.

    Commits on success, rolls back on any exception including
    KeyboardInterrupt. Used to make multi-statement work such as writing
    events and advancing a sync cursor atomic.
    """
    connection.execute("BEGIN")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")



# Decimal conversion and format
def decimal_to_text(value: Decimal) -> str:
    """Serialise a Decimal for storage, rejecting anything unsafe."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(
            "Refusing to store a float as a monetary value; use Decimal."
        )
    if not isinstance(value, Decimal):
        raise TypeError(f"Expected Decimal, got {type(value).__name__}.")
    if not value.is_finite():
        raise ValueError("Refusing to store a non-finite Decimal.")
    return format(value, "f")


def text_to_decimal(value: str) -> Decimal:
    """Parse a stored decimal string, treating the input as untrusted."""
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"Not a valid decimal value: {value!r}") from error
    if not result.is_finite():
        raise ValueError(f"Refusing a non-finite decimal value: {value!r}")
    return result


def optional_decimal_to_text(value: Decimal | None) -> str | None:
    return None if value is None else decimal_to_text(value)


def optional_text_to_decimal(value: str | None) -> Decimal | None:
    return None if value is None else text_to_decimal(value)



# Timestamp conversion
def datetime_to_epoch_ms(moment: datetime) -> int:
    """Convert a timezone-aware datetime to epoch milliseconds.

    Naive datetimes are rejected. Assuming a missing timezone means UTC is
    exactly the sort of assumption that silently misplaces funding events by
    hours.

    Integer arithmetic on a timedelta is used rather than
    datetime.timestamp(), which returns a float and loses precision.
    """
    if moment.tzinfo is None:
        raise ValueError(
            "Refusing a naive datetime; timestamps must be timezone-aware."
        )
    return (moment.astimezone(UTC) - _EPOCH) // _ONE_MILLISECOND


def epoch_ms_to_datetime(milliseconds: int) -> datetime:
    """Convert epoch milliseconds to a timezone-aware UTC datetime."""
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, int):
        raise TypeError(
            f"Expected int milliseconds, got {type(milliseconds).__name__}."
        )
    return _EPOCH + timedelta(milliseconds=milliseconds)


def optional_datetime_to_epoch_ms(moment: datetime | None) -> int | None:
    return None if moment is None else datetime_to_epoch_ms(moment)


def optional_epoch_ms_to_datetime(milliseconds: int | None) -> datetime | None:
    return None if milliseconds is None else epoch_ms_to_datetime(milliseconds)


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(tz=UTC)



# JSON conversion
def to_canonical_json(payload: Any) -> str:
    """Serialise a raw venue payload deterministically.

    Sorted keys and compact separators mean identical payloads always
    produce identical text, so stored payloads can be compared directly.
    allow_nan=False rejects NaN and Infinity, which are not valid JSON and
    should never arrive from a well-behaved API.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def from_canonical_json(text: str) -> Any:
    """Parse stored JSON text. Uses json only, never pickle or eval."""
    return json.loads(text)



# Migrations
def _ensure_migration_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            filename   TEXT    NOT NULL,
            applied_at INTEGER NOT NULL
        )
        """
    )


def _discover_migrations(
    migrations_directory: Path,
) -> list[tuple[int, Path]]:
    discovered: list[tuple[int, Path]] = []
    for path in sorted(migrations_directory.glob("*.sql")):
        match = _MIGRATION_FILENAME_PATTERN.match(path.name)
        if match is None:
            raise ValueError(
                f"Migration filename is not numbered correctly: {path.name}"
            )
        discovered.append((int(match.group(1)), path))

    versions = [version for version, _ in discovered]
    if len(set(versions)) != len(versions):
        raise ValueError("Duplicate migration version numbers found.")
    return sorted(discovered)


def applied_migration_versions(connection: sqlite3.Connection) -> set[int]:
    """Return the set of migration versions already applied."""
    _ensure_migration_table(connection)
    rows = connection.execute(
        "SELECT version FROM schema_migrations"
    ).fetchall()
    return {int(row["version"]) for row in rows}


def apply_migrations(
    connection: sqlite3.Connection,
    migrations_directory: Path = MIGRATIONS_DIRECTORY,
) -> list[str]:
    """Apply every pending migration in version order.

    Each migration file wraps itself in BEGIN TRANSACTION / COMMIT, so a
    failure part-way through rolls the whole file back and the version is
    never recorded. Re-running is therefore safe.

    Returns the filenames applied during this call, which is empty when the
    database is already current.
    """
    _ensure_migration_table(connection)
    already_applied = applied_migration_versions(connection)

    applied_now: list[str] = []
    for version, path in _discover_migrations(migrations_directory):
        if version in already_applied:
            continue

        connection.executescript(path.read_text(encoding="utf-8"))
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO schema_migrations (version, filename, applied_at)
                VALUES (?, ?, ?)
                """,
                (version, path.name, datetime_to_epoch_ms(utc_now())),
            )
        applied_now.append(path.name)

    return applied_now


def initialize_database(
    database_path: Path | str,
    migrations_directory: Path = MIGRATIONS_DIRECTORY,
) -> sqlite3.Connection:
    """Open a connection and bring the schema fully up to date."""
    connection = connect(database_path)
    apply_migrations(connection, migrations_directory)
    return connection


def reset_database(database_path: Path | str, *, confirm: bool = False) -> None:
    """Delete a local development database file.

    Refuses unless confirm=True is passed explicitly. This exists only for
    disposable local databases and is never called by ingestion code.
    """
    if not confirm:
        raise ValueError(
            "reset_database refuses to run without confirm=True."
        )
    if str(database_path) == MEMORY_DATABASE:
        return

    path = Path(database_path)
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()