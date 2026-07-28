"""Symbol normaliztion boundary.

Every venue names its markets differently. This module is the single place
where those names are translated into one canonical form, so that no other
part of the application needs to know what a particular venue calls anything
respectively.

Canonical form is BASE-PERP, upper case: BTC-PERP, ETH-PERP, KPEPE-PERP.

Two rules govern everything here:

1.  Never guess. A symbol that does not match a known, validated pattern
    raises SymbolNormalizationError carrying the raw value. The caller
    decides whether to skip it with an alert or fail outright. Silently
    mangling an unrecognised symbol into a plausible-looking one is
    worst-case scenario, because the error surfaces later as a
    quantity mismatch rather than as a parse failure.

2.  Never strip a size multiplier. Hyperliquid prefixes some markets with
    'k' to denote a 1000x contract: kPEPE is a contract on 1000 PEPE. If
    another venue lists plain PEPE, (or 1000PEPE as Variational does) then
    one unit there is not one unit here. Stripping the prefix would make two 
    incomparable contracts share a canonical symbol, and quantity reconciliation 
    would compare numbers that differ by a factor of 1000. Multiplier prefixes 
    are therefore preserved, and cross-venue equivalence must be declared explicitly 
    in an alias map by a human.
"""

from __future__ import annotations

import re

CANONICAL_SUFFIX = "-PERP"

# A canonical base is letters and digits only. Anything else such as: a slash, 
# an '@' index, a colon, a space thus indicates a market type this journal does
# not handle, and is rejected rather than interpreted.
_SAFE_BASE_PATTERN = re.compile(r"^[A-Za-z0-9]{1,20}$")

# Quote-currency suffixes seen on venues that name markets as a pair.
# Ordered longest first so that '-USDC' is tried before '-USD'.
#
# PROVISIONAL: this list is a reasonable assumption, not verified against a
# real Variational export. Milestone 5 confirms it against actual data.
_VARIATIONAL_STRIPPABLE_SUFFIXES = (
    "-PERPETUAL",
    "-USDC",
    "-PERP",
    "-USD",
    "/USDC",
    "/USD",
)

# Explicit cross-venue equivalences, for cases where two venues list the
# same contract under names that no rule could reconcile. Keys are the raw
# venue symbol, values are the canonical symbol.
#
# Populated deliberately, one entry at a time, only when a real mismatch is
# observed. An empty map is the correct starting state.
HYPERLIQUID_SYMBOL_ALIASES: dict[str, str] = {}
VARIATIONAL_SYMBOL_ALIASES: dict[str, str] = {}


class SymbolNormalizationError(ValueError):
    """Raised when a venue symbol cannot be normalized confidently.

    Carries the raw value so that callers can log or alert on it without
    having to reconstruct what was rejected.
    """

    def __init__(self, venue_symbol: object, reason: str) -> None:
        self.venue_symbol = venue_symbol
        self.reason = reason
        super().__init__(f"Cannot normalize symbol {venue_symbol!r}: {reason}")


def to_canonical(base_asset: str) -> str:
    """Build a canonical symbol from a validated base asset name."""
    if not _SAFE_BASE_PATTERN.match(base_asset):
        raise SymbolNormalizationError(
            base_asset, "base asset is not plain alphanumeric text"
        )
    return f"{base_asset.upper()}{CANONICAL_SUFFIX}"


def is_canonical(symbol: str) -> bool:
    """Whether a string is already in canonical form."""
    if not symbol.endswith(CANONICAL_SUFFIX):
        return False
    base = symbol[: -len(CANONICAL_SUFFIX)]
    return bool(_SAFE_BASE_PATTERN.match(base)) and base == base.upper()


def _require_text(venue_symbol: object) -> str:
    """Reject anything that is not a non-empty string.

    API responses are untrusted, so a symbol arriving as None, a number, or
    a nested object must fail here rather than deeper in the pipeline.
    """
    if not isinstance(venue_symbol, str):
        raise SymbolNormalizationError(
            venue_symbol, f"expected a string, got {type(venue_symbol).__name__}"
        )
    stripped = venue_symbol.strip()
    if not stripped:
        raise SymbolNormalizationError(venue_symbol, "symbol is empty")
    return stripped


def normalize_hyperliquid_symbol(venue_symbol: object) -> str:
    """Normalize a Hyperliquid market name.

    Hyperliquid names perpetual markets by their base asset alone: BTC,
    ETH, SOL, HYPE. Markets on a 1000x contract carry a 'k' prefix, which
    is preserved.

    Rejected on purpose:
      * spot pairs, which contain a slash (PURR/USDC)
      * spot index names, which begin with '@'
      * anything containing a separator, which may indicate a
        builder-deployed market whose name could collide with a main
        market of the same base asset

    Rejection is not failure. It means this journal will not guess, and the
    adapter should record an alert rather than invent a market.
    """
    symbol = _require_text(venue_symbol)

    if symbol in HYPERLIQUID_SYMBOL_ALIASES:
        return HYPERLIQUID_SYMBOL_ALIASES[symbol]

    if "/" in symbol:
        raise SymbolNormalizationError(
            venue_symbol, "looks like a spot pair, which is out of scope"
        )
    # @{index} e.g. @1 refers to spot index where index is the index of the spot 
    # pair in the universe field of the spotMeta response
    if symbol.startswith("@"):
        raise SymbolNormalizationError(
            venue_symbol, "looks like a spot index, which is out of scope"
        )
    if not _SAFE_BASE_PATTERN.match(symbol):
        raise SymbolNormalizationError(
            venue_symbol,
            "not a plain perpetual market name; may be a builder-deployed "
            "market requiring explicit support",
        )

    return to_canonical(symbol)


def normalize_variational_symbol(venue_symbol: object) -> str:
    """Normalize a Variational Omni market name.

    PROVISIONAL. Variational's account-level export format has not been
    verified, because their account API is not public. Currently API is read-only. 
    This function checks the alias map, then strips a documented set of quote-currency
    suffixes, then validates what remains.

    Milestone 5 confirms this against a real export. The tests below pin
    current behaviour so that any change is visible rather than silent.
    """
    symbol = _require_text(venue_symbol)

    if symbol in VARIATIONAL_SYMBOL_ALIASES:
        return VARIATIONAL_SYMBOL_ALIASES[symbol]

    upper = symbol.upper()
    for suffix in _VARIATIONAL_STRIPPABLE_SUFFIXES:
        if upper.endswith(suffix):
            upper = upper[: -len(suffix)]
            break

    if not _SAFE_BASE_PATTERN.match(upper):
        raise SymbolNormalizationError(
            venue_symbol,
            "no known suffix rule produced a plain base asset name",
        )

    return to_canonical(upper)