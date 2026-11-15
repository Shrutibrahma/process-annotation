#!/bin/bash
# Runs ON the Oracle server (sent by deploy.ps1). Safe to run again for updates.
set -euo pipefail

sudo cloud-init status --wait >/dev/null 2>&1 || true

if ! command -v docker >/dev/null 2>&1; then
  echo ">> installing Docker"
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER" || true

# Oracle's Ubuntu images block every port but SSH in iptables (on top of the cloud security list)
for p in 80 443; do
  sudo iptables -C INPUT -p tcp --dport "$p" -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 5 -p tcp --dport "$p" -j ACCEPT
done
if command -v netfilter-persistent >/dev/null 2>&1; then sudo netfilter-persistent save >/dev/null; fi

# swap: video conversion can spike memory on the small free shapes
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo ">> installing the app"
sudo mkdir -p /opt/process-annotation/data
sudo rm -rf /opt/process-annotation/app.new && sudo mkdir -p /opt/process-annotation/app.new
sudo tar -xf /tmp/process-annotation.tar -C /opt/process-annotation/app.new
sudo mv /tmp/process-annotation.env /opt/process-annotation/app.new/.env
sudo chmod 600 /opt/process-annotation/app.new/.env
sudo rm -rf /opt/process-annotation/app && sudo mv /opt/process-annotation/app.new /opt/process-annotation/app
if [ -d /tmp/pa-data ]; then
  echo ">> copying your existing studies (the first team created gets them)"
  sudo cp -rn /tmp/pa-data/. /opt/process-annotation/data/ && sudo rm -rf /tmp/pa-data
fi
rm -f /tmp/process-annotation.tar

echo ">> building and starting (first time takes a few minutes)"
cd /opt/process-annotation/app/deploy/oracle
sudo docker compose --env-file /opt/process-annotation/app/.env up -d --build
sudo docker image prune -f >/dev/null
sudo docker compose --env-file /opt/process-annotation/app/.env ps
