# Top-pm-traders
## Bypassing the 10,000 Trader Limit

The public Polymarket Leaderboard API endpoint has a strict pagination limit. Querying any `offset` greater than `10000` returns the identical result (the user at rank `10001`). Thus, finding the top 100,000 traders purely by querying the leaderboard is impossible without internal API access.

### Alternative Methods
To find more than 10,000 traders, we must pivot from the leaderboard to scraping **onchain trading activity** directly, effectively building our own dataset of users and calculating their PNL manually.

1. **Polymarket Subgraphs (The Graph / Goldsky)**
   Polymarket exposes their indexed onchain data via subgraphs on Goldsky.
   - We can query the `orderbook-subgraph` using `orderFilledEvents`.
   - Each event provides a `maker` and `taker` address. By paginating through millions of these events, we can extract every single wallet address that has ever traded on Polymarket.
   - Once we have a massive list of >100,000 unique `taker`/`maker` wallets, we can pass them into our existing `fetch_all_activity` and `analyze_trader_deals` functions.

2. **Scraping Gamma API Events**
   - Query all markets from `https://gamma-api.polymarket.com/events`.
   - For every single market ID, query the trades endpoint.
   - Note: The public `/trades` endpoint on `clob.polymarket.com` is protected and requires an authenticated API key (`401 Unauthorized`). This method only works if you have an active CLOB API key.

**Recommendation:** Using the public Goldsky Subgraphs is the only free way to extract >100,000 user addresses, but requires scraping all historical transactions rather than sorting by PNL out-of-the-box.
