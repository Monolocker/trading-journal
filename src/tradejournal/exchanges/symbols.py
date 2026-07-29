"""Symbol normalisation boundary.

Every venue names its markets differently. This module is the single place
where those names are translated into one canonical form, so that no other
part of the application needs to know what a venue calls anything.

Canonical form
--------------
    BASE-PERP                 a market on the venue's primary perp dex
    NAMESPACE:BASE-PERP       a market on a namespaced sub-venue

Examples: BTC-PERP, KPEPE-PERP, XYZ:AAPL-PERP.

Three rules govern everything here:

1.  Never guess. A symbol that does not match a known, validated pattern
    raises SymbolNormalizationError carrying the raw value. The caller
    decides whether to skip it with an alert or fail outright. Silently
    mangling an unrecognised symbol into a plausible-looking one is the
    worst available outcome, because the error surfaces later as a
    quantity mismatch rather than as a parse failure.

2.  Never strip a size multiplier. Hyperliquid prefixes some markets with
    'k' to denote a 1000x contract: kPEPE is a contract on 1000 PEPE. If
    another venue lists plain PEPE, one unit there is not one unit here.
    Stripping the prefix would make two incomparable contracts share a
    canonical symbol, and quantity reconciliation would compare numbers
    that differ by a factor of 1000.

3.  Never strip a market namespace. Hyperliquid's HIP-3 framework lets
    builders deploy independent perp dexs, whose markets are named
    'dex:ASSET'. A HIP-3 market is a genuinely different market from a
    same-named market on the primary dex: separate order book, separate
    deployer-operated oracle, separate funding, separate margin. For
    example, folding hyna:BTC onto BTC would cause leg reconstruction 
    to merge fills from two unrelated positions. However, compatibility 
    here was mainly updated for TradeXYZ markets.

Cross-venue pairing
-------------------
Rule 3 has a consequence. A long on Hyperliquid's XYZ:AAPL-PERP hedged by
a short on another venue's AAPL-PERP produces two different canonical
symbols, so the two legs will not pair on symbol equality alone.

That is deliberate: pairing markets because their names look similar is
exactly the silent error this module exists to prevent. Trade pairing
should instead compare base_asset() across venues, which is safe because
pairing is cross-venue by construction and therefore cannot merge two
markets on the same venue. market_namespace() remains available so that a
pairing rule can record which sub-venue a leg came from.
"""

from __future__ import annotations

import re

CANONICAL_SUFFIX = "-PERP"
NAMESPACE_SEPARATOR = ":"

# A base asset on the primary perp dex is letters and digits only.
_NATIVE_BASE_PATTERN = re.compile(r"^[A-Za-z0-9]{1,20}$")

# HIP-3 dex names are documented as 2-4 characters. A slightly wider bound
# is allowed so that a future change to that limit does not silently
# reject every market on a new dex, which is the failure this module is
# built to avoid.
_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9]{1,8}$")

# Asset names on a HIP-3 dex are chosen by the deployer and are not
# restricted to plain alphanumerics in practice, so a wider set is allowed
# here than on the primary dex. Characters that would make the canonical
# form ambiguous or that indicate a different market type are still
# rejected: ':' (a second namespace), '/' (a spot pair), '@' (a spot
# index), and whitespace.
_NAMESPACED_ASSET_PATTERN = re.compile(r"^[A-Za-z0-9._*+-]{1,32}$")

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
    """Raised when a venue symbol cannot be normalised confidently.

    Carries the raw value so that callers can log or alert on it without
    having to reconstruct what was rejected.
    """

    def __init__(self, venue_symbol: object, reason: str) -> None:
        self.venue_symbol = venue_symbol
        self.reason = reason
        super().__init__(f"Cannot normalise symbol {venue_symbol!r}: {reason}")


def to_canonical(base: str, *, namespace: str | None = None) -> str:
    """Build a canonical symbol from a base asset and optional namespace."""
    if namespace is None:
        if not _NATIVE_BASE_PATTERN.match(base):
            raise SymbolNormalizationError(
                base, "base asset is not plain alphanumeric text"
            )
        return f"{base.upper()}{CANONICAL_SUFFIX}"

    if not _NAMESPACE_PATTERN.match(namespace):
        raise SymbolNormalizationError(
            f"{namespace}:{base}",
            "market namespace is not 1 to 8 alphanumeric characters",
        )
    if not _NAMESPACED_ASSET_PATTERN.match(base):
        raise SymbolNormalizationError(
            f"{namespace}:{base}",
            "namespaced asset name contains unsupported characters",
        )
    return (
        f"{namespace.upper()}{NAMESPACE_SEPARATOR}"
        f"{base.upper()}{CANONICAL_SUFFIX}"
    )


