# Roadmap — poly_poly_bot

**Written 2026-07-25** from a full read of the code, the production VM logs, the
live paper ledgers, and Langfuse. Supersedes `BACKLOG.md` (deleted; its one live
item is carried into P1-5b below, the rest was shipped).

> **STATUS 2026-07-28 (P1 batch SHIPPED, commits `6bc4837`, `b661126`, `+config`;
> 931 tests green):** P1-1..P1-7 all landed (details at the top of §4). Two prod
> findings fell out of the P1-1 load check: the discovery disk caches
> (wcache/rescache) had been **silently dead since ~2026-06** (dirs never
> created; every sweep re-fetched everything — 18 of the 24 min sweep), now
> self-healing in code; and the §1.7b holdout anomaly **resolved itself** — the
> first holdout fired 2026-07-27 16:03 UTC (the 22-day drought was the p≈5%
> unlucky streak, wiring was correct all along). The counterfactual clock
> P1-5b needs has started.
>
> **STATUS 2026-07-25 (same day, commits `ec84422` + `1ba9647`): P0-1..P0-4 and
> P2-1/P2-2 SHIPPED and live on the VM.** The fill model is fixed (0.97 floor,
> two-sided gate), `/pnl` and the AB-RACE snapshot carry at-their-price ROI +
> fill-health + split-half persistence next to the §7 kill bar, the race
> verdict was voided (`ab_race_state.json`: `verdict_sent:false`,
> `era_floor_ts=1784976482`), and `scripts/rebaseline_ledger.py` reproduces
> §1.2/§1.3 exactly from the live ledgers. **Open watches:** (1) P0-1's 48h
> acceptance (`rebaseline_ledger.py --era`, due ~07-27: avg drag ≥ 0, zero rows
> < −300bps); (2) the §7 kill-criterion clock runs on clean fills from
> 2026-07-25, verdict due ~08-22; (3) tomorrow's 08:00 UTC snapshot is the
> first clean-era one.

This file is meant to be self-contained: a fresh session should be able to read
only this and start working. Evidence and reproduction steps are included so
nobody has to re-derive the numbers or re-litigate the conclusions.

---

## 0. State of play

The bot is a Polymarket copy-trader. It is running on the GCP VM in
`PREVIEW_MODE=true` — **no real money is at risk and none has been traded.**
Two measurement harnesses run in background threads:

- **Book A** — lagged copy, fills simulated by walking the live asks book.
- **Book B** — instant copy, flat 100 bps slippage penalty.

Wallet discovery hunts candidate wallets into a watchlist (27 wallets as of
2026-07-24); an LLM gate (`claude-opus-4-8`) vets new admissions; a statistical
promotion gate decides when a wallet is offered for real capital; `/golive`
re-checks before the manual `PREVIEW_MODE=false` flip.

**The headline conclusion of the 2026-07-25 analysis: the system currently has
no measurable edge, and the apparent edge it reported was an artifact of its own
fill simulator.** No wallet is ready for real money. Details in §1.

Two promoted wallets exist in `promoted_wallets.json`
(`0x48611e62…`, `0x5674f607…`) — both promoted via Telegram, both now sitting at
`ready: false` in `golive_watch.json`, and both auto-demoted in book B. Nothing
is pending a go-live decision.

---

## 1. Evidence base

Recomputed independently from the raw production ledgers, not read off the bot's
own reports. **Do not re-derive these before acting — reproduce them only if you
change the fill model (P0-1), which invalidates the realized column.**

### 1.1 The fill simulator gifts price — this is the whole "edge"

`MIN_FILL_FRAC = 0.5` (`src/copy_trading/copy_paper.py:37`) lets the simulator
sweep ask levels down to **half** the price the target paid.

```
79 of 201 settled A-copies (39%) filled >2% BETTER than the target.
Those 79:  realized +$377.8   |   at the target's own price  -$157.5
           => the fill model gifted +$535.3
Book A's ENTIRE net PnL is +$537.5.
```

Worst offenders (the −5000 bps entries are the `MIN_FILL_FRAC` clamp binding):

```
drag -5000bps  their 0.560 -> ours 0.280   Bitcoin Up or Down - July 21, 5:35AM
drag -4797bps  their 0.517 -> ours 0.269   Will Japan win on 2026-06-25?   pnl +135.9
drag -3987bps  their 0.440 -> ours 0.265   Spread: Arizona Diamondbacks    pnl +139.0
```

Compounding it: `copy_paper.py:586` gates on `fill.drag_bps > fill_gate_bps` —
**one-sided**. Fills 1.5% *worse* than the target are rejected; fills 50%
*better* are kept. A filter that rejects bad luck and keeps good luck
manufactures alpha by construction.

### 1.2 The honest returns

| Book | n settled | Capital | Realized ROI | ROI at target's own price |
|---|---|---|---|---|
| A (lagged) | 201 | $6,288 | **+8.55%** | **−0.11%** |
| A, clean fills only (drag ≥ −200bps) | 122 | $3,965 | +4.03% | — |
| B (instant) | 214 | $5,449 | −7.68% | −6.69% |
| **A + B combined** | **415** | **$11,737** | — | **−3.17%** |

Combined per-bet mean return **−4.05%**, SE 4.62%, **t = −0.88**,
95% CI **[−13.1%, +5.0%]** — and that is *before* Polymarket fees and Polygon
gas, neither of which either harness models.

### 1.3 Wallet selection has negative persistence

Per-wallet ROI split chronologically in half, all wallets with n ≥ 10:

```
A:  0x1e3b6822  1st +37.2%  2nd  +9.3%  HELD
    0x48611e62  1st +18.2%  2nd -15.4%  FLIPPED
    0x8dee870d  1st -16.7%  2nd +25.0%  FLIPPED
    0x161a7f66  1st  -6.8%  2nd -47.2%  HELD
    0x5674f607  1st +14.9%  2nd -23.8%  FLIPPED
    0x4a3f86ed  1st -12.7%  2nd  +8.2%  FLIPPED
    0x37c1ff27  1st +42.0%  2nd -20.3%  FLIPPED
B:  0x4a3f86ed  1st +10.2%  2nd  -7.4%  FLIPPED
    0xeef6ad0e  1st +37.8%  2nd -24.7%  FLIPPED
    0x48611e62  1st  +9.7%  2nd -29.7%  FLIPPED
    0x5674f607  1st +17.4%  2nd -29.8%  FLIPPED
    0x161a7f66  1st -44.0%  2nd -20.7%  HELD
    0x37c1ff27  1st +26.1%  2nd -17.1%  FLIPPED

corr(1st half, 2nd half) = -0.184 (A), -0.099 (B).  11 of 13 flipped sign.
```

A wallet looking good is, empirically, slightly *negative* information about what
it does next. **The bot has never computed this.** It is the single number that
says whether copy-trading works at all.

### 1.4 The "winners" are small samples that then go dark

Book A wallets bucketed by settled count:

```
n in [1,4]    17 wallets   $1,216   +12.6%
n in [5,14]    5 wallets   $1,349   +35.9%
n in [15,inf]  6 wallets   $3,723    -2.7%   <- the only ones with enough data
```

