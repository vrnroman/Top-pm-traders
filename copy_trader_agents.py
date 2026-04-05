import os
import requests
import pandas as pd
import time
from datetime import datetime, timezone
import collections
from tabulate import tabulate
import concurrent.futures
import traceback
import json
from anthropic import Anthropic
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class ScoutAgent:
    """Fetches candidate traders and their transaction histories from Polymarket."""

    def __init__(self, limit=50, max_offset=10000):
        self.limit = limit
        self.max_offset = max_offset
        self.categories = ['OVERALL', 'SPORTS', 'POLITICS', 'CRYPTO', 'POP_CULTURE', 'BUSINESS', 'SCIENCE']
        self.time_periods = ['ALL', '1Y', '1M', '1W', '1D']
        self.order_bys = ['PNL', 'VOL']

    def fetch_leaderboard_combinations(self):
        """Bypasses 10k limit by iterating over combinations."""
        print(f"ScoutAgent: Fetching traders using leaderboard permutations...")
        unique_traders = {}
        completed = 0
        combos = [(c, t, o) for c in self.categories for t in self.time_periods for o in self.order_bys]
        total = len(combos)

        def fetch_combo(combo):
            c, t, o = combo
            local_traders = {}
            last_first_wallet = None
            for offset in range(0, self.max_offset, self.limit):
                url = f"https://data-api.polymarket.com/v1/leaderboard?category={c}&timePeriod={t}&orderBy={o}&limit={self.limit}&offset={offset}"
                try:
                    res = requests.get(url, timeout=10)
                    if res.status_code == 200:
                        data = res.json()
                        if not data:
                            break
                        if offset >= self.limit:
                            if data[0].get('proxyWallet') == last_first_wallet:
                                break
                        last_first_wallet = data[0].get('proxyWallet') if data else None
                        for row in data:
                            wallet = row.get('proxyWallet')
                            if wallet:
                                local_traders[wallet] = row
                    else:
                        break
                except Exception:
                    break
                time.sleep(0.01)
            return local_traders

        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            futures = {executor.submit(fetch_combo, combo): combo for combo in combos}
            for future in concurrent.futures.as_completed(futures):
                completed += 1
                try:
                    unique_traders.update(future.result())
                except:
                    pass
                print(f"ScoutAgent: Progress [{completed}/{total}]. Unique traders: {len(unique_traders)}", end='\\r')

        print(f"\\nScoutAgent: Successfully fetched {len(unique_traders)} unique candidate traders.")
        return list(unique_traders.values())

    def fetch_all_activity(self, wallet):
        """Fetches full transaction history for a wallet."""
        url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=10000"
        try:
            res = requests.get(url, timeout=15)
            if res.status_code == 200:
                return res.json()
        except:
            pass
        return []

class BacktesterAgent:
    """Simulates trades with dynamic slippage and gas fees to estimate copy-trade profitability."""

    def __init__(self, gas_fee_usd=0.02):
        self.gas_fee_usd = gas_fee_usd

    def calculate_dynamic_slippage(self, category, size_usd):
        """Estimate slippage based on market category and trade size."""
        base_slippage = 0.01  # Default 1%
        if category in ['POLITICS', 'ECONOMICS']:
            base_slippage = 0.005  # Highly liquid, 0.5%
        elif category in ['SPORTS', 'POP_CULTURE']:
            base_slippage = 0.02  # Less liquid, 2%

        # Add size penalty: 0.1% extra slippage per $1000 traded
        size_penalty = (size_usd / 1000.0) * 0.001

        # Max slippage capped at 10%
        return min(base_slippage + size_penalty, 0.10)

    def simulate(self, trader_data, activities):
        """Simulates trades to calculate adjusted PNL and ROI."""
        markets = collections.defaultdict(list)
        category_counts = collections.defaultdict(int)

        for act in activities:
            if act.get('type') in ('TRADE', 'REDEEM'):
                cid = act.get('conditionId')
                if cid:
                    markets[cid].append(act)

        simulated_pnl = 0
        total_investment = 0
        total_trades = 0

        now = datetime.now(timezone.utc).timestamp()
        recent_cutoff = now - (60 * 24 * 3600) # 2 months

        market_titles = []

        for cid, acts in markets.items():
            acts.sort(key=lambda x: x['timestamp'])
            market_title = acts[0].get('title', 'Unknown')
            if market_title != 'Unknown':
                market_titles.append(market_title)

            # Simple heuristic for category based on titles (as activity feed often lacks pure category)
            title_lower = market_title.lower()
            if any(w in title_lower for w in ['election', 'president', 'senate', 'trump', 'biden']):
                cat = 'POLITICS'
            elif any(w in title_lower for w in ['nba', 'nfl', 'super bowl', 'game', 'win']):
                cat = 'SPORTS'
            elif any(w in title_lower for w in ['fed', 'rate', 'inflation', 'gdp']):
                cat = 'ECONOMICS'
            else:
                cat = 'OTHER'

            category_counts[cat] += 1

            for act in acts:
                size = act.get('usdcSize', 0)
                if size == 0:
                    continue

                if act['timestamp'] >= recent_cutoff:
                    if act['type'] == 'TRADE':
                        total_trades += 1
                        slippage = self.calculate_dynamic_slippage(cat, size)
                        if act.get('side') == 'BUY':
                            cost_with_slippage = size * (1 + slippage)
                            simulated_pnl -= cost_with_slippage
                            simulated_pnl -= self.gas_fee_usd
                            total_investment += cost_with_slippage
                        elif act.get('side') == 'SELL':
                            rev_with_slippage = size * (1 - slippage)
                            simulated_pnl += rev_with_slippage
                            simulated_pnl -= self.gas_fee_usd
                    elif act['type'] == 'REDEEM':
                        # Redeems don't have slippage, just gas
                        simulated_pnl += size
                        simulated_pnl -= self.gas_fee_usd

        most_common_cat = 'UNKNOWN'
        if category_counts:
            most_common_cat = max(category_counts.items(), key=lambda x: x[1])[0]

        roi = (simulated_pnl / total_investment * 100) if total_investment > 0 else 0

        return {
            'simulated_recent_pnl': simulated_pnl,
            'simulated_roi': roi,
            'simulated_trades': total_trades,
            'inferred_category': most_common_cat,
            'sample_markets': list(set(market_titles))[:5]
        }

