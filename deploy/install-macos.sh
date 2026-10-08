#!/bin/bash
# One-command setup for running News247 24/7 on a Mac and texting alerts to your iMessage.
#
#   cd News-24-7 && ./deploy/install-macos.sh
#
# What it does:
#   1. creates .venv and installs News247
#   2. asks for your phone number (and an e-mail for the SEC's required contact header)
#      and writes them to .env; creates config.yaml if you don't have one
#   3. installs a launchd agent that starts at login, restarts on crash, and keeps the Mac
#      awake (caffeinate) while it runs
#   4. sends a test iMessage — macOS will ask once to let Terminal/python control Messages: click OK
#
# Re-run any time to update. Uninstall: ./deploy/install-macos.sh --uninstall
set -euo pipefail

LABEL="com.news247.monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  say "Removed the background service. Your config, .env and data/ were left in place."
  exit 0
fi

[[ "$(uname)" == "Darwin" ]] || { echo "This installer is for macOS."; exit 1; }

# ---------------------------------------------------------------- 1. Python + install
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$(command -v "$cand")"; break
  fi
done
if [[ -z "$PY" ]]; then
  warn "Python 3.10+ not found. Install it with:  brew install python@3.12   (or from python.org), then re-run."
  exit 1
fi
say "Using $PY"
[[ -d .venv ]] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e .
say "Installed News247 $(.venv/bin/news247 --version | awk '{print $2}')"

# ---------------------------------------------------------------- 2. phone number + config
touch .env
getenv() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
setenv() {
  if grep -qE "^$1=" .env; then
    /usr/bin/sed -i '' "s|^$1=.*|$1=$2|" .env
  else
    echo "$1=$2" >> .env
  fi
}

PHONE="$(getenv IMESSAGE_TO)"
if [[ -z "$PHONE" ]]; then
  read -r -p "Phone number (or Apple ID e-mail) to text alerts to, e.g. +15551234567: " PHONE
fi
PHONE="$(echo "$PHONE" | tr -d ' ()-')"
[[ "$PHONE" =~ ^\+?[0-9]{10,15}$ || "$PHONE" == *@* ]] || { warn "That doesn't look like a phone number or e-mail: $PHONE"; exit 1; }
[[ "$PHONE" == *@* || "$PHONE" == +* ]] || PHONE="+1$PHONE"   # assume US if no country code
setenv IMESSAGE_TO "$PHONE"
setenv IMESSAGE_ENABLED true

EMAIL="$(getenv CONTACT_EMAIL)"
if [[ -z "$EMAIL" ]]; then
  read -r -p "Your e-mail (the SEC requires a contact in requests; never shared elsewhere): " EMAIL
fi
setenv CONTACT_EMAIL "$EMAIL"

# Optional speed upgrades (press Enter to skip; add later by editing .env and re-running)
if [[ -z "$(getenv X_BEARER_TOKEN)" ]]; then
  echo
  echo "Optional — X/Twitter real-time stream: OpenAI/@sama posts and squawk accounts (@DeItaone,"
  echo "@FirstSquawk) that relay FT/WSJ/Bloomberg scoops within seconds. Pay-per-use, roughly \$0.005"
  echo "per post. Get a bearer token at https://developer.x.com (Projects & Apps > Keys and tokens)."
  read -r -p "X bearer token (Enter to skip): " XTOK
  if [[ -n "$XTOK" ]]; then setenv X_BEARER_TOKEN "$XTOK"; setenv X_STREAM_ENABLED true; fi
fi
if [[ -z "$(getenv ALPACA_KEY)" ]]; then
  echo
  echo "Optional — Alpaca news stream (free): Benzinga headlines pushed in real time. Sign up at"
  echo "https://app.alpaca.markets (paper trading, no money needed) and create API keys."
  read -r -p "Alpaca API key ID (Enter to skip): " AKEY
  if [[ -n "$AKEY" ]]; then
    read -r -p "Alpaca secret key: " ASEC
    setenv ALPACA_KEY "$AKEY"; setenv ALPACA_SECRET "$ASEC"; setenv ALPACA_ENABLED true
  fi
fi
chmod 600 .env

if [[ ! -f config.yaml ]]; then
  .venv/bin/news247 init --path config.yaml >/dev/null
  say "Created config.yaml (edit it any time; re-run this script or restart the service after changes)"
fi
mkdir -p data

# ---------------------------------------------------------------- 3. launchd service
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string><string>-s</string>
    <string>$REPO/.venv/bin/news247</string>
    <string>-c</string><string>$REPO/config.yaml</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$REPO/data/service.out.log</string>
  <key>StandardErrorPath</key><string>$REPO/data/service.err.log</string>
</dict>
</plist>
EOF

# ---------------------------------------------------------------- 4. test message (permission prompt)
say "Sending a test iMessage to $PHONE — if macOS asks to let Terminal control Messages, click OK."
open -a Messages || true
sleep 2
if .venv/bin/news247 -c config.yaml test-notify --only imessage; then
  say "Test iMessage sent. Check your phone."
else
  warn "The test message failed. Make sure Messages is signed in to iMessage (Messages > Settings > iMessage),"
  warn "and allow automation in System Settings > Privacy & Security > Automation, then re-run this script."
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
say "News247 is running in the background and will start automatically at login."
cat <<MSG

  Dashboard:   http://localhost:8247
  Logs:        tail -f "$REPO/data/news247.log"
  Stop:        launchctl bootout gui/$(id -u)/$LABEL
  Uninstall:   ./deploy/install-macos.sh --uninstall

  For true 24/7: keep the Mac plugged in, and in System Settings > Battery (or Energy)
  turn on "Prevent automatic sleeping when the display is off". Closing a laptop lid
  still sleeps it unless an external display is connected.
MSG