Every headline wallet (+80.5%, +53.3%, +22.0%) has n ≤ 11 **and has been dark for
22–30 days.** The wallets still trading sit at −12% to +4.5%.

Book A by week, settled: `W25 +60.4% | W26 +29.2% | W27 +15.5% | W28 −14.9% |
W29 −22.7% | W30 −15.5%`.

### 1.5 Category and price concentration

```
A: sports n=148 $4,402  -1.0%  |  other n=41 $1,455 +26.0%  |  crypto n=9 $375 +46.5%
B: sports n=185 $4,786  -8.8%  |  other n=17   $417  +4.6%  |  crypto n=5  $90 -22.9%
B price bucket [0.2,0.4): n=19, ROI -61.5%, win rate 16% (breakeven ~30%)
```

Sports is 333 of 415 copies and is the losing category in both books. The
positive slices are too small to act on (n=41, n=9) — this is a reason to **cut
the known loser**, not to chase the apparent winner.

### 1.6 The funnel is not searching

`WALLET_DISCOVERY_SKILL_POOL=40` (`config.py:404`),
`WALLET_DISCOVERY_INTERVAL_S=86400` (`config.py:402`). Production DISCOVERY log
lines, one sweep per day:

```
2026-07-19  swept=43 qualified=23 new=0 removed=1 watchlist=23
2026-07-20  swept=43 qualified=24 new=1 removed=0 watchlist=24
2026-07-21  swept=42 qualified=24 new=0 removed=0 watchlist=24
2026-07-22  swept=44 qualified=24 new=1 removed=1 watchlist=24
2026-07-23  swept=41 qualified=25 new=2 removed=1 watchlist=25
2026-07-24  swept=43 qualified=27 new=3 removed=1 watchlist=27
```

The same ~43 wallets are re-ranked daily. `consensus: ... 0 cells>=k · 0 new`
every single day — the consensus detector has never fired.

The top of the watchlist is ranked on raw wallet ROI with **no copy evidence at
all**: `roi=6.26 t=61.2 copy_n=0`, `roi=6.14 t=22.6 copy_n=0`. Every LLM
rejection says the same sentence — *"the classic signature of settlement-lag
scooping near $1 which a delayed copier cannot capture."* The screen manufactures
scoopers; Claude pays $0.09 a time to kill them.

Evidence intake is also throttled: on 2026-07-24 the harness saw 34,779 target
trade rows and rejected **28,334 (81.5%) as below the $300 minimum**
(`copy_paper_min_usd`, `config.py:191`), netting ~15 copies/day across the whole
watchlist. Meanwhile `COPY_GOLIVE_MIN_SETTLED=30` (`config.py:364`) and wallets
go dark after ~20 copies — **nothing can ever reach the bar before its wallet
dies.** Lowering the bar is not the fix; more wallets in parallel is.

### 1.7 Gate integrity

`data/gate-history.jsonl`, 71 decisions over 22 days:

```
verdicts: skip 28 | watch 25 | follow 6 | admit-fail-open 9 | skip-deferred 3
admitted: 43     holdout admits: 0     confidence bands: low 25, medium 24, high 5
```

Langfuse, 99 traces since 2026-06-29 (`wallet-gate` 88, `promotion-gate` 9):

```
verdicts: skip 43 | watch 32 | follow 7 | NULL OUTPUT 15 | other 2
latency: median 15.9s, max 45.2s      error traces: 0
total spend ~$8 (~$0.30/day), model claude-opus-4-8
```

Three problems: **(a)** ~13–15% of gate calls fail open — the wallet is admitted
unvetted and **zero error traces are recorded**, so the failures are invisible in
telemetry. **(b)** `GATE_HOLDOUT_FRAC=0.1` is correctly wired (`main.py:826`) yet
produced **0 holdouts across 28 skips**; expected ~3, p(zero) ≈ 5% — unlucky or
broken, unresolved. **(c)** the dossier the gate judges is full of nulls
(`n_closed`, `capital`, `hit_rate`, `concentration`, `mean_entry_price`,
`up_ratio` all `null` in live traces) while `why_flagged` simultaneously claims
"hit 100% over 455 closed markets, ROI +613%" against a `pnl_curve.net_pnl` of
$767.

### 1.8 Server health

Healthy. VM up 39 days, container `RestartCount=0` since 2026-07-17, load 0.11,
disk 7.0G/20G (38%), RAM 1.1G/2.0G. **No ERROR/WARNING/CRITICAL/tracebacks in
seven days of logs.**

The problems are cruft, not failure:

- **93.5% of log volume is noise** — 68,889 of 73,666 lines on 2026-07-24 are
  `urllib3.connectionpool` DEBUG from the Telegram long-poll. Real signal is
  ~4,500 lines/day. Root cause: `main.py:885` sets the **root** logger to DEBUG
  with a DEBUG file handler (`main.py:880`), capturing every third-party record.
- Log retention **works correctly** — `_purge_old_bot_logs` runs on startup and
  every midnight rollover; prod sets `BOT_LOG_RETENTION_DAYS=20`, so 19 days on
  disk is the configured window, not a leak. Fix the noise, not the retention.
- Docker json-file log driver has **no size cap** (`deploy.sh:178`), 18 MB and
  growing.
- ~14 MB of dead data in `data/` from strategies purged in `5f7a127` (2026-07-05).
- 1.05 GB reclaimable Docker images.
- **Port 22 open to `0.0.0.0/0`** via the `default-allow-ssh` project rule (hence
  constant scanner hits in journalctl). RDP 3389 and two VNC 5901 rules are also
  world-open. This VM holds a Polymarket private key.

### 1.9 Caveats on the above

- The −3.17% figure rests on `ideal_pnl` being computed correctly at
  `their_price` — verified at `copy_paper.py:153` and spot-checked against raw
  rows, but it shares the ledger's own resolution data. If market resolution is
  mis-recorded anywhere, both this number and the bot's inherit it.
- The persistence result (§1.3) is **independent of the fill model** and is the
  finding to trust most.
- n=9 crypto and n=41 "other" are too small to be findings. Treat them as noise.
- The holdout anomaly (§1.7b) is p≈5% — suspicious, not proven broken.

---

## 2. How to reproduce

Pull the live prod state (read-only; keys never leave the VM):

```bash
VM="gcloud compute ssh poly-poly-bot --zone asia-northeast1-a --project roman-vm --tunnel-through-iap"

# ledgers + gate + promotion state
$VM --command "cd /home/tianyuezhou/app/data && sudo tar czf /tmp/pm.tgz \
  copy_paper_ledger.jsonl copy_paper_ledger_b.jsonl copy_watchlist.json \
  gate-history.jsonl discovery_state.json promotion_offers*.json \
  promoted_wallets.json golive_watch.json copy_blacklist*.json && sudo base64 /tmp/pm.tgz" \
  | grep -vE 'WARNING|NumPy|cloud.google|please see' | tr -d '\n ' | base64 -d > pm.tgz
tar xzf pm.tgz
```

Core numbers from the ledgers (note `copy_id` is append-with-update — last row
per id wins, or you will double-count):

