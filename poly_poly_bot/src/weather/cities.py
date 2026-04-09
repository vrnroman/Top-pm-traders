"""City configuration for weather betting bot."""

CITIES = {
    # US cities (Fahrenheit, 2°F range buckets on Polymarket)
    "nyc": {
        "name": "New York City", "lat": 40.7128, "lon": -74.0060,
        "tz": "America/New_York", "unit": "fahrenheit",
        "slug": "nyc",
    },
    "chicago": {
        "name": "Chicago", "lat": 41.8781, "lon": -87.6298,
        "tz": "America/Chicago", "unit": "fahrenheit",
        "slug": "chicago",
    },
    "los-angeles": {
        "name": "Los Angeles", "lat": 34.0522, "lon": -118.2437,
        "tz": "America/Los_Angeles", "unit": "fahrenheit",
        "slug": "los-angeles",
    },
    "dallas": {
        "name": "Dallas", "lat": 32.7767, "lon": -96.7970,
        "tz": "America/Chicago", "unit": "fahrenheit",
        "slug": "dallas",
    },
    "denver": {
        "name": "Denver", "lat": 39.7392, "lon": -104.9903,
        "tz": "America/Denver", "unit": "fahrenheit",
        "slug": "denver",
    },
    "miami": {
        "name": "Miami", "lat": 25.7617, "lon": -80.1918,
        "tz": "America/New_York", "unit": "fahrenheit",
        "slug": "miami",
    },
    "atlanta": {
        "name": "Atlanta", "lat": 33.7490, "lon": -84.3880,
        "tz": "America/New_York", "unit": "fahrenheit",
        "slug": "atlanta",
    },
    "seattle": {
        "name": "Seattle", "lat": 47.6062, "lon": -122.3321,
        "tz": "America/Los_Angeles", "unit": "fahrenheit",
        "slug": "seattle",
    },
    "houston": {
        "name": "Houston", "lat": 29.7604, "lon": -95.3698,
        "tz": "America/Chicago", "unit": "fahrenheit",
        "slug": "houston",
    },
    "austin": {
        "name": "Austin", "lat": 30.2672, "lon": -97.7431,
        "tz": "America/Chicago", "unit": "fahrenheit",
        "slug": "austin",
    },
    "san-francisco": {
        "name": "San Francisco", "lat": 37.7749, "lon": -122.4194,
        "tz": "America/Los_Angeles", "unit": "fahrenheit",
        "slug": "san-francisco",
    },
    "phoenix": {
        "name": "Phoenix", "lat": 33.4484, "lon": -112.0740,
        "tz": "America/Phoenix", "unit": "fahrenheit",
        "slug": "phoenix",
    },
    "dc": {
        "name": "Washington DC", "lat": 38.9072, "lon": -77.0369,
        "tz": "America/New_York", "unit": "fahrenheit",
        "slug": "dc",
    },
    # International cities (Celsius, single-degree buckets)
    "london": {
        "name": "London", "lat": 51.5074, "lon": -0.1278,
        "tz": "Europe/London", "unit": "celsius",
        "slug": "london",
    },
    "hong-kong": {
        "name": "Hong Kong", "lat": 22.3193, "lon": 114.1694,
        "tz": "Asia/Hong_Kong", "unit": "celsius",
        "slug": "hong-kong",
    },
    "tokyo": {
        "name": "Tokyo", "lat": 35.6762, "lon": 139.6503,
        "tz": "Asia/Tokyo", "unit": "celsius",
        "slug": "tokyo",
    },
    "seoul": {
        "name": "Seoul", "lat": 37.5665, "lon": 126.9780,
        "tz": "Asia/Seoul", "unit": "celsius",
        "slug": "seoul",
    },
    "paris": {
        "name": "Paris", "lat": 48.8566, "lon": 2.3522,
        "tz": "Europe/Paris", "unit": "celsius",
        "slug": "paris",
    },
    "toronto": {
        "name": "Toronto", "lat": 43.6532, "lon": -79.3832,
        "tz": "America/Toronto", "unit": "celsius",
        "slug": "toronto",
    },
}
