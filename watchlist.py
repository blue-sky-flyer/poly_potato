"""
Watchlist of Polymarket markets relevant to Trump-adjacent geopolitical events.
Seeded manually; the collector also auto-discovers new high-volume political markets.
"""

# Known condition IDs for currently active markets
# Format: { condition_id: human_label }
WATCHLIST = {
    # Hormuz / Iran — highest liquidity active geopolitical market
    "0x348cd9adf4f6855f58bd9c6dbf9ff251c4142ef77233a5dc95c65b4b61cd2187": "Strait of Hormuz traffic normal by end of June 2026",

    # Iran regime
    "0x6ab0ce92e138eec9776d055e052140ff284f885fb8a54c74a944316e4a2e4d80": "Saeed Jalili head of state in Iran end of 2026",

    # Colombia / Latin America
    "0xc6e54956b79ddf6f8640df06eea9ffb1f5e558907b2a9031aa00cefeb5f3ab22": "US strike on Colombia by Dec 31",
}

# Keywords used to auto-discover new political markets from the gamma API
DISCOVERY_KEYWORDS = [
    "iran nuclear", "iran deal", "iran strike", "iran war", "iran ceasefire",
    "israel strike", "israel iran", "israel war",
    "hormuz", "khamenei", "netanyahu",
    "venezuela maduro", "colombia strike",
    "china taiwan", "taiwan invasion",
    "russia ukraine", "nato article",
    "oil price", "wti crude", "opec cut",
    "trump sanctions", "trump military",
]

# Minimum liquidity (USDC) for auto-discovered markets to be included
MIN_LIQUIDITY = 5_000