```python
import json, math, collections
def load(p):
    d = {}
    for l in open(p):
        if l.strip():
            r = json.loads(l); d[r["copy_id"]] = r
    return [r for r in d.values() if r.get("closed") and (r.get("spent") or 0) > 0]

for lab, p in (("A", "copy_paper_ledger.jsonl"), ("B", "copy_paper_ledger_b.jsonl")):
    rs = load(p)
    sp = sum(r["spent"] for r in rs)
    print(lab, "realized", sum(r.get("pnl") or 0 for r in rs) / sp,
               "at-their-price", sum(r.get("ideal_pnl") or 0 for r in rs) / sp)
```

Split-half persistence (§1.3): group by `target`, sort each wallet's rows by
`closed_ts`, compare `sum(pnl)/sum(spent)` of the first half vs the second,
then Pearson-correlate the pairs across wallets.

Langfuse (run **on the VM** so keys stay there):

```bash
$VM --command 'eval "$(docker exec poly-poly-bot env | grep -E "^LANGFUSE_" | sed "s/^/export /")"
AUTH=$(printf "%s:%s" "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64 -w0)
curl -s -H "Authorization: Basic $AUTH" "$LANGFUSE_HOST/api/public/traces?limit=100"'
```

---

## 3. P0 — the measurement instrument is broken

**Nothing downstream is trustworthy until P0-1 and P0-2 land. Do not tune
thresholds, promote wallets, or judge strategies before then.**

