#!/usr/bin/env bash
# Create an Always-Free e2-micro VM running Zeke. Run locally after `gcloud auth login`
# (and once `gcloud config set project <id>`). Re-runnable: delete the VM first to recreate.
#
#   ./create-vm.sh
#
# Always-Free constraints (verify current limits — they change): exactly one e2-micro
# per month, in us-west1 / us-central1 / us-east1, with a <=30GB standard persistent disk.
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
ZONE="${ZONE:-us-central1-a}"
NAME="${NAME:-zeke}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$PROJECT" ]; then
  echo "No project set. Run: gcloud config set project <your-project-id>" >&2
  exit 1
fi

gcloud compute instances create "$NAME" \
  --project="$PROJECT" \
  --zone="$ZONE" \
  --machine-type=e2-micro \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-size=30GB --boot-disk-type=pd-standard \
  --metadata-from-file=startup-script="$HERE/startup.sh"

cat <<EOF

VM '$NAME' created in $ZONE. The startup script is installing deps + the systemd unit.
Finish setup (the token never touches metadata this way):

  gcloud compute ssh $NAME --zone=$ZONE
  sudo nano /etc/zeke.env          # set DISCORD_BOT_TOKEN=...
  sudo systemctl start zeke
  systemctl status zeke            # should be 'active (running)'
  journalctl -u zeke -f            # watch logs / "Zeke is online as ..."

To update later: gcloud compute ssh $NAME --zone=$ZONE -- 'cd /opt/zu && sudo git pull && sudo systemctl restart zeke'
EOF
