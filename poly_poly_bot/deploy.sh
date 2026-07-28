#!/usr/bin/env bash
set -euo pipefail

# ─── Configuration ───────────────────────────────────────────────
# Load project ID from .env (or use default)
if [ -f .env ]; then
    GCP_PROJECT_ID=$(grep -E '^GCP_PROJECT_ID=' .env | cut -d= -f2 | tr -d '"' || true)
fi
GCP_PROJECT_ID="${GCP_PROJECT_ID:-roman-vm}"

INSTANCE="poly-poly-bot"
ZONE="asia-northeast1-a"   # Tokyo, Japan
MACHINE="e2-medium"         # 2 vCPU (1.0 baseline, burst to 2), 4GB RAM.
                            # Upsized from e2-small on 2026-06-15: the e2-small
                            # (0.5 vCPU baseline, 2GB) was CPU-throttled enough
                            # that even `docker load` of the shipped image
                            # starved sshd, and 2GB likely caused the
                            # network-dead episodes.

# ─── Artifact Registry ───────────────────────────────────────────
# The build host (CI runner, native amd64) builds the image and PUSHES it to
# Artifact Registry; the VM PULLS it over Google's internal network. This
# replaces the old "docker save | gzip → scp over IAP → docker load" path,
# which crawled at ~0.75 MB/s pushing a ~500MB tarball through the IAP SSH
# tunnel (~10 min). AR pulls in the same region take seconds.
AR_LOCATION="asia-northeast1"           # same region as the VM → fast pulls
AR_HOST="${AR_LOCATION}-docker.pkg.dev"
AR_REPO="poly-poly-bot"
IMAGE="${AR_HOST}/${GCP_PROJECT_ID}/${AR_REPO}/poly-poly-bot"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo manual)"
IMAGE_SHA="${IMAGE}:${GIT_SHA}"   # immutable per-commit tag (kept for rollback)
IMAGE_LATEST="${IMAGE}:latest"    # moving tag the VM runs (so old image becomes
                                  # dangling on the next pull → prune reclaims it)

# SSH transport. In CI (GitHub Actions) the runner has no route to the VM's
# SSH port, so set DEPLOY_USE_IAP=1 to tunnel ssh/scp through IAP — no public
# IP or open-to-the-world firewall rule required. Local deploys leave it unset
# and connect directly, exactly as before.
SSH_FLAGS=()
if [ "${DEPLOY_USE_IAP:-0}" = "1" ]; then
    SSH_FLAGS=(--tunnel-through-iap)
    echo "  SSH: tunneling through IAP"
fi

# The app lives in ~/app of the SSH login user. Locally you connect as your own
# gcloud identity; in CI the service account is a *different* Linux user with a
# *different* home, so it would deploy into the wrong place and miss the
# existing container + data volume. Set DEPLOY_SSH_USER to the username whose
# ~/app holds the current deployment so CI lands in the same home. Unset =
# connect as the caller's default user (local behaviour, unchanged).
TARGET="$INSTANCE"
if [ -n "${DEPLOY_SSH_USER:-}" ]; then
    TARGET="${DEPLOY_SSH_USER}@${INSTANCE}"
    echo "  SSH user: $DEPLOY_SSH_USER"
fi

echo "=== Poly Poly Bot Deployment (build → push to AR → VM pulls) ==="
echo "  Project: $GCP_PROJECT_ID"
echo "  Instance: $INSTANCE ($MACHINE)"
echo "  Image: $IMAGE_SHA"
echo ""

# ─── Step 0: Docker required on THIS host ────────────────────────
# We build + push the image here. macOS arm64 has no docker by default — the
# canonical deploy path is `git push origin main` (CI builds + pushes). To
# deploy by hand, the build host needs docker.
if ! command -v docker >/dev/null 2>&1; then
    echo "❌ docker not found on this host."
    echo "   This deploy builds + pushes the image, so the build host needs docker."
    echo "   Either push to main (CI builds it for you) or install a runtime"
    echo "   (e.g. macOS: 'brew install colima docker && colima start')."
    exit 1
fi

# ─── Step 1: Ensure VM exists ────────────────────────────────────
if ! gcloud compute instances describe "$INSTANCE" \
    --project="$GCP_PROJECT_ID" --zone="$ZONE" &>/dev/null; then
    echo "[1/5] Creating VM..."
    gcloud compute instances create "$INSTANCE" \
        --project="$GCP_PROJECT_ID" \
        --zone="$ZONE" \
        --machine-type="$MACHINE" \
        --image-family=debian-12 \
        --image-project=debian-cloud \
        --boot-disk-size=20GB \
        --metadata=startup-script='#!/bin/bash
            apt-get update
            apt-get install -y docker.io docker-compose-v2
            systemctl enable docker
            systemctl start docker
            usermod -aG docker $(whoami)
        '
    echo "  Waiting 30s for startup..."
    sleep 30
