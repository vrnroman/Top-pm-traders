import requests
import concurrent.futures

urls = [f"https://data-api.polymarket.com/v1/leaderboard?category=OVERALL&timePeriod=ALL&orderBy=PNL&limit=50&offset={i*50}" for i in range(100)]

def fetch(url):
    res = requests.get(url)
    return res.status_code

with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
    results = list(executor.map(fetch, urls))

from collections import Counter
print(Counter(results))
