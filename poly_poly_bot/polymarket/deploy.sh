#!/bin/bash
set -e

# Configuration
if [ -f .env ]; then
  source .env
fi
PROJECT_ID="${GCP_PROJECT_ID:-poly-bot-reks}"
INSTANCE_NAME="polymarket-bot-vm"
ZONE="asia-northeast1-a"
MACHINE_TYPE="e2-micro"

echo "=========================================="
echo " Polymarket Bot GCP Deployment script"
echo "=========================================="

echo "1. Creating deployment archive..."
# Package code without heavy folders to speed up transfer
tar --exclude='node_modules' --exclude='.git' --exclude='dist' --exclude='.env' -czvf deploy.tar.gz .

# Check if the GCP VM instance exists
if ! gcloud compute instances describe $INSTANCE_NAME --project=$PROJECT_ID --zone=$ZONE > /dev/null 2>&1; then
    echo "2. VM instance '$INSTANCE_NAME' not found. Creating it..."
    gcloud compute instances create $INSTANCE_NAME \
        --project=$PROJECT_ID \
        --zone=$ZONE \
        --machine-type=$MACHINE_TYPE \
        --image-family=debian-11 \
        --image-project=debian-cloud \
        --metadata=startup-script="#! /bin/bash
sudo apt-get update
sudo apt-get install -y docker.io
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker \$USER"
    
    echo "Waiting for VM to initialize and install Docker (approx 30s)..."
    sleep 30
else
    echo "2. VM instance '$INSTANCE_NAME' already exists."
fi

echo "3. Uploading archive & local .env file to VM..."
gcloud compute scp deploy.tar.gz .env $INSTANCE_NAME:~/ \
    --project=$PROJECT_ID \
    --zone=$ZONE \
    --ssh-key-expire-after=1h

echo "4. Rebuilding and restarting bot on VM..."
gcloud compute ssh $INSTANCE_NAME --project=$PROJECT_ID --zone=$ZONE --command="
mkdir -p ~/app
# Extract the archive into the app directory
tar -xzf ~/deploy.tar.gz -C ~/app
# Move the .env file securely to the app directory
mv ~/.env ~/app/.env
cd ~/app

mkdir -p ~/app/data

echo 'Building docker image...'
sudo docker build -t polybot .

echo 'Restarting container...'
sudo docker stop polybot || true
sudo docker rm polybot || true
# We map the data directory to the host VM to safeguard your logs across deployments
sudo docker run -d --name polybot -v ~/app/data:/app/data --env-file .env --restart unless-stopped polybot

echo 'Removing remote archive...'
rm ~/deploy.tar.gz
"

echo "5. Cleaning up local archive..."
rm deploy.tar.gz

echo "=========================================="
echo "Deployment Complete! 🚀"
echo "To view logs, run:"
echo "gcloud compute ssh $INSTANCE_NAME --project=$PROJECT_ID --zone=$ZONE --command='sudo docker logs -f polybot'"
echo "=========================================="
