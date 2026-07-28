#!/usr/bin/env bash
# vm_hygiene.sh — one-shot VM hygiene sweep (ROADMAP P2-3 + P2-4, 2026-07-28).
#
# Runs ON the VM (poly-poly-bot, as the tianyuezhou user). Idempotent: every
# step no-ops when its target is already gone, so it is safe to rerun.
#
#   P2-3  archive + delete dead strategy data (tennis/weather, purged 2026-07-05
#         in 5f7a127). Archive lands in ~/app/data/archive/ with a sha256
#         manifest BEFORE anything is deleted; delete aborts if the archive
#         fails to verify.
#   P2-4  docker image prune, with a docker-system-df receipt (I3) appended to
#         ~/app/logs/hygiene.log so the reclaim is auditable later.
set -euo pipefail

DATA="$HOME/app/data"
LOGS="$HOME/app/logs"
ARCHIVE_DIR="$DATA/archive"
HYGIENE_LOG="$LOGS/hygiene.log"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

DEAD_FILES=(
  tennis_scan_metrics.jsonl
  tennis_trades.jsonl
  tennis_bet_state.json
  tennis_paper_book.json
  weather_trades.jsonl
)

mkdir -p "$ARCHIVE_DIR" "$LOGS"

echo "== vm_hygiene $TS =="

# --- P2-3: archive, verify, delete -----------------------------------------
present=()
for f in "${DEAD_FILES[@]}"; do
  [[ -f "$DATA/$f" ]] && present+=("$f")
done

if (( ${#present[@]} == 0 )); then
  echo "P2-3: no dead strategy files present — nothing to do"
else
  bundle="$ARCHIVE_DIR/dead-strategies-$TS.tar.gz"
  manifest="$ARCHIVE_DIR/dead-strategies-$TS.sha256"
  echo "P2-3: archiving ${#present[@]} files -> $bundle"
  (cd "$DATA" && sha256sum "${present[@]}") | tee "$manifest"
  (cd "$DATA" && tar czf "$bundle" "${present[@]}")
  # Verify before deleting anything: extract to scratch and check member
  # CONTENT against the manifest (a bare `tar tzf` only proves the gzip
  # stream reads, not that the bytes are the ones we checksummed).
  scratch="$(mktemp -d)"
  tar xzf "$bundle" -C "$scratch"
  (cd "$scratch" && sha256sum -c "$manifest")
  rm -rf "$scratch"
  (cd "$DATA" && rm -v "${present[@]}")
  echo "P2-3: deleted ${#present[@]} files (manifest: $manifest)"
fi

# --- P2-4: docker image prune with a receipt (I3) ---------------------------
before="$(docker system df --format '{{.Type}} {{.TotalCount}} {{.Size}} {{.Reclaimable}}' | tr '\n' '; ')"
prune_out="$(docker image prune -a -f)"
after="$(docker system df --format '{{.Type}} {{.TotalCount}} {{.Size}} {{.Reclaimable}}' | tr '\n' '; ')"
{
  echo "$TS hygiene-sweep"
  echo "  before: $before"
  echo "$prune_out" | sed 's/^/  prune: /'
  echo "  after:  $after"
} | tee -a "$HYGIENE_LOG"

echo "== disk =="
df -h "$HOME" | tail -1
echo "== done =="
