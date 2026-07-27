# Pinned to bookworm: the bare `python:3.12-slim` tag rolled to Debian 13
# (trixie) on 2026-07-27, whose own nodejs 20 package shadowed the nodesource
# repo and splits npm into a separate package — the build died at
# `npm: not found`. Pin the distro so upstream tag drift can't break the build.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ && rm -rf /var/lib/apt/lists/*

# Node + Claude Code CLI. The wallet-discovery LLM gate (Strategy 1c) shells out
# to `claude -p`, which runs on the operator's Claude subscription
# (CLAUDE_CODE_OAUTH_TOKEN — from `claude setup-token`) rather than an
# ANTHROPIC_API_KEY. The gate is fail-open, so if the token is unset the bot
# still runs (new wallets are admitted ungated).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

# glibc malloc tuning. This is a ~19-thread process using numpy/pandas/scipy/
# web3. By default glibc spins up to 8*ncpu per-thread malloc arenas, each of
# which hoards freed memory and never returns it to the OS — so RSS balloons
# far above live-object size (measured 2026-06-15: ~1.44GB anonymous heap vs a
# ~210MB code floor). Capping arenas + lowering the trim threshold reclaims
# freed heap, cutting steady-state and post-discovery-sweep RSS substantially.
ENV MALLOC_ARENA_MAX=2 \
    MALLOC_TRIM_THRESHOLD_=131072

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Pre-deploy gates: lint (F821 undefined names) + smoke (import every
# production module, including lazy imports inside helper functions).
# This catches the class of bug that took the bot down on 2026-05-10
# (latent NameErrors / ImportErrors in cold paths). Build fails here →
# deploy script never ships a broken image.
RUN pip install --no-cache-dir ruff && \
    ruff check --select F821 src main.py && \
    python -c "\
import main; \
from src.copy_trading import runner, trade_executor, inventory, clob_client, order_executor; \
from src import telegram_bot; \
from src.copy_trading.trade_executor import _trade_store, _trade_queue, _inventory, _risk_manager, _tiered_risk, _strategy_config; \
_trade_store(); _trade_queue(); _inventory(); _risk_manager(); _tiered_risk(); _strategy_config(); \
print('SMOKE OK')" && \
    pip uninstall -y ruff

RUN mkdir -p data cache results logs

RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "main.py"]