else
    echo "[1/5] VM exists, reusing."
fi

# Ensure the AR repo exists (idempotent). The CI service account has
# artifactregistry.writer, which includes describe, so this is a no-op for it;
# an admin identity will create the repo if it's somehow missing.
gcloud artifacts repositories describe "$AR_REPO" \
    --location="$AR_LOCATION" --project="$GCP_PROJECT_ID" >/dev/null 2>&1 || \
gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker --location="$AR_LOCATION" \
    --project="$GCP_PROJECT_ID" --description="poly-poly-bot deploy images"

# ─── Step 2: Build HERE + push to Artifact Registry ──────────────
# The Dockerfile's RUN steps run the lint (ruff F821) + import smoke gate, so a
# broken image fails the build before it's ever pushed. --platform linux/amd64
# is a no-op on the amd64 runner and forces a VM-compatible image on an arm64
# Mac. .dockerignore keeps .env + git/cache/data out of the image.
echo "[2/5] Building image on $(uname -m) host + pushing to AR..."
DOCKER_BUILDKIT=1 docker build --platform linux/amd64 -t "$IMAGE_SHA" -t "$IMAGE_LATEST" .

# Configure docker to auth to AR with the active gcloud identity (CI SA on the
# runner; your user creds locally), then push. Both tags share layers, so the
# second push is metadata-only.
gcloud auth configure-docker "$AR_HOST" --quiet
docker push "$IMAGE_SHA"
docker push "$IMAGE_LATEST"

# ─── Step 3: Upload .env + AR pull token ─────────────────────────
echo "[3/5] Uploading .env + AR pull token to VM..."
gcloud compute ssh "$TARGET" \
    --project="$GCP_PROJECT_ID" --zone="$ZONE" "${SSH_FLAGS[@]}" \
    --command='mkdir -p ~/app'
gcloud compute scp "${SSH_FLAGS[@]}" .env "$TARGET:~/app/.env" \
    --project="$GCP_PROJECT_ID" --zone="$ZONE"

# The VM's service account lacks the cloud-platform/AR OAuth scope, so it can't
# mint its own pull token. Instead mint one HERE (from the deploy identity,
# which has AR read) and ship it as a short-lived file the VM feeds to
# `docker login` — keeps the token off the command line / argv.
TOKEN_FILE="$(mktemp)"
gcloud auth print-access-token > "$TOKEN_FILE"
gcloud compute scp "${SSH_FLAGS[@]}" "$TOKEN_FILE" "$TARGET:~/app/.ar_token" \
    --project="$GCP_PROJECT_ID" --zone="$ZONE"
rm -f "$TOKEN_FILE"

