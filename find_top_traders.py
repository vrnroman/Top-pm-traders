import requests
import pandas as pd
import time
from datetime import datetime, timezone
import collections
from tabulate import tabulate
import concurrent.futures
import traceback

def fetch_leaderboard():
    limit = 50
    max_offset = 10000

    categories = ['OVERALL', 'SPORTS', 'POLITICS', 'CRYPTO', 'POP_CULTURE', 'BUSINESS', 'SCIENCE']
    time_periods = ['ALL', '1Y', '1M', '1W', '1D']
    order_bys = ['PNL', 'VOL']

    print(f"Fetching traders from leaderboard using all combinations to bypass the 10,000 limit...")

    unique_traders = {}
    total_combinations = len(categories) * len(time_periods) * len(order_bys)
    completed_combinations = 0

    for c in categories:
        for t in time_periods:
            for o in order_bys:
                completed_combinations += 1
                for offset in range(0, max_offset, limit):
                    url = f"https://data-api.polymarket.com/v1/leaderboard?category={c}&timePeriod={t}&orderBy={o}&limit={limit}&offset={offset}"
                    try:
                        response = requests.get(url, timeout=10)
                        if response.status_code == 200:
                            data = response.json()
                            if not data:
                                break

                            # API loop detection: if offset > 0 but first item matches last batch, API is looping.
                            if offset >= limit:
                                first_wallet_new = data[0].get('proxyWallet')
                                if first_wallet_new == last_first_wallet:
                                    break

                            last_first_wallet = data[0].get('proxyWallet') if data else None

                            for row in data:
                                wallet = row.get('proxyWallet')
                                if wallet and wallet not in unique_traders:
                                    unique_traders[wallet] = row
                        else:
                            break
                    except Exception:
                        break

                    time.sleep(0.01)

                print(f"Progress: [{completed_combinations}/{total_combinations}] Combinations. Unique traders so far: {len(unique_traders)}", end='\r')

    print(f"\nSuccessfully fetched {len(unique_traders)} unique top traders from leaderboard permutations.")
    return list(unique_traders.values())

def fetch_all_activity(wallet):
    all_activity = []
    url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=10000"
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            data = response.json()
            all_activity.extend(data)
    except Exception:
        pass
    return all_activity

def analyze_trader_deals(wallet, proxy_wallet, all_activity):
    markets = collections.defaultdict(list)
    for act in all_activity:
        if act.get('type') in ('TRADE', 'REDEEM', 'ADD_LIQUIDITY', 'REMOVE_LIQUIDITY'):
            condition_id = act.get('conditionId')
            if condition_id:
                markets[condition_id].append(act)

    deals = []
    earliest_time = float('inf')
    latest_time = 0

    now = datetime.now(timezone.utc).timestamp()
    two_months_ago = now - (60 * 24 * 3600)

    recent_pnl = 0
    total_trades_count = 0

    for condition_id, activities in markets.items():
        activities.sort(key=lambda x: x['timestamp'])

        buy_cost = 0
        sell_revenue = 0

        deal_earliest = float('inf')
        deal_latest = 0

        condition_trades_count = 0

        for act in activities:
            ts = act['timestamp']
            earliest_time = min(earliest_time, ts)
            latest_time = max(latest_time, ts)
            deal_earliest = min(deal_earliest, ts)
            deal_latest = max(deal_latest, ts)

            if act['type'] == 'TRADE':
                condition_trades_count += 1
                total_trades_count += 1
                if act.get('side') == 'BUY':
                    buy_cost += act.get('usdcSize', 0)
                elif act.get('side') == 'SELL':
                    sell_revenue += act.get('usdcSize', 0)
            elif act['type'] == 'REDEEM':
                sell_revenue += act.get('usdcSize', 0)

        profit = sell_revenue - buy_cost
        hold_time_seconds = deal_latest - deal_earliest
        is_profitable = profit > 0
        is_resolved = sell_revenue > 0

        # We need a way to extract the market. 'title' is available in the activity feed.
        titles = [a.get('title') for a in activities if a.get('title')]
        market_title = titles[0] if titles else "Unknown"

        if sell_revenue > 0 or condition_trades_count > 0:
            deal_info = {
                'condition_id': condition_id,
                'market_title': market_title,
                'profit': profit,
                'hold_time_hours': hold_time_seconds / 3600,
                'is_profitable': is_profitable,
                'is_resolved': is_resolved,
                'latest_ts': deal_latest,
                'trades_count': condition_trades_count
            }
            deals.append(deal_info)
            if deal_latest >= two_months_ago:
                recent_pnl += profit

    if earliest_time < float('inf'):
        active_duration_days = (latest_time - earliest_time) / (24 * 3600)
    else:
        active_duration_days = 0

    return {
        'deals': deals,
        'active_duration_days': active_duration_days,
        'recent_pnl': recent_pnl,
        'latest_activity': latest_time,
        'total_trades_count': total_trades_count
    }

