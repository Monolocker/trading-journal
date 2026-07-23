"""Read-only trade journal for delta-neutral perp positions.

Ingests and reconciles execution history from Hyperliquid and Variational.
This package never places, cancels, or modifies orders. Additionally, 
this package never changes leverage, never transfers or withdraws funds, 
and never signs a txn
"""

__version__ = "0.1.0"