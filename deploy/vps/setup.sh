#!/usr/bin/env bash
# News247 on your own Linux server (Hetzner, Oracle Cloud, DigitalOcean, any Ubuntu/Debian box).
# The same app Render ran, on a permanent disk, with HTTPS from Caddy and updates from GitHub.
#
#   curl -fsSL https://raw.githubusercontent.com/adhabnr-ux/News-24-7/claude/market-monitoring-system-bq69x4/deploy/vps/setup.sh | sudo bash
#
# Run it again any time: it keeps your settings and only fills in what's missing.
# Full guide: docs/MOVE-OFF-RENDER.md
set -euo pipefail

REPO="${REPO:-https://github.com/adhabnr-ux/News-24-7.git}"
BRANCH="${BRANCH:-claude/market-monitoring-system-bq69x4}"
DIR="${DIR:-/opt/news247}"
VPS="$DIR/deploy/vps"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ask() { # ask VAR "question" [secret]: prompts on the terminal even when piped from curl
  local var="$1" q="$2" secret="${3:-}" val=""
  if [ -n "$secret" ]; then read -r -s -p "$q: " val </dev/tty; echo >/dev/tty; else read -r -p "$q: " val </dev/tty; fi
  printf -v "$var" '%s' "$val"
}
setenv() { # setenv KEY VALUE: set a line in .env (keeps every other line)
  local k="$1" v="$2" f="$VPS/.env"
  if grep -q "^$k=" "$f"; then
    local tmp; tmp="$(mktemp)"; awk -v k="$k" -v v="$v" -F= '$1==k{print k"="v; next} {print}' "$f" >"$tmp"; cat "$tmp" >"$f"; rm -f "$tmp"
  else
    printf '%s=%s\n' "$k" "$v" >>"$f"
  fi
}
getenv() { grep -E "^$1=" "$VPS/.env" 2>/dev/null | head -1 | cut -d= -f2- || true; }

if [ "$(uname -s)" != Linux ]; then
  echo "This sets up a Linux server (Hetzner, Oracle Cloud, ...), not this $(uname -s) computer."
  echo "Create the server first, connect to it (ssh root@SERVER-IP), and run this command there."
  echo "Guide: https://github.com/adhabnr-ux/News-24-7/blob/claude/market-monitoring-system-bq69x4/docs/MOVE-OFF-RENDER.md"
  exit 1
fi
[ "$(id -u)" = 0 ] || { echo "Run as root: sudo bash setup.sh"; exit 1; }

say "1/6  Packages (Docker, git)"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
command -v git >/dev/null 2>&1 || (apt-get update -y && apt-get install -y git)
systemctl enable --now docker >/dev/null 2>&1 || true

# Small servers (1 GB) need swap to build the image and stay up
if [ "$(awk '/MemTotal/{print $2}' /proc/meminfo)" -lt 1900000 ] && ! swapon --show | grep -q .; then
  say "      Adding 2 GB of swap (this server has under 2 GB of memory)"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
fi

say "2/6  The app ($REPO, $BRANCH)"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin "$BRANCH" && git -C "$DIR" checkout -q "$BRANCH" && git -C "$DIR" reset -q --hard "origin/$BRANCH"
else
  git clone -q --branch "$BRANCH" "$REPO" "$DIR"
fi

say "3/6  Settings (the same values you had on Render)"
[ -f "$VPS/.env" ] || cp "$VPS/env.example" "$VPS/.env"
chmod 600 "$VPS/.env"
if [ -z "$(getenv DASHBOARD_TOKEN)" ]; then
  echo "Paste DASHBOARD_TOKEN from Render (Environment tab). Keeping it means phones subscribed on"
  echo "Render keep getting alerts. Leave empty to make a new one (phones then turn alerts on again)."
  ask TOKEN "DASHBOARD_TOKEN" secret
  [ -n "$TOKEN" ] || TOKEN="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)"
  setenv DASHBOARD_TOKEN "$TOKEN"
fi
if [ -z "$(getenv CONTACT_EMAIL)" ]; then
  ask EMAIL "CONTACT_EMAIL (the SEC asks for one in its requests)"
  setenv CONTACT_EMAIL "$EMAIL"
fi
for k in STATE_DB ALPACA_KEY ALPACA_SECRET; do
  if [ -z "$(getenv $k)" ]; then
    ask V "$k from Render (Enter to skip)" secret
    [ -z "$V" ] || setenv "$k" "$V"
  fi
done
echo "WhatsApp, if you used it: edit $VPS/.env afterwards (the WHATSAPP_* lines), then run"
echo "  cd $VPS && docker compose up -d"

say "4/6  The address (HTTPS)"
if [ -z "$(getenv SITE_ADDRESS)" ]; then
  IP="$(curl -fsS4 https://api.ipify.org || curl -fsS4 https://ifconfig.me)"
  DEFAULT="$(echo "$IP" | tr . -).sslip.io"
  echo "Your own domain (pointed at $IP with an A record), or Enter for a free address: $DEFAULT"
  ask SITE "Domain"
  SITE="${SITE:-$DEFAULT}"
  setenv SITE_ADDRESS "$SITE"
  setenv PUBLIC_URL "https://$SITE"
fi
SITE="$(getenv SITE_ADDRESS)"

say "5/6  Firewall (22, 80, 443)"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q active; then
  ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi
# Oracle Cloud's Ubuntu images ship iptables rules that drop everything but SSH
if iptables -S INPUT 2>/dev/null | grep -q -- '-j REJECT'; then
  for p in 80 443; do
    iptables -C INPUT -p tcp --dport $p -j ACCEPT 2>/dev/null || iptables -I INPUT 5 -p tcp --dport $p -j ACCEPT
  done
  command -v netfilter-persistent >/dev/null 2>&1 && netfilter-persistent save >/dev/null 2>&1 || true
fi

say "6/6  Start, and keep up to date"
cd "$VPS"
docker compose up -d --build

cat >/etc/systemd/system/news247-update.service <<UNIT
[Unit]
Description=Update News247 from GitHub when there's a new commit
After=docker.service network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'cd $DIR && git fetch -q origin $BRANCH && if [ "\$(git rev-parse HEAD)" != "\$(git rev-parse origin/$BRANCH)" ]; then git reset -q --hard origin/$BRANCH && cd $VPS && docker compose up -d --build && docker image prune -f >/dev/null; fi'
UNIT
cat >/etc/systemd/system/news247-update.timer <<UNIT
[Unit]
Description=Check GitHub for News247 updates every hour

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now news247-update.timer >/dev/null

say "Waiting for the app to answer…"
for _ in $(seq 1 60); do
  if curl -fsS "https://$SITE/health" >/dev/null 2>&1; then break; fi
  sleep 5
done
if curl -fsS "https://$SITE/health" >/dev/null 2>&1; then
  say "News247 is up: https://$SITE/app/"
else
  say "Started, but https://$SITE isn't answering yet (the certificate can take a minute)."
  echo "Check: cd $VPS && docker compose logs --tail 50"
fi
echo
echo "On your phone: open https://$SITE/app/?token=<your DASHBOARD_TOKEN>, Add to Home Screen,"
echo "and tap Turn on alerts. Logs: cd $VPS && docker compose logs -f news247"
