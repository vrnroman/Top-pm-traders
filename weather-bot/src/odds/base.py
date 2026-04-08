"""Abstract base class for odds providers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.odds.models import MatchOdds


class OddsProvider(ABC):
    """Interface for fetching sharp sportsbook odds."""

    @abstractmethod
    def fetch_tennis_odds(self, tours: list[str] | None = None) -> list[MatchOdds]:
        """Fetch current tennis match odds.

        Args:
            tours: Filter by tour, e.g. ["ATP", "WTA"]. None = all.

        Returns:
            List of MatchOdds for upcoming tennis matches.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name for logging."""
        ...