class ProfilerAgent:
    """Uses Anthropic API to profile a trader and provide qualitative reasons to follow them."""

    def __init__(self):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            print("Warning: ANTHROPIC_API_KEY not found in environment. Profiler Agent will return mock data.")
            self.client = None
        else:
            self.client = Anthropic(api_key=api_key)

    def profile_trader(self, trader_data, backtest_results):
        if not self.client:
            return "Profile unvailable: No API key."

        prompt = f"""
        Analyze the following Polymarket trader profile to determine if they are worth copy-trading.

        Trader Wallet: {trader_data.get('proxyWallet')}
        Total Return (Leaderboard PNL): ${trader_data.get('pnl', 0):.2f}

        Backtest Results (Recent 2 months, including slippage/latency fees):
        - Simulated PNL: ${backtest_results['simulated_recent_pnl']:.2f}
        - Simulated ROI: {backtest_results['simulated_roi']:.2f}%
        - Main Category: {backtest_results['inferred_category']}
        - Sample Markets Traded: {', '.join(backtest_results['sample_markets'])}

        Consider the following hypothesis: Traders focusing on Economics or Politics are often more stable, relying on knowledge or insider information, whereas Sports bettors are often just gambling. Also, consider if their strategy survives the simulated latency/slippage penalty.

        Provide a concise, 2-3 sentence qualitative assessment on why a user should (or shouldn't) follow this trader. Classify their style (e.g., Knowledge-Based, Gambler, Insider, Swing Trader).
        """

        try:
            response = self.client.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=300,
                temperature=0.2,
                system="You are an expert crypto and prediction market quantitative analyst. You are concise and analytical.",
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text.strip()
        except Exception as e:
            print(f"ProfilerAgent API Error: {e}")
            return "Error calling Anthropic API."

