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

## Agentic Copy-Trading Ecosystem

We have introduced an automated Python script (`copy_trader_agents.py`) that acts as an orchestrator for several agents to discover, backtest, and evaluate Polymarket traders you might want to copy.

### How to Run the Ecosystem

1. **Install Dependencies:**
   Ensure you have installed the required packages:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment:**
   The `ProfilerAgent` uses an LLM (Claude) to provide qualitative assessments of the traders. You need to provide an Anthropic API key.
   Create a file named `.env` in the root directory and add your key:
   ```
   ANTHROPIC_API_KEY=your_actual_api_key_here
   ```
   *Note: If you do not provide this key, the system will still run and backtest the traders, but the LLM assessment column will be blank.*

3. **Run the Script:**
   Execute the orchestrator:
   ```bash
   python copy_trader_agents.py
   ```

### How the Ecosystem Works

The orchestrator manages four distinct agents:

1. **ScoutAgent:** Responsible for data discovery. It first attempts to load pre-calculated candidates from an existing `top_traders.csv` (to save time and API calls). If no file is found, it queries the Polymarket Leaderboard API across multiple permutations (Category, Time, Order By) to bypass the 10,000 result pagination limit.
2. **BacktesterAgent:** Responsible for validating strategies. Because you will be copy-trading with a ~200ms latency, this agent simulates historical trades while applying a dynamic slippage penalty. The slippage varies based on the inferred market category (e.g., highly liquid Politics vs. less liquid Sports) and scales up with the trade size. It also applies simulated Polygon gas fees. It ensures the trader's ROI survives these penalties.
3. **ProfilerAgent:** Responsible for qualitative analysis. It sends the trader's stats and backtested metrics to the Anthropic API (Claude), asking the LLM to classify the trader's style (e.g., Knowledge-Based, Swing Trader, Gambler) and provide a 2-3 sentence reason on why you should follow them, favoring Economics/Politics over Sports.
4. **ReportAgent:** Responsible for output generation. It formats the final curated list of survivors into an easily readable Markdown file (`top_copy_candidates.md`) and a self-contained, interactive HTML dashboard (`top_copy_candidates.html`) with sorting and filtering capabilities.