# ─── Step 4: Pull & Run on VM (no build, no tarball) ─────────────
echo "[4/5] Pulling image on VM and starting..."
gcloud compute ssh "$TARGET" \
    --project="$GCP_PROJECT_ID" --zone="$ZONE" "${SSH_FLAGS[@]}" \
    --command='
        set -e
        AR_HOST="'"$AR_HOST"'"
        IMAGE="'"$IMAGE_LATEST"'"
        cd ~/app

        # Preserve data across deployments
        mkdir -p data cache results logs

        # Force preview mode on every deploy: drop the persisted preview/live
        # toggle so the bot boots with PREVIEW_MODE from .env. Telegram
        # /live 3 CONFIRM re-creates the file as needed.
        rm -f data/runtime_state.json

        # Authenticate to AR with the shipped token, then drop it immediately.
        docker login -u oauth2accesstoken --password-stdin "$AR_HOST" < ~/app/.ar_token
        rm -f ~/app/.ar_token

        echo "Pulling $IMAGE ..."
        docker pull "$IMAGE"

        # Build-manifest + claude-drift witness (J2, 2026-07-28): probe the
        # freshly PULLED image BEFORE the container swap, so a drift marker is
        # consumed by the boot that follows this swap, not left for a
        # weeks-later restart (the first cut wrote it post-boot, a race that
        # could defer the exact alert this exists to deliver). NOTE: no
        # apostrophes anywhere in this remote command; it is single-quoted.
        # on claude CLI version change: loud print + WARNING + marker the bot
        # surfaces once on Telegram (main.py _consume_claude_drift_marker).
        # NOTE: the grep/sed parsing here has no test coverage (shell) — the
        # python-side consume is unit-tested; keep the line format stable.
        # Never fails the deploy.
        set +e
        GIT_SHA="'"$GIT_SHA"'"
        LOG="$HOME/app/logs/hygiene.log"
        TS="$(date -u +%Y%m%dT%H%M%SZ)"
        DIGEST="$(docker inspect --format "{{index .RepoDigests 0}}" "$IMAGE" 2>/dev/null)"
        META="$(docker run --rm --entrypoint sh "$IMAGE" -c ". /etc/os-release; echo \$VERSION_ID; node --version; claude --version | awk \"{print \\\$1}\"; python --version | awk \"{print \\\$2}\"; pip freeze | sort | sha256sum | cut -c1-16" 2>/dev/null)"
        OS_ID="$(echo "$META" | sed -n 1p)"
        NODE_V="$(echo "$META" | sed -n 2p)"
        CLAUDE_V="$(echo "$META" | sed -n 3p)"
        PY_V="$(echo "$META" | sed -n 4p)"
        PIP_H="$(echo "$META" | sed -n 5p)"
        LINE="$TS build-manifest sha=$GIT_SHA image=$DIGEST os=$OS_ID node=$NODE_V claude=$CLAUDE_V python=$PY_V pipfreeze=$PIP_H"
        echo "$LINE" >> "$LOG"
        echo "  $LINE"
        # Diff vs the LAST manifest line (grep the marker, never tail -1 —
        # vm_hygiene.sh appends other receipts to the same log).
        PREV="$(grep " build-manifest " "$LOG" | grep -v "$TS" | tail -1)"
        OLD_CLAUDE="$(echo "$PREV" | sed -n "s/.*claude=\([^ ]*\).*/\1/p")"
        if [ -n "$OLD_CLAUDE" ] && [ -n "$CLAUDE_V" ] && [ "$OLD_CLAUDE" != "$CLAUDE_V" ]; then
            echo "  WARNING: claude-code CLI CHANGED: $OLD_CLAUDE -> $CLAUDE_V (gate behavior may drift)"
            echo "$TS WARNING claude-version-drift $OLD_CLAUDE -> $CLAUDE_V" >> "$LOG"
            printf "{\"old\": \"%s\", \"new\": \"%s\", \"ts\": \"%s\"}\n" \
                "$OLD_CLAUDE" "$CLAUDE_V" "$TS" > ~/app/data/claude-version-drift.json
        fi
        set -e

        # Swap container: stop old, run new. Downtime is the few seconds of
        # pull (usually cached) + restart.
        docker stop poly-poly-bot 2>/dev/null || true
        docker rm poly-poly-bot 2>/dev/null || true

        # --memory guardrail: on the 2GB e2-small the bot runs ~640MB steady
        # and ~1GB at the chunked discovery peak, so 1500m leaves headroom yet
        # caps a runaway. If the container ever exceeds it, the cgroup OOM-kills
        # just THIS container (which --restart brings back) instead of the
        # kernel OOM-killer taking down the guest agent / networking and
        # network-deading the whole VM (the 2026-06-15 outage). memory-swap=
        # memory disables swap for the container.
        docker run -d \
            --name poly-poly-bot \
            --restart unless-stopped \
            --memory=1500m \
            --memory-swap=1500m \
            --log-opt max-size=50m --log-opt max-file=3 \
            --env-file .env \
            -v ~/app/data:/app/data \
            -v ~/app/cache:/app/cache \
            -v ~/app/results:/app/results \
            -v ~/app/logs:/app/logs \
            "$IMAGE"

        echo "Container started:"
        docker ps --filter name=poly-poly-bot --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

        docker logout "$AR_HOST" >/dev/null 2>&1 || true

        # Reclaim space: the previous :latest image is now untagged (<none>)
        # because the pull re-pointed the tag. Only touches dangling images +
        # stopped containers; the current image stays.
        echo "Pruning dangling images and stopped containers..."
        docker image prune -f
        docker container prune -f
    '

# ─── Step 5: Verify ──────────────────────────────────────────────
echo "[5/5] Verifying..."
sleep 3
gcloud compute ssh "$TARGET" \
    --project="$GCP_PROJECT_ID" --zone="$ZONE" "${SSH_FLAGS[@]}" \
    --command='docker logs poly-poly-bot --tail 20'

echo ""
echo "=== Deployment complete ($IMAGE_SHA) ==="
echo "  Monitor: gcloud compute ssh $INSTANCE --zone=$ZONE --tunnel-through-iap --command='docker logs -f poly-poly-bot'"
echo "  Stop:    gcloud compute ssh $INSTANCE --zone=$ZONE --tunnel-through-iap --command='docker stop poly-poly-bot'"
echo "  Rollback: re-pull a prior tag, e.g. docker pull $IMAGE:<old-sha> && docker run ... $IMAGE:<old-sha>"
