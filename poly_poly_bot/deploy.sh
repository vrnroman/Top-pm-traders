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
MACHINE="e2-small"          # 2 vCPU, 2GB RAM

echo "=== Poly Poly Bot Deployment (Python-only) ==="
echo "  Project: $GCP_PROJECT_ID"
echo "  Instance: $INSTANCE"
echo "  Zone: $ZONE"
echo "  Machine: $MACHINE"
echo ""

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

# ─── Step 2: Archive code ────────────────────────────────────────
echo "[2/5] Archiving code..."
ARCHIVE="/tmp/poly-poly-bot-deploy.tar.gz"
tar czf "$ARCHIVE" \
    --exclude='.git' \
    --exclude='cache' \
    --exclude='results' \
    --exclude='logs' \
    --exclude='data' \
    --exclude='__pycache__' \
    --exclude='.env' \
    --exclude='.DS_Store' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='*.egg-info' \
    --exclude='node_modules' \
    -C "$(dirname "$0")" .

echo "  Archive: $(du -h "$ARCHIVE" | cut -f1)"

# ─── Step 3: Upload ─────────────────────────────────────────────
echo "[3/5] Uploading to VM..."
gcloud compute ssh "$INSTANCE" \
    --project="$GCP_PROJECT_ID" --zone="$ZONE" \
    --command='mkdir -p ~/app'
gcloud compute scp "$ARCHIVE" "$INSTANCE:~/deploy.tar.gz" \
    --project="$GCP_PROJECT_ID" --zone="$ZONE"
gcloud compute scp .env "$INSTANCE:~/app/.env" \
    --project="$GCP_PROJECT_ID" --zone="$ZONE"

# ─── Step 4: Build & Run ─────────────────────────────────────────
echo "[4/5] Building & starting on VM..."
gcloud compute ssh "$INSTANCE" \
    --project="$GCP_PROJECT_ID" --zone="$ZONE" \
    --command='
        set -e
        mkdir -p ~/app
        cd ~/app
        tar xzf ~/deploy.tar.gz
        rm ~/deploy.tar.gz

        # Preserve data across deployments
        mkdir -p data cache results logs

        # Stop existing container
        docker stop poly-poly-bot 2>/dev/null || true
        docker rm poly-poly-bot 2>/dev/null || true

        # Build
        docker build -t poly-poly-bot .

        # Run
        docker run -d \
            --name poly-poly-bot \
            --restart unless-stopped \
            --env-file .env \
            -v ~/app/data:/app/data \
            -v ~/app/cache:/app/cache \
            -v ~/app/results:/app/results \
            -v ~/app/logs:/app/logs \
            poly-poly-bot

        echo "Container started:"
        docker ps --filter name=poly-poly-bot --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    '

# ─── Step 5: Verify ──────────────────────────────────────────────
echo "[5/5] Verifying..."
sleep 3
gcloud compute ssh "$INSTANCE" \
    --project="$GCP_PROJECT_ID" --zone="$ZONE" \
    --command='docker logs poly-poly-bot --tail 20'

echo ""
echo "=== Deployment complete ==="
echo "  Monitor: gcloud compute ssh $INSTANCE --zone=$ZONE --command='docker logs -f poly-poly-bot'"
echo "  Stop:    gcloud compute ssh $INSTANCE --zone=$ZONE --command='docker stop poly-poly-bot'"
echo "  Logs:    gcloud compute ssh $INSTANCE --zone=$ZONE --command='cat ~/app/logs/bot-\$(date +%Y-%m-%d).log'"

# Cleanup
rm -f "$ARCHIVE"
