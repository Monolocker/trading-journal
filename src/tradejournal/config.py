"""Environment-backed configuration.

All settings apart of this trade journal come from the 
environment variables, which can be optionally seeded from 
a .env file. These are not read from or by the network and 
no secret is required. A user's address on HL is public, while
variational is a local directory of exported files (due to
limited read-only API in current state)

Settings objects are resolved into a frozen Settings class rather than
read from os.environ at each use, so a run cannot change behavior 
half-way through, can tests can construct Settings directly w/o
touching the process environment.

Missing or malformed values raise ConfigError naming the variable.
TJ stands for TradeJournal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass 
from pathlib import Path

DEFAULT_DATABASE_PATH = "./data/tradejournal.db"
DEFAULT_INFO_URL = "https://api.hyperliquid.xyz/info"
DEFAULT_IMPORT_DIR = "./data/variational"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_LOG_LEVEL = "INFO"

PLACEHOLDER_ADDRESS = "0x" + "0" * 40

class ConfigError(Exception):
    """A required setting is missing or unusable."""

@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    hyperliquid_account_address: str | None
    hyperliquid_info_url: str
    variational_import_dir: str
    http_timeout_seconds: float
    http_max_retries: int
    log_level: str

    def require_hyperliquid_address(self) -> str:
        """The account address, or a clear error explaining what to set."""
        if not self.hyperliquid_account_address: 
            raise ConfigError(
                "TJ_HYPERLIQUID_ACCOUNT_ADDRESS is not set. Set it to your "
                "PUBLIC master address (42-character hex). Empty results for "
                "agent/API wallet addresses. No private key ever needed."
            )
        return self.hyperliquid_account_address
    

def _float(name: str, default: float, environ: dict[str, str]) -> float:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try: 
        return float(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from error
    

def _int(name: str, default: int, environ: dict[str,str]) -> int:
    raw = environ.get(name, "").strip()
    if not raw: 
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from error
    

def load_settings(
        environ: dict[str, str] | None = None, *, env_file: Path | None = None
) -> Settings: 
    """Resolve settings from the environment, optionally seeding from .env
    
    Values already present in the environment win over the file (usual precedence),
    this lets one-off runs override a stored value w/o editing anything
    """
    if environ is None: 
        if env_file is None: 
            env_file = Path(".env")
        if env_file.is_file():
            _seed_from_env_file(env_file)
    environ = dict(os.environ)

    address = environ.get("TJ_HYPERLIQUID_ACCOUNT_ADDRESS", "").strip()
    if address == PLACEHOLDER_ADDRESS:
        # The shipped .env.example address value. Treating it as real 
        # would send a wasteful request and return an empty journal
        address = ""

    return Settings(
        database_path=Path(
            environ.get("TJ_DATABASE_PATH", "").strip()
            or DEFAULT_DATABASE_PATH
        ),
        hyperliquid_account_address=address or None,
        hyperliquid_info_url=(
            environ.get("TJ_HYPERLIQUID_INFO_URL", "").strip()
            or DEFAULT_INFO_URL
        ),
        variational_import_dir=Path(
            environ.get("TJ_VARIATIONAL_IMPORT_DIR", "").strip()
            or DEFAULT_IMPORT_DIR
        ),
        http_timeout_seconds=_float(
            "TJ_HTTP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, environ
        ),
        http_max_retries=_int(
            "TJ_HTTP_MAX_RETRIES", DEFAULT_MAX_RETRIES, environ,
        ),
        log_level=(
            environ.get("TJ_LOG_LEVEL", "").strip().upper()
            or DEFAULT_LOG_LEVEL
        ),
    )

def _seed_from_env_file(env_file: Path) -> None: 
    """Load key=value lines into os.environ w/o overwriting."""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))