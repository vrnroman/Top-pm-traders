"""Odds data fetching module — pluggable providers for sharp sportsbook odds."""

from src.odds.base import OddsProvider
from src.odds.models import MatchOdds, OddsComparison
from src.odds.oddspapi import OddsPapiProvider
from src.odds.scraper import OddsScraperProvider
from src.odds.smarkets import SmarketsProvider

__all__ = [
    "OddsProvider",
    "MatchOdds",
    "OddsComparison",
    "OddsPapiProvider",
    "OddsScraperProvider",
    "SmarketsProvider",
]
