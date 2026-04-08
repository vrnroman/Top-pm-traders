#!/bin/bash
set -e

# Configuration
if [ -f .env ]; then
  source .env
fi
PROJECT_ID="${GCP_PROJECT_ID:-poly-bot-reks}"
INSTANCE_NAME="polymarket-bot-vm"
ZONE="asia-northeast1-a"

echo "=========================================="
echo " Update .env & restart bot on GCP VM"
echo "=========================================="

echo "1. Uploading local .env to VM..."
gcloud compute scp .env $INSTANCE_NAME:~/ \
    --project=$PROJECT_ID \
    --zone=$ZONE \
    --ssh-key-expire-after=1h

echo "2. Moving .env into app dir and restarting container..."
gcloud compute ssh $INSTANCE_NAME --project=$PROJECT_ID --zone=$ZONE --command="
mv ~/.env ~/app/.env
cd ~/app
echo 'Restarting container with updated .env...'
sudo docker stop polybot || true
sudo docker rm polybot || true
sudo docker run -d --name polybot -v ~/app/data:/app/data --env-file .env --restart unless-stopped polybot
echo 'Done! Container restarted with new env.'
"

echo "=========================================="
echo "Update complete!"
echo "To view logs:"
echo "gcloud compute ssh $INSTANCE_NAME --project=$PROJECT_ID --zone=$ZONE --command='sudo docker logs -f polybot'"
echo "=========================================="