def process_single_trader(t):
    try:
        proxy_wallet = t.get('proxyWallet')
        if not proxy_wallet:
            return None

        activity = fetch_all_activity(proxy_wallet)
        if not activity:
            return None

        stats = analyze_trader_deals(None, proxy_wallet, activity)

        deals = stats['deals']
        total_deals = len(deals)
        active_days = stats['active_duration_days']
        recent_pnl = stats['recent_pnl']
        total_trades = stats['total_trades_count']

        win_rate = 0
        short_hold_percentage = 0
        if total_deals > 0:
            resolved_deals = [d for d in deals if d['is_resolved']]
            resolved_count = len(resolved_deals)
            if resolved_count > 0:
                profitable_deals = sum(1 for d in resolved_deals if d['is_profitable'])
                win_rate = (profitable_deals / resolved_count) * 100
                short_holds = sum(1 for d in resolved_deals if d['hold_time_hours'] < 3)
                short_hold_percentage = (short_holds / resolved_count) * 100

        unique_markets = len(deals)
        avg_trades_per_market = (total_trades / unique_markets) if unique_markets > 0 else 0

        # Calculate most common market
        market_titles = [d.get('market_title') for d in deals if d.get('market_title') and d.get('market_title') != "Unknown"]
        most_common_market = "Unknown"
        if market_titles:
            most_common_market = collections.Counter(market_titles).most_common(1)[0][0]

        t['total_deals'] = total_deals
        t['active_duration_days'] = active_days
        t['recent_pnl'] = recent_pnl
        t['win_rate'] = win_rate
        t['short_hold_percentage'] = short_hold_percentage
        t['unique_markets_traded'] = unique_markets
        t['avg_trades_per_market'] = avg_trades_per_market
        t['most_common_market'] = most_common_market
        return t
    except Exception as e:
        pass
    return None