def split_canonical(symbol: str) -> tuple[str | None, str]:
    """Split a canonical symbol into (namespace, base asset).

    The namespace is None for a market on a venue's primary perp dex.
    Raises if the argument is not in canonical form, so that a caller
    cannot accidentally pass a raw venue symbol here.
    """
    if not isinstance(symbol, str) or not symbol.endswith(CANONICAL_SUFFIX):
        raise SymbolNormalizationError(symbol, "not a canonical symbol")

    body = symbol[: -len(CANONICAL_SUFFIX)]
    if NAMESPACE_SEPARATOR not in body:
        return (None, body)

    namespace, _, base = body.partition(NAMESPACE_SEPARATOR)
    return (namespace, base)


def base_asset(symbol: str) -> str:
    """Return the underlying asset of a canonical symbol.

    Both BTC-PERP and XYZ:BTC-PERP have the base asset BTC. Trade pairing
    uses this to match legs across venues when one venue lists a market on
    a namespaced sub-venue and the other does not.
    """
    return split_canonical(symbol)[1]


def market_namespace(symbol: str) -> str | None:
    """Return the sub-venue namespace of a canonical symbol, if any."""
    return split_canonical(symbol)[0]


def is_canonical(symbol: str) -> bool:
    """Whether a string is already in canonical form."""
    if not isinstance(symbol, str) or not symbol.endswith(CANONICAL_SUFFIX):
        return False
    if symbol != symbol.upper():
        return False

    body = symbol[: -len(CANONICAL_SUFFIX)]
    if NAMESPACE_SEPARATOR not in body:
        return bool(_NATIVE_BASE_PATTERN.match(body))

    namespace, _, base = body.partition(NAMESPACE_SEPARATOR)
    return bool(
        _NAMESPACE_PATTERN.match(namespace)
        and _NAMESPACED_ASSET_PATTERN.match(base)
    )


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
    """Normalise a Hyperliquid market name.

    Two market shapes are supported:

        BTC          a market on the primary perp dex, named by base asset
        xyz:AAPL     a market on a HIP-3 builder-deployed perp dex

    A HIP-3 market keeps its dex namespace, so xyz:AAPL normalises to
    XYZ:AAPL-PERP and can never be confused should HL natively list equity
    perps.

    Rejected on purpose:
      * spot pairs, which contain a slash (PURR/USDC)
      * spot index names, which begin with '@'
      * anything carrying more than one namespace separator

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
    if symbol.startswith("@"):
        raise SymbolNormalizationError(
            venue_symbol, "looks like a spot index, which is out of scope"
        )

    if NAMESPACE_SEPARATOR in symbol:
        if symbol.count(NAMESPACE_SEPARATOR) > 1:
            raise SymbolNormalizationError(
                venue_symbol, "more than one namespace separator"
            )
        namespace, _, asset = symbol.partition(NAMESPACE_SEPARATOR)
        if not namespace or not asset:
            raise SymbolNormalizationError(
                venue_symbol, "namespace and asset must both be non-empty"
            )
        try:
            return to_canonical(asset, namespace=namespace)
        except SymbolNormalizationError as error:
            raise SymbolNormalizationError(
                venue_symbol, error.reason
            ) from None

    if not _NATIVE_BASE_PATTERN.match(symbol):
        raise SymbolNormalizationError(
            venue_symbol, "not a plain perpetual market name"
        )

    return to_canonical(symbol)


def normalize_variational_symbol(venue_symbol: object) -> str:
    """Normalise a Variational Omni market name.

    PROVISIONAL. Variational's account-level export format has not been
    verified, because their account API is not public. This function
    checks the alias map, then strips a documented set of quote-currency
    suffixes, then validates what remains.

    No namespace form is supported, because no evidence of one has been
    seen. A symbol containing ':' is rejected rather than guessed at.

    Milestone 5 confirms this against a real export. The tests pin current
    behaviour so that any change is visible rather than silent.
    """
    symbol = _require_text(venue_symbol)

    if symbol in VARIATIONAL_SYMBOL_ALIASES:
        return VARIATIONAL_SYMBOL_ALIASES[symbol]

    if NAMESPACE_SEPARATOR in symbol:
        raise SymbolNormalizationError(
            venue_symbol,
            "namespaced markets are not yet supported for this venue",
        )

    upper = symbol.upper()
    for suffix in _VARIATIONAL_STRIPPABLE_SUFFIXES:
        if upper.endswith(suffix):
            upper = upper[: -len(suffix)]
            break

    if not _NATIVE_BASE_PATTERN.match(upper):
        raise SymbolNormalizationError(
            venue_symbol,
            "no known suffix rule produced a plain base asset name",
        )

    return to_canonical(upper)