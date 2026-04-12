"""Unified configuration for all three strategies."""

from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv
from src.config_validators import parse_addresses, validate_private_key, validate_address

# Load .env from project root (poly_poly_bot/)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


def _required(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ValueError(f"Missing required env var: {name}")
    return val


def _optional(name: str, fallback: str) -> str:
    return os.environ.get(name, "").strip() or fallback


def _opt_float(name: str, fallback: float) -> float:
    v = os.environ.get(name, "").strip()
    return float(v) if v else fallback


def _opt_int(name: str, fallback: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else fallback


def _opt_bool(name: str, fallback: bool) -> bool:
    v = os.environ.get(name, "").strip()
    if not v:
        return fallback
    return v.lower() == "true"


# --- Strategy 1 (Copy Trading) wallet loading ---

def _load_user_addresses() -> list[str]:
    """Load tracked wallets from USER_ADDRESSES or tiered strategy wallets."""
    raw = os.environ.get("USER_ADDRESSES", "").strip()
    if raw:
        addrs = parse_addresses(raw)
        for addr in addrs:
            validate_address(addr, "USER_ADDRESSES entry")
        return addrs
    tier1a = os.environ.get("STRATEGY_1A_WALLETS", "").strip()
    tier1b = os.environ.get("STRATEGY_1B_WALLETS", "").strip()
    if tier1a or tier1b:
        all_addrs: set[str] = set()
        if tier1a:
            for a in parse_addresses(tier1a):
                validate_address(a, "STRATEGY_1A_WALLETS")
                all_addrs.add(a)
        if tier1b:
            for a in parse_addresses(tier1b):
                validate_address(a, "STRATEGY_1B_WALLETS")
                all_addrs.add(a)
        return list(all_addrs)
    return []


def _load_private_key() -> str:
    """Load and validate private key, then remove from env."""
    raw = os.environ.get("PRIVATE_KEY", "").strip()
    if not raw:
        return ""
    validated = validate_private_key(raw)
    # Remove from env immediately — module const is the single source
    if "PRIVATE_KEY" in os.environ:
        del os.environ["PRIVATE_KEY"]
    return validated


_private_key = _load_private_key()


def get_private_key() -> str:
    """Access validated private key."""
    return _private_key


# Determine which strategies are enabled
_s1a_enabled = _opt_bool("STRATEGY_1A_ENABLED", bool(os.environ.get("STRATEGY_1A_WALLETS", "").strip()))
_s1b_enabled = _opt_bool("STRATEGY_1B_ENABLED", bool(os.environ.get("STRATEGY_1B_WALLETS", "").strip()))
_s1c_enabled = _opt_bool("STRATEGY_1C_ENABLED", False)
_strategy1_enabled = _s1a_enabled or _s1b_enabled or _s1c_enabled
_strategy2_enabled = _opt_bool("STRATEGY2_ENABLED", False)
_strategy3_enabled = _opt_bool("STRATEGY3_ENABLED", False)

_user_addresses = _load_user_addresses()

# Validate proxy wallet only when Strategy 1 is enabled
_proxy_wallet = ""
if _strategy1_enabled and os.environ.get("PROXY_WALLET", "").strip():
    _proxy_wallet = validate_address(_required("PROXY_WALLET"), "PROXY_WALLET")


class Config:
    """Unified configuration for all strategies."""

    # --- Strategy toggles ---
    strategy1_enabled: bool = _strategy1_enabled
    strategy2_enabled: bool = _strategy2_enabled
    strategy3_enabled: bool = _strategy3_enabled

    # --- Global ---
    preview_mode: bool = _opt_bool("PREVIEW_MODE", True)
    telegram_bot_token: str = _optional("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = _optional("TELEGRAM_CHAT_ID", "")

    # --- Strategy 1: Copy Trading ---
    user_addresses: list[str] = _user_addresses
    proxy_wallet: str = _proxy_wallet
    signature_type: int = _opt_int("SIGNATURE_TYPE", 0)

    copy_strategy: str = _optional("COPY_STRATEGY", "PERCENTAGE")
    copy_size: float = _opt_float("COPY_SIZE", 10.0)
    max_order_size_usd: float = _opt_float("MAX_ORDER_SIZE_USD", 100.0)
    min_order_size_usd: float = _opt_float("MIN_ORDER_SIZE_USD", 1.0)
    max_position_per_market_usd: float = _opt_float("MAX_POSITION_PER_MARKET_USD", 500.0)
    max_daily_volume_usd: float = _opt_float("MAX_DAILY_VOLUME_USD", 1000.0)
    fetch_interval: float = _opt_int("FETCH_INTERVAL", 1) * 1.0  # seconds
    fetch_concurrency: int = max(1, _opt_int("FETCH_CONCURRENCY", 5))
    max_trade_age_hours: float = _opt_float("MAX_TRADE_AGE_HOURS", 1.0)
    max_price_drift_bps: int = _opt_int("MAX_PRICE_DRIFT_BPS", 300)
    max_spread_bps: int = _opt_int("MAX_SPREAD_BPS", 500)
    max_copies_per_market_side: int = _opt_int("MAX_COPIES_PER_MARKET_SIDE", 2)
    redeem_interval_hours: float = _opt_float("REDEEM_INTERVAL_HOURS", 0.5)
    trade_monitor_mode: str = _optional("TRADE_MONITOR_MODE", "data-api")

    # --- Strategy 2: Weather Betting ---
    cities_to_bet: str = _optional("CITIES_TO_BET", "nyc,chicago,denver,dallas")
    days_in_advance: int = _opt_int("DAYS_IN_ADVANCE", 4)
    min_edge: float = _opt_float("MIN_EDGE", 0.10)
    bet_size: float = _opt_float("BET_SIZE", 10.0)
    max_bets_per_city: int = _opt_int("MAX_BETS_PER_CITY", 2)
    schedule_hour_sgt: int = _opt_int("SCHEDULE_HOUR_SGT", 15)
    schedule_minute_sgt: int = _opt_int("SCHEDULE_MINUTE_SGT", 0)

    # --- Strategy 3: Tennis Arb ---
    tennis_odds_provider: str = _optional("TENNIS_ODDS_PROVIDER", "oddspapi")
    oddspapi_api_key: str = _optional("ODDSPAPI_API_KEY", "")
    tennis_min_divergence: float = _opt_float("TENNIS_MIN_DIVERGENCE", 0.10)
    tennis_max_bet_size: float = _opt_float("TENNIS_MAX_BET_SIZE", 100.0)
    tennis_kelly_fraction: float = _opt_float("TENNIS_KELLY_FRACTION", 0.25)
    tennis_scan_interval: int = _opt_int("TENNIS_SCAN_INTERVAL", 300)
    tennis_tournaments: str = _optional("TENNIS_TOURNAMENTS", "ATP,WTA")
    tennis_min_polymarket_volume: float = _opt_float("TENNIS_MIN_POLYMARKET_VOLUME", 50000)
    tennis_min_polymarket_liquidity: float = _opt_float("TENNIS_MIN_POLYMARKET_LIQUIDITY", 10000)

    # --- APIs ---
    clob_api_url: str = _optional("CLOB_API_URL", "https://clob.polymarket.com")
    data_api_url: str = _optional("DATA_API_URL", "https://data-api.polymarket.com")
    rpc_url: str = _optional("RPC_URL", "https://polygon-rpc.com")
    etherscan_api_key: str = _optional("ETHERSCAN_API_KEY", "")
    chain_id: int = 137

    # --- Directories ---
    data_dir: str = str(Path(__file__).resolve().parent.parent / "data")
    cache_dir: str = str(Path(__file__).resolve().parent.parent / "cache")
    results_dir: str = str(Path(__file__).resolve().parent.parent / "results")
    logs_dir: str = str(Path(__file__).resolve().parent.parent / "logs")


CONFIG = Config()