def process_traders():
    traders = fetch_leaderboard()

    promising_traders = []
    for t in traders:
        vol = t.get('vol', 0)
        pnl = t.get('pnl', 0)
        # Process those with >$2000 PNL overall as baseline
        if vol > 0 and pnl > 2000:
            roi = (pnl / vol) * 100
            t['roi_percentage'] = roi
            promising_traders.append(t)

    print(f"Filtered down to {len(promising_traders)} traders with basic >$2000 overall PNL criteria.")

    analyzed_traders = []

    print(f"Beginning concurrent analysis of {len(promising_traders)} traders...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(process_single_trader, t): t for t in promising_traders}

        done_count = 0
        total = len(futures)

        for future in concurrent.futures.as_completed(futures):
            done_count += 1
            if done_count % 100 == 0:
                print(f"Processed {done_count}/{total} traders...", end='\r')

            try:
                result = future.result()
                if result:
                    analyzed_traders.append(result)
            except Exception:
                pass

    print(f"\nFinished API analysis. Total analyzed with data: {len(analyzed_traders)}")

    print("Applying detailed heuristics filters...")
    final_traders = []
    for t in analyzed_traders:
        if t['active_duration_days'] < 60:
            continue
        if t['total_deals'] < 8:
            continue
        deals_per_week = t['total_deals'] / (t['active_duration_days'] / 7)
        if deals_per_week < 1:
            continue
        if t['short_hold_percentage'] > 15:
            continue
        if t['recent_pnl'] < 2000:
            continue

        final_traders.append(t)

    final_60 = [t for t in final_traders if t['win_rate'] >= 60]
    if len(final_60) > 0:
        return final_60

    print("No traders met 60% win rate. Lowering to 51%...")
    final_51 = [t for t in final_traders if t['win_rate'] >= 51]
    return final_51

def main():
    final_traders = process_traders()
    print(f"\nFinal successful traders: {len(final_traders)}")

    if final_traders:
        df = pd.DataFrame(final_traders)
        # Select important columns
        cols = ['userName', 'proxyWallet', 'pnl', 'roi_percentage', 'total_deals', 'win_rate', 'short_hold_percentage', 'recent_pnl', 'active_duration_days', 'unique_markets_traded', 'avg_trades_per_market', 'most_common_market']

        df_out = df[cols].copy()
        df_out['pnl'] = df_out['pnl'].round(2)
        df_out['roi_percentage'] = df_out['roi_percentage'].round(2)
        df_out['win_rate'] = df_out['win_rate'].round(2)
        df_out['short_hold_percentage'] = df_out['short_hold_percentage'].round(2)
        df_out['recent_pnl'] = df_out['recent_pnl'].round(2)
        df_out['active_duration_days'] = df_out['active_duration_days'].round(2)
        df_out['avg_trades_per_market'] = df_out['avg_trades_per_market'].round(2)

        # Sort by Win Rate and Recent PNL
        df_out = df_out.sort_values(by=['win_rate', 'recent_pnl'], ascending=[False, False])

        df_out.to_csv('top_traders.csv', index=False)
        print("Saved to top_traders.csv")

        markdown_str = tabulate(df_out, headers='keys', tablefmt='pipe', showindex=False)
        with open('top_traders.md', 'w') as f:
            f.write("# Top Polymarket Traders\n\n")
            f.write("Filtered for high win rate, consistency, and >3h hold times.\n\n")
            f.write(markdown_str)
        print("Saved to top_traders.md")

        html_str = df_out.to_html(index=False, justify='center', table_id='tradersTable')
        html_template = f"""
        <html>
        <head>
            <title>Top Polymarket Traders</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                table {{ border-collapse: collapse; width: 100%; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; text-align: center; }}
                th {{ background-color: #f2f2f2; cursor: pointer; }}
                tr:nth-child(even) {{ background-color: #f9f9f9; }}
                tr:hover {{ background-color: #ddd; }}
                th.sort-asc::after {{ content: " \\25B2"; }}
                th.sort-desc::after {{ content: " \\25BC"; }}
                #filterInput {{ margin-bottom: 20px; padding: 10px; width: 50%; font-size: 16px; }}
            </style>
            <script>
            document.addEventListener('DOMContentLoaded', function () {{
                const table = document.getElementById('tradersTable');
                const headers = table.querySelectorAll('th');
                const filterInput = document.getElementById('filterInput');

                // Sorting
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

                // Filtering
                filterInput.addEventListener('keyup', function() {{
                    const filterValue = filterInput.value.toLowerCase();
                    const tbody = table.querySelector('tbody');
                    const rows = tbody.querySelectorAll('tr');

                    rows.forEach(row => {{
                        const textContent = row.textContent.toLowerCase();
                        if (textContent.includes(filterValue)) {{
                            row.style.display = '';
                        }} else {{
                            row.style.display = 'none';
                        }}
                    }});
                }});
            }});
            </script>
        </head>
        <body>
            <h2>Top Polymarket Traders</h2>
            <p>Traders with a high win rate, regular activity, active > 2 months, low frequency of &lt;3h trades, and >= $2000 PNL in the last 2 months.</p>
            <p><strong>Note:</strong> Click on any column header to sort the table.</p>
            <input type="text" id="filterInput" placeholder="Filter traders by searching any text (e.g., 'Politics', wallet address)..." />
            {html_str}
        </body>
        </html>
        """
        with open('top_traders.html', 'w') as f:
            f.write(html_template)
        print("Saved to top_traders.html")
    else:
        print("No traders matched the criteria.")

if __name__ == "__main__":
    main()