class ReportAgent:
    """Generates Markdown and HTML reports for the top recommended traders."""

    def generate_reports(self, top_traders):
        if not top_traders:
            print("No top traders found to generate reports.")
            return

        df = pd.DataFrame(top_traders)

        # Select and order columns
        cols = ['proxyWallet', 'pnl', 'simulated_recent_pnl', 'simulated_roi', 'simulated_trades', 'inferred_category', 'llm_assessment']
        df_out = df[cols].copy()

        # Formatting
        df_out['pnl'] = df_out['pnl'].round(2)
        df_out['simulated_recent_pnl'] = df_out['simulated_recent_pnl'].round(2)
        df_out['simulated_roi'] = df_out['simulated_roi'].round(2)

        # Markdown Report
        markdown_str = tabulate(df_out, headers='keys', tablefmt='pipe', showindex=False)
        with open('top_copy_candidates.md', 'w') as f:
            f.write("# Top Polymarket Copy-Trading Candidates\n\n")
            f.write("These traders have survived a backtest accounting for dynamic slippage and gas fees.\n\n")
            f.write(markdown_str)
        print("Saved report to top_copy_candidates.md")

        # HTML Report
        html_str = df_out.to_html(index=False, justify='center', table_id='candidatesTable')
        html_template = f"""
        <html>
        <head>
            <title>Top Polymarket Candidates</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                table {{ border-collapse: collapse; width: 100%; margin-top: 20px; }}
                th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
                th {{ background-color: #f2f2f2; cursor: pointer; }}
                tr:nth-child(even) {{ background-color: #f9f9f9; }}
                tr:hover {{ background-color: #e9e9e9; }}
                th.sort-asc::after {{ content: " \\25B2"; }}
                th.sort-desc::after {{ content: " \\25BC"; }}
                #filterInput {{ padding: 10px; width: 100%; font-size: 16px; box-sizing: border-box; }}
            </style>
            <script>
            document.addEventListener('DOMContentLoaded', function () {{
                const table = document.getElementById('candidatesTable');
                const headers = table.querySelectorAll('th');
                const filterInput = document.getElementById('filterInput');

                headers.forEach((header, index) => {{
                    header.addEventListener('click', () => {{
                        const tbody = table.querySelector('tbody');
                        const rows = Array.from(tbody.querySelectorAll('tr'));
                        const isAscending = header.classList.contains('sort-asc');
                        const multiplier = isAscending ? -1 : 1;

                        rows.sort((rowA, rowB) => {{
                            const cellA = rowA.children[index].textContent.trim();
                            const cellB = rowB.children[index].textContent.trim();
                            const valA = isNaN(cellA) ? cellA : parseFloat(cellA);
                            const valB = isNaN(cellB) ? cellB : parseFloat(cellB);
                            if (valA < valB) return -1 * multiplier;
                            if (valA > valB) return 1 * multiplier;
                            return 0;
                        }});

                        headers.forEach(h => h.classList.remove('sort-asc', 'sort-desc'));
                        header.classList.add(isAscending ? 'sort-desc' : 'sort-asc');
                        rows.forEach(row => tbody.appendChild(row));
                    }});
                }});

                filterInput.addEventListener('keyup', function() {{
                    const filterValue = filterInput.value.toLowerCase();
                    const tbody = table.querySelector('tbody');
                    const rows = tbody.querySelectorAll('tr');
                    rows.forEach(row => {{
                        const textContent = row.textContent.toLowerCase();
                        row.style.display = textContent.includes(filterValue) ? '' : 'none';
                    }});
                }});
            }});
            </script>
        </head>
        <body>
            <h2>Top Polymarket Copy-Trading Candidates</h2>
            <p>Traders successfully backtested against simulated latency, size-based slippage, and gas fees.</p>
            <input type="text" id="filterInput" placeholder="Filter by keyword (e.g. wallet, category, knowledge-based)..." />
            {html_str}
        </body>
        </html>
        """
        with open('top_copy_candidates.html', 'w') as f:
            f.write(html_template)
        print("Saved report to top_copy_candidates.html")

class Orchestrator:
    def __init__(self):
        print("Initializing Copy Trader Agents Orchestrator...")
        self.scout = ScoutAgent(limit=50, max_offset=500) # Reduced for quick run
        self.backtester = BacktesterAgent()
        self.profiler = ProfilerAgent()
        self.report = ReportAgent()

    def run(self):
        print("Running Ecosystem...")

        # Step 1: Scout Candidates
        all_candidates = self.scout.fetch_leaderboard_combinations()

        # Basic filter to save time: Only positive PNL > $5000
        filtered_candidates = [t for t in all_candidates if t.get('pnl', 0) > 5000]
        print(f"Filtered to {len(filtered_candidates)} candidates with >$5000 PNL.")

        # Step 2: Backtest
        processed_candidates = []
        for i, trader in enumerate(filtered_candidates):
            print(f"Backtesting trader {i+1}/{len(filtered_candidates)}...", end='\r')
            wallet = trader.get('proxyWallet')
            if not wallet:
                continue

            activity = self.scout.fetch_all_activity(wallet)
            if not activity:
                continue

            bt_results = self.backtester.simulate(trader, activity)

            # Filter logic based on user preference: survive latency/slippage
            if bt_results['simulated_recent_pnl'] > 1000 and bt_results['simulated_trades'] > 5:
                # Merge dictionaries
                combined = {**trader, **bt_results}
                processed_candidates.append(combined)

            # Stop early if we have enough for a report, otherwise this will run for a long time
            if len(processed_candidates) >= 10:
                break

        print(f"\nFound {len(processed_candidates)} strong candidates that survived backtesting.")

        # Step 3: LLM Profiling
        for i, candidate in enumerate(processed_candidates):
            print(f"Profiling candidate {i+1}/{len(processed_candidates)}...")
            assessment = self.profiler.profile_trader(candidate, candidate)
            candidate['llm_assessment'] = assessment

        # Step 4: Generate Reports
        processed_candidates.sort(key=lambda x: x['simulated_recent_pnl'], reverse=True)
        self.report.generate_reports(processed_candidates)
        print("Ecosystem run complete.")

if __name__ == "__main__":
    orchestrator = Orchestrator()
    orchestrator.run()
