"""
Watchlist of Polymarket markets relevant to Trump-adjacent geopolitical events.
Seeded manually; the collector also auto-discovers new high-volume political markets.
"""

# Known condition IDs for currently active markets
# Format: { condition_id: human_label }
WATCHLIST = {
    # Iran / Middle East — long-dated, meaningful liquidity
    "0x6ab0ce92e138eec9776d055e052140ff284f885fb8a54c74a944316e4a2e4d80": "Saeed Jalili head of state in Iran end of 2026",
    "0xcba13d2eec9eceed57bd08d8157bd01fff8a54bef61baa6d9303fb2c3cd75c5a": "Netherlands warships through Strait of Hormuz by June 30",

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
