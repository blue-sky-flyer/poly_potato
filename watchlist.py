"""
Watchlist of Polymarket markets relevant to Trump-adjacent geopolitical events.
Seeded manually; the collector also auto-discovers new high-volume political markets.
"""

# Known condition IDs for currently active markets
# Format: { condition_id: human_label }
WATCHLIST = {
    # Iran
    "0x421bc1929df1429cf2cb94f80c1ce6a3ed0d1f0b7a2749b9890075f94eb549e9": "US-Iran permanent peace deal by May 26",
    "0x518a5b030b205706b8ffe6bbad9bd3de59548348e5c0471827f5de21e513333c": "Strait of Hormuz traffic returns to normal by end of May",
    "0x6ab0ce92e138eec9776d055e052140ff284f885fb8a54c74a944316e4a2e4d80": "Saeed Jalili head of state in Iran end of 2026",

    # Colombia / Latin America
    "0xc6e54956b79ddf6f8640df06eea9ffb1f5e558907b2a9031aa00cefeb5f3ab22": "US strike on Colombia by Dec 31",

    # Oil / macro
    "0x59a37ea3830d532957b04d3c437a329e14a5dc840096d48c7ee4b55ba3d9cca8": "WTI Crude hit $85 low in May",
}

# Keywords used to auto-discover new political markets from the gamma API
DISCOVERY_KEYWORDS = [
    "iran", "israel", "strike", "war", "nuclear", "ceasefire",
    "venezuela", "maduro", "colombia", "sanctions",
    "china", "taiwan", "invasion",
    "trump", "tariff", "executive order",
    "oil", "opec", "hormuz",
    "russia", "ukraine", "nato",
    "khamenei", "netanyahu",
]

# Minimum liquidity (USDC) for auto-discovered markets to be included
MIN_LIQUIDITY = 5_000
