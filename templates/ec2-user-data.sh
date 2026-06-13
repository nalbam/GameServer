#!/bin/bash
#
# GameServer EC2 bootstrap (Amazon Linux 2023)
# Rendered by gameserver.py — placeholder tokens are substituted before launch.
#
# Installs Docker + Git, pulls the game image from ghcr.io, fetches the
# runtime env from SSM, and starts the container. The container is bound to
# 0.0.0.0:<port> so it is reachable as http://<eip>:<port> immediately.
# HTTPS (Nginx + certbot) is layered on later by the domain-connect step.
#
set -euo pipefail

readonly LOG_FILE="/var/log/user-data.log"
readonly GAME="__GAME__"
readonly INSTALL_DIR="/home/ec2-user/__GAME__"
readonly DOCKER_IMAGE="__DOCKER_IMAGE__"
readonly DOCKER_TAG="__DOCKER_TAG__"
readonly CONTAINER_NAME="__GAME__"
readonly APP_PORT="__APP_PORT__"
readonly SSM_PARAM_NAME="__SSM_PARAM_NAME__"

exec > >(tee -a "$LOG_FILE") 2>&1

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"; }

log "=== GameServer bootstrap: $GAME ==="

log "Updating system..."
dnf update -y

log "Installing Docker + Git..."
dnf install -y docker git
systemctl enable docker
systemctl start docker
usermod -aG docker ec2-user

mkdir -p "$INSTALL_DIR"
chown -R ec2-user:ec2-user "$INSTALL_DIR"

log "Fetching env from SSM: $SSM_PARAM_NAME"
ENV_CONTENT=$(aws ssm get-parameter \
    --name "$SSM_PARAM_NAME" \
    --with-decryption \
    --output text \
    --query Parameter.Value 2>/dev/null || echo "")

if [[ -z "$ENV_CONTENT" ]]; then
    log "SSM param empty; writing minimal .env"
    cat > "$INSTALL_DIR/.env" <<EOF
NODE_ENV=production
PORT=$APP_PORT
EOF
else
    echo "$ENV_CONTENT" > "$INSTALL_DIR/.env"
fi
chmod 600 "$INSTALL_DIR/.env"
chown ec2-user:ec2-user "$INSTALL_DIR/.env"

log "Pulling image: ${DOCKER_IMAGE}:${DOCKER_TAG}"
docker pull "${DOCKER_IMAGE}:${DOCKER_TAG}"

log "Starting container (0.0.0.0:${APP_PORT})..."
docker stop "$CONTAINER_NAME" 2>/dev/null || true
docker rm "$CONTAINER_NAME" 2>/dev/null || true
docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    -p "0.0.0.0:${APP_PORT}:${APP_PORT}" \
    --env-file "$INSTALL_DIR/.env" \
    "${DOCKER_IMAGE}:${DOCKER_TAG}"

log "=== Bootstrap complete: http://<eip>:${APP_PORT} ==="
