#!/usr/bin/env bash
# GCE startup script: provision Zeke on a fresh Debian VM under systemd.
# Runs automatically on first boot (passed via --metadata-from-file). Idempotent.
# It sets everything up EXCEPT the token — you add that, then start the service
# (so the secret never lives in instance metadata or gcloud history).
set -euxo pipefail

REPO="https://github.com/k3-mt/zu"
DEST=/opt/zu

apt-get update
apt-get install -y python3-venv python3-pip git

if [ -d "$DEST/.git" ]; then
  git -C "$DEST" pull --ff-only || true
else
  git clone --depth 1 "$REPO" "$DEST"
fi

python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install --upgrade pip
"$DEST/.venv/bin/pip" install -r "$DEST/community/discord-bot/requirements.txt"

# Token file — created blank; fill it in over SSH, then `systemctl start zeke`.
if [ ! -f /etc/zeke.env ]; then
  cat > /etc/zeke.env <<'EOF'
DISCORD_BOT_TOKEN=
# WELCOME_CHANNEL=general
EOF
  chmod 600 /etc/zeke.env
fi

install -m 0644 "$DEST/community/discord-bot/deploy/zeke.service" /etc/systemd/system/zeke.service
systemctl daemon-reload
systemctl enable zeke   # enabled, but NOT started until the token is set
