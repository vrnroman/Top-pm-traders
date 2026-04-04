import requests
import pandas as pd
import time
from datetime import datetime, timezone
import collections
from tabulate import tabulate
import json

def fetch_leaderboard(max_traders=500):
    limit = 50
    all_traders = []

    for offset in range(0, max_traders, limit):
        url = f"https://data-api.polymarket.com/v1/leaderboard?category=OVERALL&timePeriod=ALL&orderBy=PNL&limit={limit}&offset={offset}"
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            if not data:
                break
            all_traders.extend(data)
        else:
            print(f"Error fetching leaderboard: {response.status_code}")
            break
        time.sleep(0.1)

    return all_traders

def fetch_all_activity(wallet):
    all_activity = []
    url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=10000"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            all_activity.extend(data)
    except requests.exceptions.RequestException as e:
        print(f"Error fetching activity for {wallet}: {e}")
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

    for condition_id, activities in markets.items():
        activities.sort(key=lambda x: x['timestamp'])

        buy_cost = 0
        sell_revenue = 0

        deal_earliest = float('inf')
        deal_latest = 0

        for act in activities:
            ts = act['timestamp']
            earliest_time = min(earliest_time, ts)
            latest_time = max(latest_time, ts)
            deal_earliest = min(deal_earliest, ts)
            deal_latest = max(deal_latest, ts)

            if act['type'] == 'TRADE':
                if act.get('side') == 'BUY':
                    buy_cost += act.get('usdcSize', 0)
                elif act.get('side') == 'SELL':
                    sell_revenue += act.get('usdcSize', 0)
            elif act['type'] == 'REDEEM':
                sell_revenue += act.get('usdcSize', 0)

        profit = sell_revenue - buy_cost
        hold_time_seconds = deal_latest - deal_earliest
        is_profitable = profit > 0

        if sell_revenue > 0:
            deal_info = {
                'condition_id': condition_id,
                'profit': profit,
                'hold_time_hours': hold_time_seconds / 3600,
                'is_profitable': is_profitable,
                'latest_ts': deal_latest
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
        'latest_activity': latest_time
    }

def process_traders():
    # To get best results, we fetch top 1000
    print("Fetching top traders leaderboard (up to 1000 traders)...")
    traders = fetch_leaderboard(max_traders=1000)
    print(f"Fetched {len(traders)} total traders from leaderboard.")

    promising_traders = []
    for t in traders:
        vol = t.get('vol', 0)
        pnl = t.get('pnl', 0)
        if vol > 0 and pnl > 2000:
            roi = (pnl / vol) * 100
            t['roi_percentage'] = roi
            promising_traders.append(t)

    print(f"Filtered down to {len(promising_traders)} traders with basic >$2000 PNL criteria.")

    analyzed_traders = []
    total = len(promising_traders)
    for i, t in enumerate(promising_traders):
        proxy_wallet = t.get('proxyWallet')
        if not proxy_wallet:
            continue

        if i % 10 == 0:
            print(f"[{i+1}/{total}] Analyzing trades...", end='\r')

        activity = fetch_all_activity(proxy_wallet)
        if not activity:
            continue

        stats = analyze_trader_deals(None, proxy_wallet, activity)

        total_deals = len(stats['deals'])
        active_days = stats['active_duration_days']
        recent_pnl = stats['recent_pnl']

        deals = stats['deals']
        win_rate = 0
        short_hold_percentage = 0
        if total_deals > 0:
            profitable_deals = sum(1 for d in deals if d['is_profitable'])
            win_rate = (profitable_deals / total_deals) * 100
            short_holds = sum(1 for d in deals if d['hold_time_hours'] < 3)
            short_hold_percentage = (short_holds / total_deals) * 100

        t['total_deals'] = total_deals
        t['active_duration_days'] = active_days
        t['recent_pnl'] = recent_pnl
        t['win_rate'] = win_rate
        t['short_hold_percentage'] = short_hold_percentage
        analyzed_traders.append(t)

    print("\nFinished API analysis.")

    print("\nApplying detailed heuristics filters...")
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
        cols = ['userName', 'proxyWallet', 'rank', 'pnl', 'roi_percentage', 'total_deals', 'win_rate', 'short_hold_percentage', 'recent_pnl', 'active_duration_days']

        # Add proxyWallet explicitly if not displayed
        df_out = df[cols].copy()

        # Round decimals
        df_out['pnl'] = df_out['pnl'].round(2)
        df_out['roi_percentage'] = df_out['roi_percentage'].round(2)
        df_out['win_rate'] = df_out['win_rate'].round(2)
        df_out['short_hold_percentage'] = df_out['short_hold_percentage'].round(2)
        df_out['recent_pnl'] = df_out['recent_pnl'].round(2)
        df_out['active_duration_days'] = df_out['active_duration_days'].round(2)

        # CSV
        df_out.to_csv('top_traders.csv', index=False)
        print("Saved to top_traders.csv")

        # Markdown
        markdown_str = tabulate(df_out, headers='keys', tablefmt='pipe', showindex=False)
        with open('top_traders.md', 'w') as f:
            f.write("# Top Polymarket Traders\n\n")
            f.write("Filtered for high win rate, consistency, and >3h hold times.\n\n")
            f.write(markdown_str)
        print("Saved to top_traders.md")

        # HTML
        html_str = df_out.to_html(index=False, justify='center')
        html_template = f"""
        <html>
        <head>
            <title>Top Polymarket Traders</title>
            <style>
                table {{ border-collapse: collapse; width: 100%; font-family: Arial, sans-serif; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; text-align: center; }}
                th {{ background-color: #f2f2f2; }}
                tr:nth-child(even) {{ background-color: #f9f9f9; }}
                tr:hover {{ background-color: #ddd; }}
            </style>
        </head>
        <body>
            <h2>Top Polymarket Traders</h2>
            <p>Traders with a high win rate, regular activity, active > 2 months, low frequency of <3h trades, and >= $2000 PNL in the last 2 months.</p>
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
