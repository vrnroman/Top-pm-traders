import requests
import json
import time

def fetch_activity(wallet):
    url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=1000"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Error: {response.status_code}")
        return []

activities = fetch_activity("0x56687bf447db6ffa42ffe2204a05edaa20f55839")
print(f"Fetched {len(activities)} activities for top trader.")
if activities:
    print(f"Earliest activity TS: {activities[-1]['timestamp']}")