> **P0-1..P0-4 all SHIPPED 2026-07-25** (`ec84422`, `1ba9647`; 886 tests green).
> What landed beyond the letter of the items (owner asked "think what else
> could be needed", manager-approved): a standing **fill-health witness**
> (`copy_paper.fill_health`; avg/min drag, % better-than-target, deep-gift
> count, rendered in `/pnl` and the daily snapshot), a **divergence tripwire**
> (|realized − @price| > 200bps on ≥10 settled → `⚠SUSPECT-fills` + WARNING
> log — would have flagged the 2026-07 artifact months early), split-half corr
> computed on **both** realized and ideal returns, `era_state.py` as the single
> clean-era marker every surface scopes by, and a verifier-caught guard so
> `split_half_corr` can never fabricate a correlation from float noise.
> Deferred to the owner (see the end-of-run desk): whether `/golive` +
> `golive_watch` should also floor on at-their-price ROI + persistence.
> **RESOLVED same day (owner: option A): shipped `80e6dc4`, review-hardened
> `2668a81`** — the go-live gate now requires at-price ROI ≥ 0 on ≥5 clean
> settles and book split-half corr ≥ 0, clean-era data only, fail-closed while
> the era is young (`COPY_GOLIVE_HONEST_METRICS=false` restores the legacy
> gate; recalibrate floor values at the 08-22 verdict).
>
> **48H ACCEPTANCE READ (2026-07-27, prod):** deep-gift 0 on both books (the
> artifact channel is closed). A: 4 clean settles, avg drag −11bps (under the
> ≥0 criterion on tiny n; the two-sided gate makes small negative averages
> possible by design — keep reading as clean n accrues). A realized ≈
> at-price (−22.9% vs −23.1%): realized is an honest number again. The 07-26
> and 07-27 daily snapshots both went out era-floored.

### P0-1 · Stop the fill simulator gifting price — **SHIPPED `ec84422`**
- **Why:** §1.1 — 39% of A-copies filled better than the target; that alone is
  the book's entire profit.
- **Change:** `src/copy_trading/copy_paper.py:37` — `MIN_FILL_FRAC = 0.5` → `0.97`.
  A credible same-side ask cannot sit 3%+ under what the target just paid; at 0.5
  the simulator sweeps stale book data.
- **Also:** `copy_paper.py:586` — make the fill gate two-sided
  (`abs(fill.drag_bps) > self.fill_gate_bps`) or add a separate favourable-side
  floor. `copy_paper_fill_gate_bps=150` at `config.py:217`.
- **Acceptance:** after 48h of fresh copies, `avg_drag_bps` in `/pnl` is ≥ 0 and
  no new ledger row has `drag_bps < -300`.
- **Effort:** ~1h incl. tests.

### P0-2 · Re-baseline both books from `ideal_pnl` — **SHIPPED `ec84422`**
- **Why:** §1.2 — the honest combined number is −3.17%, not +8.55%.
- **Change:** new `scripts/rebaseline_ledger.py` re-scoring
  `copy_paper_ledger*.jsonl` on `ideal_pnl` (already stored per row,
  `copy_paper.py:153`), emitting per-wallet and per-book ROI. Then make
  `pnl_unified` report **both** realized and at-their-price ROI, permanently and
  side by side.
- **Why this shape:** keeps two months of history instead of discarding it, and
  produces a number the fill model cannot inflate.
- **Acceptance:** `/pnl` shows both columns; at-their-price matches the script.
- **Effort:** ~3h.

### P0-3 · Invalidate the A-vs-B race verdict — **SHIPPED `ec84422`**
- **Why:** the day-7 verdict memo went out 2026-07-18
  (`ab_race_state.json: verdict_ts 1784361601`) on a book whose A-side profit is
  the P0-1 artifact. A +8.55% vs B −7.68% is fill model, not strategy — at their
  own price it is −0.11% vs −6.69%.
- **Change:** reset `ab_race_state.json`, restart the race clock after P0-1, and
  add the at-their-price metric to the `AB-RACE` daily snapshot so the next
  verdict cannot be won on fills.
- **Effort:** ~1h.

### P0-4 · Ship the persistence test as a first-class metric — **SHIPPED `ec84422`+`1ba9647`**
- **Why:** §1.3 — corr = −0.18 / −0.10, never measured by the bot.
- **Change:** `promotion_gate.py` already computes `second_half_roi` per wallet;
  add a book-level `split_half_corr()` beside it and surface it in `/pnl` and the
  daily digest. ~40 lines.
- **Decision rule — write this down before you have a result you are attached
  to:** if split-half correlation stays ≤ 0 on clean post-P0-1 data across ≥ 15
  wallets with n ≥ 10, wallet-copying is falsified. Stop investing in it rather
  than tuning it. See §5.
- **Effort:** ~2h.

---

## 4. P1 — actually search for edge

> **P1-1..P1-7 SHIPPED 2026-07-28** (931 tests; `6bc4837` cache+RAM fix,
> `b661126` feature batch, config flip as the tip commit so the 10× revert is
> one commit). What landed beyond the letter of the items (ideator field,
> manager-filtered): per-sweep **funnel telemetry** (I2: pool → qualified →
> gate → fail-open → holdout → admitted, one structured line), a **persistent
> cull-histogram.jsonl** (I4: the P1-2 acceptance share is trendable, not
> hand-counted), and a **cost sensitivity panel** in the `/pnl` trust block
> (I8: combined at-price ROI under ×0/×0.5/×1/×2 cost multipliers — the 08-22
> kill verdict now reads in real-money terms). Rejected at the gate: a
> copy-replay sample guard (redundant with the proven tier), a runtime
> dossier preflight (would fail-open or change gate semantics; the ≤2-null
> unit test covers it), a synthetic fail-open test path (would pollute
> gate-history calibration data). Re-watch items: (1) `new=` in the DISCOVERY
> line averages > 10/day for a week (P1-1 acceptance); (2) hit-rate-scooper +
> replay-proven-negative drop below 20% of the cull histogram (P1-2
> acceptance, readable from cull-histogram.jsonl); (3) first wide sweep's
> duration + VM RAM; (4) 5 Langfuse traces show no >2-null dossiers (P1-3
> acceptance); (5) P1-6 shrinks the copied universe the 08-22 verdict
> measures — a still-negative verdict then is *more* damning, not less; (6)
> vetting throughput vs influx — first wide sweep: new=72, gate_in=17 (the
> 20-call cap). Fail-safe (above-cap = admit-cap, paper-only) and mostly the
> cold-start flood, but if the admit-cap backlog is still growing when the
> 7-day new= watch reads (~08-04), revisit: raise the cap (cost exits the
> blessed $5–9/day band — surface with numbers) or triage gate-call order by
> P1-2 replay rank.

### P1-1 · Widen the funnel 10× — **SHIPPED** (config flip + `6bc4837` prereq)
- **Why:** §1.6 — `new=0..3`/day on a fixed 43-wallet pool is not a search.
- **Change:** `config.py:404` `WALLET_DISCOVERY_SKILL_POOL` 40 → 400;
  `config.py:402` `WALLET_DISCOVERY_INTERVAL_S` 86400 → 21600 (4×/day).
- **Watch:** e2-small RAM is 1.1G/2.0G used. If this strains the VM, that is the
  trigger to resize, not to stay small.
- **Cost:** gate calls ~5/day → ~50–100/day, i.e. $0.30/day → ~$5–9/day. Against
  the goal that is nothing (§1.7 — total spend to date is $8).
- **Acceptance:** `new=` in the DISCOVERY sweep line averages > 10/day for a week.
- **Effort:** config change, but needs a VM load check. ~2h.

### P1-2 · Rank the pool on copy-replay ROI, not wallet ROI — **SHIPPED `b661126`**
- **Why:** §1.6 — top-ranked watchlist wallets have `copy_n=0`, and the LLM gate
  spends its budget killing the scoopers the screen keeps producing.
- **Change:** make `copy_replay` ROI a **ranking key** in the skill screen, not
  only a downstream filter (`wallet_discovery_min_copy_replay_roi` exists at
  `config.py:428` — promote it to the sort). Wallets with `copy_n=0` should rank
  *below* wallets with proven copy-replay, not above.
- **Acceptance:** `hit-rate-scooper` + `replay-proven-negative` drop below 20% of
  the cull histogram (they currently dominate it).
- **Effort:** ~4h.

### P1-3 · Fix the dossier the gate sees — **SHIPPED `b661126`**
- **Why:** §1.7c — the model is refereeing contradictory, half-null inputs.
- **Change:** `discovery_runner._dossier_from_eval` — populate the null fields or
  drop them from the payload; reconcile `why_flagged` against `pnl_curve` before
  sending.
- **Acceptance:** no dossier ships with > 2 null fields; spot-check 5 Langfuse
  traces.
- **Effort:** ~3h.

### P1-4 · Make gate fail-open visible — **SHIPPED `b661126`**
- **Why:** §1.7a — ~13–15% of wallets admitted unvetted, zero error traces.
- **Change:** `discovery_runner._llm_gate` — emit a Langfuse trace with
  `level=ERROR` on parse/call failure; add the fail-open count to `/gate`; alert
  on Telegram above 20% in a sweep.
- **Effort:** ~2h.

### P1-5 · Verify the holdout branch fires — **SHIPPED `b661126`** (and the prod anomaly self-resolved 07-27: first holdout fired; drought was the p≈5% streak)
- **Why:** §1.7b — 0 holdouts across 28 skips.
- **Change:** unit test driving `_llm_gate` with a stubbed RNG asserting the
  holdout row is written (`discovery_runner.py` ~line 684); log the holdout roll
  at DEBUG so it is observable.
- **Effort:** ~2h.

### P1-5b · Gate calibration Phase 2 — *carried over from the deleted BACKLOG.md*
- **Status:** blocked on P1-5. Phase 1 (the holdout) shipped 2026-07-03 but has
  produced **zero holdout rows in 22 days**, so the counterfactual clock has
  never actually started.
- **The measurement:** join `data/gate-history.jsonl` to `copy_paper_ledger.jsonl`
  and report **admitted-ROI vs holdout-ROI**, sliced by confidence band and
  qualifying theory. The money question: do the high-confidence skips we
  holdout-admitted actually lose? If yes the gate is +EV; if the holdouts win,
  the gate is rejecting edge.
- **Why the holdout is non-negotiable:** a naive join only sees admitted wallets —
  a selection-biased self-congratulation loop, not a test. Do **not** ship the
  report without holdout data or it will only ever flatter the gate.
- **Discipline:** treat the first calibration report as evidence to tune the gate
  prompt/thresholds *by hand*, never to auto-change them. Keep the holdout
  fraction small and its exposure capped — by construction it admits wallets the
  gate thinks are bad.
- **Entry points:** `gate_history.py` (`summarize` grows a calibration mode, or a
  new `gate_calibration.py`), `telegram_bot._handle_gate`.
- **Effort:** ~1 day, after weeks of holdout outcomes exist.

### P1-6 · Act on the category evidence already in hand — **SHIPPED `b661126`**
- **Why:** §1.5 — sports is 333 of 415 copies and loses in both books, while the
  `approved_categories` filter (`copy_paper_live.py:506-520`) defaults to
  unrestricted.
- **Change:** flip the default from "absent → don't block" to "absent → require
  ≥ N settled copies in that category first"; add the same treatment for
  entry-price buckets.
- **Caveat:** this is about cutting the known loser. Do **not** tilt into crypto
  on n=9.
- **Effort:** ~3h.

### P1-7 · Model fees and gas — **SHIPPED `b661126`**
- **Why:** §1.2 — even −3.17% is gross. Neither harness charges Polymarket fees
  or Polygon gas.
- **Change:** subtract realistic per-trade cost in both books.
- **Effort:** ~2h.

---

## 5. P2 — hygiene

> **P2-3/P2-4/P2-6 SHIPPED 2026-07-28** (P2-5 stays owner-REJECTED). Beyond the
> letter of the items (ideator field, manager-filtered): telemetry **regression
> tests** pinning the envelope→Langfuse mapping + cost fallback + a
> **`telemetry-suspect` watchdog** (a successful generation with zero usage now
> WARNINGs instead of silently corrupting a week of cost data), an idempotent
> **`scripts/vm_hygiene.sh`** (archive+sha256-manifest+delete, prune, df
> receipts to `~/app/logs/hygiene.log`), and a src pin test for the deleted
> filenames. Two fell out of the run: **the deploy broke twice** —
> `deb.nodesource.com/setup_22.x` now returns HTTP 403 and the old
> `curl | bash` (no pipefail) swallowed it, so apt installed the distro nodejs
> (npm split out) and the build died; earlier greens were layer-cache luck.
> Node now installs from a **pinned official tarball** (v22.17.0) and the base
> is pinned to `python:3.12-slim-bookworm`. And a disk census found **wcache at
> 6.7GB after its first day back** — the `CACHE_MAX_FILES=15000` backstop was
> ~37GB on a 20G disk (a bound that fires only after the disk is full), now
> 4000 (~10GB, `ecbf421`). The one-off orphan scan (I2; standing sentinel
> rejected) archived+deleted 2 more dead files (`price_paths.json` 2.5M,
> `resolutions.json` 48K, stale since 2026-05-23);
> `discovery_state.json.consensus.json` looked orphaned but is LIVE (computed
> ref, `discovery_runner.py:280`). **Watch:** (1) claude-code CLI in the image
> is still unpinned (`npm -g` latest) — every rebuild is a lottery on gate
> behavior; the watchdog detects shape drift, recalibrate if it ever fires;
> (2) rescache grows slowly forever (578MB, immutable) — years to matter.

### P2-1 · Kill the log noise — biggest win, smallest change — **SHIPPED `ec84422`**
- 68,889 of 73,666 lines/day are urllib3 DEBUG (§1.8).
- **Change:** add `"urllib3"`, `"urllib3.connectionpool"`, `"web3"`, `"asyncio"`
  to the noisy tuple at `src/logger.py:208`.
- **Effect:** 16 MB/day → ~1 MB/day; 327 MB → ~20 MB at the same 20-day
  retention. **Leave `BOT_LOG_RETENTION_DAYS=20` alone** — retention is not the
  problem.
- **Effort:** 5 min.

### P2-2 · Cap the Docker json log — **SHIPPED `ec84422`**
`deploy.sh:178` has no `--log-opt`. Add
`--log-opt max-size=50m --log-opt max-file=3`. **5 min.**

### P2-3 · Delete dead strategy data — **SHIPPED** (archive + sha256 manifest in `~/app/data/archive/`, then delete; orphans included)
`tennis_scan_metrics.jsonl` (12 MB), `tennis_trades.jsonl`,
`tennis_bet_state.json`, `tennis_paper_book.json`, `weather_trades.jsonl` — ~14 MB
from strategies purged in `5f7a127` (2026-07-05). Archive, then delete. **15 min.**

### P2-4 · Prune Docker images — **SHIPPED** (reclaimed 1.052 GB, df receipt in `hygiene.log`)
1.05 GB reclaimable (`poly-poly-bot:latest` 933 MB, 5 weeks old).
`docker image prune -a`. **5 min.**

### P2-5 · Close the world-open ports — **REJECTED by owner (2026-07-25, do not re-pitch)**
`default-allow-ssh` permits `tcp:22` from `0.0.0.0/0`; `allow-iap-ssh`
(35.235.240.0/20) already covers real access. Also world-open:
`default-allow-rdp` (3389), `allow-vnc-5901` and `llow-vnc` (5901).
- **Blocked on owner:** these are project-wide defaults — confirm no other
  `roman-vm` instance depends on them before deleting.
- **Effort:** 30 min incl. the check.

### P2-6 · Fix Langfuse usage accounting — **SHIPPED `7adc2e3`**
Input tokens report as 2–6/call since ~2026-07-11 (vs 3,000–5,000 before);
2026-07-11 → 07-14 report `totalCost: 0`. Post-07-11 costs are understated.
Check the usage payload in `src/copy_trading/langfuse_telemetry.py`. **1h.**

---

## 6. Sequence

1. **First 10 minutes:** P2-1, P2-2.
2. **Then:** P0-1 → P0-2 → P0-3. This is the set that stops the bleeding.
3. **Then:** P0-4.
4. **Then:** P1-1 + P1-2 together — they are the same funnel fix from two ends.
5. **Then:** P1-3, P1-4, P1-5 as one batch (all gate integrity), unblocking P1-5b.
6. **Opportunistically:** P1-6, P1-7, P2-3/4/5/6.

**Framing:** P0 does not find edge. It tells you whether the edge you thought you
had is real — and the current read is that it is not. P1 is the bet that a 10×
wider search finds something a 40-wallet pool could not.

---

## 7. Kill criterion

Agree this now, while it is cheap:

> After P0-1 ships and ≥ 4 weeks of clean fills have accrued, if the combined
> at-their-price ROI is still negative **and** split-half correlation (P0-4) is
> still ≤ 0 across ≥ 15 wallets with n ≥ 10, then wallet-copying on Polymarket is
> falsified for this approach. Stop tuning it. Either pivot the search to rules
> and market structure rather than wallet identity, or stop.

The failure mode this guards against is the one the codebase has already shown:
a wallet looks good at n=7, gets promoted, decays, gets blacklisted, and the
cycle repeats with the next small sample.

---

## 8. Operational reference

- **VM:** `poly-poly-bot`, zone `asia-northeast1-a`, project `roman-vm`,
  `e2-small`, IAP-only access
  (`gcloud compute ssh poly-poly-bot --zone asia-northeast1-a --project roman-vm --tunnel-through-iap`).
- **Volumes:** `~/app/{data,logs,cache,results}` → `/app/...` in the container.
- **It has gone network-dead before** (metadata server unreachable → SSH fails
  with "failed to connect to port 22"). `gcloud compute instances reset` recovers
  it; the container auto-restarts (`--restart unless-stopped`).
- **Ship workflow** (see `CLAUDE.md`): full test suite must pass (count comes
  from the run, not from this file;
  `cd poly_poly_bot && .venv/bin/python -m pytest tests/ -q`) → commit → push to
  `main` → GitHub Actions builds amd64 and pushes to Artifact Registry, VM pulls.
  Watch with `gh run watch <id>`. **Never** run `bash deploy.sh` on the Mac — it
  is arm64 and has no Docker.
- **Report the test count the run printed**, never "all tests pass" without a
  number — and never copy that count into this file (see `CLAUDE.md`).

### Key config anchors

> **STALE — verified 2026-08-02 (s-r7m3qk):** most of the `file:line`
> references below no longer resolve (they predate several refactors), and at
> least one `Current` value is also out of date. Treat only the *setting
> names* as reliable: `grep` the name. Do not read a value out of this table
> without checking it in the code.

| Setting | Location | Current |
|---|---|---|
| `MIN_FILL_FRAC` | `copy_paper.py` | `0.97` (was `0.5`; P0-1 shipped) |
| fill gate (one-sided) | `copy_paper.py:586` | `> fill_gate_bps` ← **P0-1** |
| `ideal_pnl` computation | `copy_paper.py:153` | — |
| `COPY_PAPER_FILL_GATE_BPS` | `config.py:217` | `150` |
| `COPY_PAPER_MIN_USD` | `config.py:191` | `300` |
| `WALLET_DISCOVERY_INTERVAL_S` | `config.py:423` | `21600` (shipped P1-1) |
| `WALLET_DISCOVERY_SKILL_POOL` | `config.py:430` | `400` (shipped P1-1) |
| `WALLET_DISCOVERY_MIN_COPY_REPLAY_ROI` | `config.py:428` | `0.02` ← **P1-2** |
| `COPY_GOLIVE_MIN_SETTLED` | `config.py:364` | `30` |
| `GATE_HOLDOUT_FRAC` | `config.py:487` | `0.1` ← **P1-5** |
| holdout wiring | `main.py:826` | correct |
| root logger set to DEBUG | `main.py:878-887` | ← **P2-1** |
| noisy-logger suppression list | `logger.py:208` | ← **P2-1** |
| `approved_categories` filter | `copy_paper_live.py:506-520` | ← **P1-6** |
| docker run (no log cap) | `deploy.sh:178` | ← **P2-2** |

Production env overrides (`docker exec poly-poly-bot env`): `PREVIEW_MODE=true`,
`WALLET_DISCOVERY_ENABLED=true`, `COPY_PAPER_ENABLED=true`,
`WALLET_DISCOVERY_LLM_REVIEW_ENABLED=true`, `BOT_LOG_RETENTION_DAYS=20`,
`LANGFUSE_*`. The deploy workflow also pins `AB_RACE_VERDICT_DAYS=27` via
`ensure_env` (`.github/workflows/deploy.yml`), which overrides the `config.py`
default of `7.0` — that is what puts the verdict on 08-22, so do not read the
clock off `config.py`. Anything not listed here runs on `config.py` defaults.

---

## 9. Inspection s-r7m3qk — 2026-08-02 (second pass over the health batch)

A full re-read of the four `feat/fix(health)` commits plus the money path,
against the **live VM**, not just the code. What shipped is enumerated in §9.1;
what is still open is enumerated in §9.2 (verdict-integrity, before 08-22) and
§9.3 (lower). Counts are deliberately not restated here — the lists are the
tally, and a hand-maintained summary number is exactly what went stale twice
during this session.

**Framing for the next session:** protect the 08-22 verdict from being voided
or made unreadable, and arrive on 08-22 with the pivot's evidence in hand.

### 9.1 SHIPPED

- **The §7 verdict memo could never have been delivered.** `_bucket` renders a
  literal `<$25`; the memo is `parse_mode="HTML"`. Verified against the live
  Telegram API: `400 can't parse entities: Unsupported start tag "$25"`, and
  `<pre>` does not protect it. `main.py` only sets `verdict_sent` when
  `_send_chunked` returns True, so on 2026-08-22 the memo would have failed,
  re-fired every morning forever, and `/verdict` would never have armed.
  Labels are now `under $25`; a test asserts no Telegram-bound line contains
  `<`; and `send_message` retries once as plain text on a parse error so no
  future market-derived string can cost a message.
- **`/setkey` echoed the private key** for every input shape except the one the
  redaction regex matched (bare key with no `0x` — which `config_validators`
  accepts; `/setkey@botname`; a typo'd `CONFIRM`). Arguments are now dropped
  wholesale. **No key or token was found in any current log or data file.**
- **Disk ~1 day from full.** disk-watch TRIPPED 11:41 UTC: `free 4.1G shrinking
  1521MB/day`. `prune_cache` bounded wcache by FILE COUNT; entry size is the
  wallet's history length, so 4000 files had grown to **8.2G of a 20G disk**.
  Now budgeted in bytes and *derived from free space* via a reserve, rather
  than another hand-picked constant (that has gone stale twice: 37GB in P2,
  8.2GB now). Also pruned at sweep END. rescache count cap 120k → 400k: at
  ~90k writes/sweep an entry survived ~1.3 sweeps, so it was mostly paying to
  write files it deleted before reuse.
- **A failed `/activity` fetch was cached as truth.** `_get` returns `None`
  after four attempts; the caller could not tell that from "no more trades" and
  wrote the empty/truncated list to cache for the full 24h TTL. 18 wallets held
  a cached `[]`. A genuine `[]` is still cached; an incomplete fetch never is,
  and the sweep now WARNs with a count (the discovery path previously logged
  **nothing at all** on a failed fetch).
- **`bot-*.log` could not report a problem.** `_OperationalFilter` sent WARNING+
  *exclusively* to `signals-*.log`. Grepping the operational log returned a
  clean sheet while 261 warnings/day and a tripped disk alarm sat elsewhere.
  WARNING+ now goes to both. *(If you grep prod logs, know that `bot-*.log` was
  blind to warnings before 2026-08-02.)*
- **The go-live gate could bless real money on artifact-era evidence.**
  `compute_stats` has no era parameter, so every *realized* bar not labelled
  "clean era" is all-time — the number P0-1 voided — while the honest
  checks need only a handful of clean settles and a 3-wallet correlation. New blocking bar:
  `COPY_GOLIVE_MIN_CLEAN_SETTLED` (default 30).
- **The P1-6 evidence gate read dust fills** — the only aggregation in the repo
  that didn't. A dust loss is capped at −1/row but a dust **win is unbounded**:
  one row that swept a stale 0.001 ask returns +$49,950 on $50 and can drag a
  whole losing category positive, so the block never fires. One-directional:
  it can only push toward admitting.
- **§7 readability witness.** Nothing watched whether ≥15 wallets at n≥10 would
  actually exist on 08-22. A pre-registered falsification that quietly never
  resolves is worse than either outcome. Now on the daily snapshot.

### 9.2 QUEUED — verdict-integrity, do before 08-22

- **P1-7 charges a ROUND-TRIP spread against a book that redeems at par.**
  `copy_cost.py` assumes "sell or redeem at the bid", but `copy_paper.realize()`
  sets `payout = shares` — a winning share settles at $1.00, no spread. The
  exit half is only actually paid on `realize_exit` rows, yet `ideal_cost_usd`
  charges the full round trip on **every** row at open, so every `@net` figure
  is biased negative. **Do not quote a number for this from here** — it moves
  daily and this file has already carried a stale one; read it live from
  `/pnl`'s cost-sensitivity panel or `scripts/rebaseline_ledger.py --era`.
  **Interim: read the ×0.5 sensitivity column as the primary, not ×1.**
  This does NOT decide whether §7 fires — §7 and §9.4's arms are defined on
  **gross** at-price ROI and correlation, neither of which is a function of
  modeled cost (§9.8). It decides the separate ECONOMIC read: whether there is
  money in this after costs, where `@net` is the only negative figure.
- **`at-their-price` ROI is not fill-model-independent.** `ideal_pnl` uses
  `their_price` but every consumer divides by `spent = shares·entry_price`, so
  `reported = true × (their_price/entry_price)`. Clean era is bounded by the
  fill gate (±1.5%) so the §7 sign is safe; **all-time** figures are not — a
  0.5× legacy fill doubles the reported number, and those rows were kept
  deliberately "to show their honest economics". Fix: accumulate
  `ideal_cost_basis = Σ shares·their_price` and divide by that.
- **`/verdict recalibrate` does not do what it says.** It writes an *absolute*
  `AB_RACE_VERDICT_DAYS=30` against a fixed `era_start`, so 27→30 is **+3 days,
  not +30** (the UI says "extend era 30d"). A second recalibrate is a no-op
  that also clears `verdict_sent`, so the memo re-posts the next morning — the
  "extend" knob becomes a daily-memo loop. It also immediately re-disarms
  `/verdict` itself, with no way back.
- **Autopsy fingerprints never expire.** `seen_fps[fp] = now` is never pruned,
  so a fixed-then-recurring anomaly is silent **forever** — precisely the
  −$575 double-write class the module exists to catch. Also `_save_state` sits
  inside the broad `except Exception: pass`, so one raise re-alerts everything
  daily and records no size baseline.
- **Admit-cap wallets are admitted permanently unvetted.** Past
  `llm_review_top_n` (20) a wallet is recorded `admit-cap, admitted=True` with
  **no queue entry**; next sweep it is in `prev_on` and never gated again. Cap
  is 500, so up to 480 unvetted admits in one sweep. `_maybe_alert_gate_failopen`
  excludes admit-cap from both terms, so a sweep that gates 20 and admits 52
  ungated raises **no alert**. Note: the P1 watch item "if the admit-cap
  backlog is still growing" **cannot be satisfied — there is no backlog.**
- **`paper_proven_wallets` is all-time realized**, so artifact-era gift fills
  grant the highest-privilege discovery path (force-include, bypasses tail-ratio
  / drawdown / scooper / category gates, ranks first under cap pressure). Five
  pre-fix gifted settles at any positive ROI is enough.

### 9.3 QUEUED — lower

- `disk_watch.check` skips `_save_state` when the Telegram send raises, so a
  persistently broken Telegram degrades the trajectory alarm to floor-only.
- `simulate_copy_fill` **breaks** the book walk on a zero-size level instead of
  **continuing**, truncating the fill (the sub-floor case correctly continues).
- `won` means "the outcome settled true" in `realize()` but "we made money" in
  `realize_exit()`; consumers (hit_rate, Wilson LB, the demote hold) cannot tell
  them apart, so many small profitable exits plus a few large resolution losses
  can dodge the blacklist.
- `outcome_index` defaults to 0 with no cross-check against `token_id`; a feed
  row missing `outcomeIndex` on a NO-side buy books an inverted result.
- `telegram_bot._stamped` / `_sensitivity` skip the dust quarantine.
  (`strategy_compare._book_stats` did too; fixed in `bab44f7`.)
- `split_half_corr` rounds before the floor comparison: a true `-5e-5` becomes
  `-0.0`, and `-0.0 >= 0.0` is True. **Deliberately not fixed** — every
  sign-preserving fix distorts the displayed magnitude by more than the 1e-5 it
  corrects. Revisit only if the floor moves off zero.
- P1-6's "can never deadlock" guarantee is silently coupled to
  `COPY_PAPER_CATEGORY_GATE`: with the gate off, `stamped` is permanently
  False, removing the sole re-admission channel. The price-bucket map is also
  built book-wide across all categories, so once each of the 5 buckets clears
  n=15 an unstamped wallet is blocked in every price band.

### 9.4 The §7 decision, written before the number exists

Agreed now so the result cannot pick the rule. `/verdict` offers three arms;
until today only two had content.

- **RETIRE** — B's clean-era split-half corr ≤ 0 with ≥15 wallets at n≥10,
  **and** combined at-their-price ROI < 0. This is §7 met. Stop tuning
  wallet-copying. The P0–P2 harness (honest measurement, cost model, paper
  books, era scoping) is generic and is what a rules-based strategy would
  reuse — retiring the thesis is not discarding the work.
- **HOLD** — corr > 0 **and** at-price ROI ≥ 0. The thesis survives; nothing
  changes; re-read at the next boundary.
- **RECALIBRATE** — the mixed case (a marginal miss: corr within ±0.05 of zero,
  or ROI negative but within one standard error). Concretely it means: extend
  the era by a *relative* 30 days (see the bug in §9.2 — the current knob adds
  3), and apply the cut that was deliberately **not** made mid-experiment:
  drop sports pool-wide — but **re-derive the premise first** (`/slice B`):
  as of 2026-08-02 sports does NOT lose in both books at their own price. It
  is **~62% of settled A+B copies, ~55% in the clean era** (live ledgers, 2026-08-02; the ledgers
  append continuously, so treat the share as approximate and re-derive rather
  than quoting a row count. §1.5's 333-of-415 is the 2026-07-25 snapshot and
  is NOT current).
  The reason to hold the cut until the boundary is not the share but the
  **wallet count**: dropping sports takes book B's clean-era wallets at n≥10
  from **27 to 11**, under the ≥15 bar — it would convert a readable verdict
  into an undefined one, which is strictly worse than either outcome. Both
  figures independently re-derived twice on 2026-08-02; the wallet counts
  agreed exactly, the row counts drifted by one between runs minutes apart,
  which is why the share is stated as a percentage and not a tally.
  So it is the content of the recalibrate arm, not a change to make now.

**Frozen until 08-22** (anything that changes what the verdict measures): the
sports cut, any new live book, any promotion or copy-gate threshold change, any
ranking change that alters which trades enter the sample. Plumbing fixes
(§9.1/§9.2) are not in this category — a silently truncating fetch is a defect,
not a design parameter, and leaving it running would hand the negative result
an obvious alternative explanation.

### 9.5 Fade-the-hot-wallet — first read (2026-08-02, read-only)

The most-trusted finding (§1.3) is that a wallet looking good is slightly
*negative* information. Nobody had tried inverting it on purpose. Computed on
the live ledgers, at-their-price (fill-model independent), dust excluded,
wallets with n≥10 settled, split chronologically in half:

```
Book A (11 wallets)   follow-HOT (1st half +): 6 wal, $1,996 -> 2nd half  -7.03%
                      follow-COLD (1st half -): 5 wal, $1,026 -> 2nd half  +0.79%
                      fade edge (cold - hot) = +7.82 pp
Book B (33 wallets)   follow-HOT: 24 wal, $6,402 -> 2nd half  -2.05%
                      follow-COLD: 9 wal, $3,211 -> 2nd half  +6.70%
                      fade edge (cold - hot) = +8.75 pp
```

Directionally consistent across both books and both signs. **But do not read
this as an edge yet**, for two reasons that must be settled first:

1. **This is exactly what regression to the mean produces under a ZERO-signal
   null.** Selecting on "first half was positive" selects partly on noise, and
   noise regresses. A fade edge of this shape is the *expected* artifact, not
   evidence against it. It needs a null model (shuffle the halves, or bootstrap
   the wallet assignment) before it means anything.
2. **The magnitude does not clear modeled costs.** Cold-wallet second halves are
   +0.79% (A) and +6.70% (B) at-their-price gross, against a modeled
   round-trip cost of roughly 10% (materially less if the entry-only
   correction in §9.2 is right — read it live rather than from here). Only
   book B is even in the conversation, on $3.2k of capital and 9 wallets.

It is also **not independent evidence** — it is a re-read of the same rows §1.3
already reported. Treat it as a pre-registered hypothesis for after 08-22, with
its own kill bar written before it is tested, not as a reason to soften the §7
verdict.

**One useful side-fact:** book B has **33 wallets at n≥10 all-time**, comfortably
over the §7 bar of 15 — but §7 counts *clean-era* wallets, and the clean era is
8 days old. That gap is precisely what the new readability witness (§9.1) now
reports every morning.

### 9.6 Verifier round 1 — one defect in this session's own fix

The byte governor shipped in `7f5feb6` **failed open at the exact disk state it
exists for**. `allowance = max(0, total + free - reserve)` clamps to `0` when
free space reaches the reserve; `0` is falsy, and the eviction test read that as
"no budget configured" — so the governor switched itself off under pressure, and
`min(ceiling, 0)` discarded the explicit 5G/1.5G ceilings too. It was ~0.64G of
free space from firing in prod. Compounding it, the same commit raised the
rescache count cap 120k → 400k, which is +1.1G of growth headroom in precisely
the OFF state. Fixed in `d39440d`: the test is `max_bytes is not None`, and the
allowance carries a floor (default 0.5G, injectable) so real pressure shrinks
the cache to a working set instead of either ignoring the budget or emptying it
and forcing a full refetch into an already-429-ing API.

Also from the same pass: the wcache prune now logs (it was the one silent lever,
on the biggest thing on the disk); the §7 readability witness reports **both**
books, since A sits at 4w against the 15w bar and a B-only witness would have
said "readable" while A printed inconclusive on the day; and two comments that
the same commit had made stale were corrected.

**Verifier's independent measurements on live prod, worth keeping:** §7 is
already readable on B (**27 wallets at n≥10, bar 15**, 19 days out). Both
promoted wallets read `READY=False` with *and* without the new clean-era bar —
they already fail on paper-ROI, at-price-ROI and the promotion floor — so the
30-copy default changes nothing today and only closes the artifact-era hole.
Zero secrets in prod logs (no bot-token pattern, no 64-hex key, no setkey echo).

**Still open from that pass, queued (not fixed):** the go-live
`min_split_half_corr` bar's wallet floor is only 3 (`split_half_corr` defaults
`min_wallets=3`, and `/golive` uses the default) — it is not unguarded, but 3-4
wallets clears a `>= 0` bar about half the time under the null, and it has been
seen *passing* at `+0.45 (4w)`. The settled-count half of that hole was closed,
this half was not. Direction is safe (the gate is strictly stricter and
PREVIEW_MODE is on). Same item as §9.7.1 — keep the two in step.

### 9.7 Go-live preconditions — check these before flipping PREVIEW_MODE=false

The manual flip is the one true one-way door. `/golive` renders the mechanical
gate; this list is what the gate does **not** yet check and a human must.

**These four are now rendered in the `/golive` output itself**, so the flip
decision carries them at the point it is made rather than depending on anyone
finding this section. If you edit them here, edit `_handle_golive` too.

1. **`min_split_half_corr`'s wallet floor is only 3.** `split_half_corr`
   enforces `min_wallets=3`, so the check is not unguarded — but a Pearson
   correlation over 3-4 points clears a `>= 0` bar about half the time under
   the null, so it contributes almost no discrimination at the present book
   size. Read the `(Nw)` count next to it, not just the sign. (Round-1 verifier finding, s-r7m3qk;
   deliberately not code-fixed because the gate is strictly stricter with it
   than without and PREVIEW_MODE is on.)
2. **The clob credential path.** `py_clob_client` logs
   `request error status=400 url=/auth/api-key` a few times a day. Benign in
   PREVIEW (known create-or-derive fallback, self-heals), but it is the
   credential derivation a real order depends on. Confirm a successful API-key
   derivation in the logs before the flip, not after.
3. **The cost model bias (§9.2).** Modeled cost currently charges a round-trip
   spread against a book that redeems at par. Any go-live sizing computed off
   `@net` is using a number biased ~4-6pp too negative.
4. **All-time vs clean-era.** Every *realized* bar not labelled "clean era" is
   all-time (the unlabelled `active within Nd` bar is a recency check, not a
   PnL one); only `COPY_GOLIVE_MIN_CLEAN_SETTLED` and the two honest checks are
   era-scoped. If the wallet's all-time record is much better than its clean-era
   record, the difference is the fill artifact, not skill.

### 9.8 Re-derived 2026-08-02 — §7 does not fire today, on EITHER leg

Round-7/8 verification forced a re-derivation instead of carrying figures
forward. Two prose claims in §9 were false, and correcting them changes what
the 08-22 verdict is expected to say. **Per the standing rule at the end of
this section, no ROI figures are reproduced here** — run
`docker exec poly-poly-bot python scripts/rebaseline_ledger.py --era`, or
`/pnl` and `/slice B`. That command prints both books, the combined figures,
the persistence pair and the §7 bar in one screen.

1. **"Sports loses in both books" is false.** In book B's clean era sports is
   solidly positive at their own price, and it is not even the worst slice
   (`other` is worse at `@net`). It loses only after modeled costs. §1.5's
   finding was real on 2026-07-25 data and does not hold now, yet it was the
   sole stated premise for the recalibrate arm's sports cut — re-derive before
   applying that cut.

2. **Both §7 legs are currently unmet.** §7 fires only on clean-era at-price
   ROI **< 0** AND split-half corr **≤ 0** across ≥15 wallets at n≥10. Today
   combined at-price ROI is positive, and book B's clean-era persistence is
   **positive across 27 wallets** — above the 15-wallet bar, so it is a
   measured result, not an unmeasurable one. §9.4's RETIRE arm assumed both
   would be met. On today's data the tree resolves to **HOLD** (corr > 0 and
   at-price ROI ≥ 0), and it resolves cleanly — this is not an ambiguous state.

