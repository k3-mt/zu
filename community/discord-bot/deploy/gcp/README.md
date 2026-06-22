# Hosting Zeke on Google Cloud (Always Free)

Zeke is a single always-on process holding one outbound WebSocket — no inbound ports.
The genuinely-free, always-on home for that on GCP is a **Compute Engine `e2-micro`
Always-Free VM**, run under `systemd`.

## Why e2-micro, not Cloud Run

| Option | Always-on bot? | Free? |
|---|---|---|
| **GCE `e2-micro` (Always Free)** | ✅ a real VM that stays up | ✅ 1/mo in `us-west1`/`us-central1`/`us-east1`, 30GB disk |
| Cloud Run | ⚠️ scales to zero; needs `min-instances=1` + always-on CPU for a gateway bot | ❌ that config is billable |
| Fly.io | ✅ | ~ usage-based now, card required, small allowance |
| Railway | ✅ | ❌ one-time trial credit, then ~$5/mo |

A bot can't live on Cloud Run's request model for free — it holds a persistent gateway
connection, not HTTP requests. The e2-micro VM is the right tool. (Limits change — verify
current Always-Free terms; a billing account/card is required but isn't charged within them.)

## Deploy

Prereqs: `gcloud` installed, `gcloud auth login`, and a project selected
(`gcloud config set project <id>`) with billing enabled.

```bash
cd community/discord-bot/deploy/gcp
./create-vm.sh                      # creates the e2-micro VM + runs startup.sh
```

`startup.sh` (runs on the VM) installs Python + deps, clones the repo to `/opt/zu`, and
installs the [`zeke.service`](../zeke.service) systemd unit — **enabled but not started**,
because the token isn't set yet. Finish over SSH:

```bash
gcloud compute ssh zeke --zone=us-central1-a
sudo nano /etc/zeke.env            # DISCORD_BOT_TOKEN=...  (chmod 600 already)
sudo systemctl start zeke
journalctl -u zeke -f              # expect: "Zeke is online as ..."
```

The token lives only in `/etc/zeke.env` (mode 600) on the box — never in instance
metadata or shell history. For stronger handling, store it in **Secret Manager** and have
`startup.sh` fetch it instead.

## Update / operate

```bash
# pull latest + restart
gcloud compute ssh zeke --zone=us-central1-a -- 'cd /opt/zu && sudo git pull && sudo systemctl restart zeke'
# logs / status
gcloud compute ssh zeke --zone=us-central1-a -- 'systemctl status zeke; journalctl -u zeke -n 50'
```

Before it'll do anything useful: enable the **Server Members** privileged intent
(Developer Portal → Bot) for the join-welcome, and make sure the bot has **Send Messages**.

## Self-hosting (any VPS)

The same [`zeke.service`](../zeke.service) works on any Debian/Ubuntu box: clone to
`/opt/zu`, make a venv at `/opt/zu/.venv`, `pip install -r requirements.txt`, write
`/etc/zeke.env`, copy the unit to `/etc/systemd/system/`, then `systemctl enable --now zeke`.
