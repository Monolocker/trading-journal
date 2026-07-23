"""Smoke tests: the package imports and the local SQLite build is usable.

These run before any application code exists so that environment problems
surface now rather than during the database milestone.
"""

import sqlite3

import tradejournal

# SQLite 3.35 (March 2021) gives us UPSERT, generated columns, and RETURNING,
# all of which the repository layer in Milestone 2 relies on.
MINIMUM_SQLITE_VERSION = (3, 35, 0)


def test_package_imports() -> None:
    assert tradejournal.__version__ == "0.1.0"


def test_sqlite_version_is_supported() -> None:
    assert sqlite3.sqlite_version_info >= MINIMUM_SQLITE_VERSION, (
        f"Python links SQLite {sqlite3.sqlite_version}, which is older than the "
        f"required {'.'.join(str(part) for part in MINIMUM_SQLITE_VERSION)}."
    )


def test_foreign_keys_can_be_enabled() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys = ON;")
        (enabled,) = connection.execute("PRAGMA foreign_keys;").fetchone()
    finally:
        connection.close()
    assert enabled == 1