3. **The cost model does NOT decide whether §7 fires.** An earlier draft of
   this section said it did; that was wrong. §7 and §9.4's arms are defined on
   **gross** at-price ROI and split-half correlation, and neither is a function
   of modeled cost. What the §9.2 cost bug decides is the **economic** read —
   whether there is money in this after costs — because `@net` is the only
   figure that is currently negative. Both questions matter; they are not the
   same question, and only the second one turns on §9.2.

**This is not "the thesis is winning", and the caveats are load-bearing:**

- Persistence at 27 wallets carries a standard error of roughly 0.2, so a
  reading near zero is weak evidence either way. The honest statement is "no
  *demonstrated* persistence", not "no persistence" — and equally not
  "persistence established". §1.3's negative correlation was a 2026-07-25
  measurement; it has since flipped sign, which is itself what §1.3 predicted
  wallet-level ROI would do.
- **The combined A+B statistic double-counts.** A and B copy the same
  watchlist, and about three quarters of book A's clean-era settled rows share
  a `copy_id` with a book B row at an identical `their_price` — at
  at-their-price those are literally the same observation counted twice, so
  the combined `n` is inflated by roughly a fifth and its standard error is
  understated on top of §1.2's already-weak t. By dollars the combined figure
  is mostly book B wearing a combined label. De-duping to one row per
  `copy_id` moves clean-era at-price **further above zero** on either
  tie-break, so the *sign* of the ROI leg is robust — but §7 pre-registered
  "combined" (§1.2 defines it as A+B) and the freeze forbids swapping the
  statistic now. Recorded here so 08-22 is not ambushed by it.
- A positive gross at-price ROI on a book whose wallet selection has no
  demonstrated persistence is consistent with beta — a rising market lifting
  everything copied — rather than with skill at picking wallets.

**Standing rule for this file, learned over eight verification rounds:** do not
write a derived ROI into this document. Point at `/pnl`, `/slice`, or
`scripts/rebaseline_ledger.py --era`. Every hand-copied ROI in §9 went stale or
was wrong at birth — including two written in the commit that first added this
very rule. The machine-derived wallet counts held.
