"""
Polymarket watchlist.

WATCHLIST: manually pinned markets tracked regardless of liquidity.
MIN_LIQUIDITY: auto-discovery threshold — all active markets above this
               are watched automatically, no keyword filter.
"""

# Manually pinned condition IDs — tracked even if below MIN_LIQUIDITY
WATCHLIST = {
    # Hormuz / Iran — highest liquidity active geopolitical market
    "0x348cd9adf4f6855f58bd9c6dbf9ff251c4142ef77233a5dc95c65b4b61cd2187": "Strait of Hormuz traffic normal by end of June 2026",

    # Iran regime
    "0x6ab0ce92e138eec9776d055e052140ff284f885fb8a54c74a944316e4a2e4d80": "Saeed Jalili head of state in Iran end of 2026",

    # Colombia / Latin America
    "0xc6e54956b79ddf6f8640df06eea9ffb1f5e558907b2a9031aa00cefeb5f3ab22": "US strike on Colombia by Dec 31",
}

# Auto-discover all active markets with liquidity above this threshold (USDC)
# Covers ~50-150 markets at any given time — no topic filter applied
MIN_LIQUIDITY = 10_000
