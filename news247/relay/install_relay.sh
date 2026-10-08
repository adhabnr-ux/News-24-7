#!/bin/bash
# News247 iMessage relay — installer for macOS.
#
# Turns this Mac into your private iMessage gateway: it keeps one encrypted connection open to
# your News247 monitor and texts you its alerts through the Messages app. No ports to open, no
# third-party service.
#
# Easiest: copy the one-line command from your dashboard's Setup page (it fills in everything):
#   curl -fsSL 'https://YOUR-MONITOR/relay/install.sh?token=…' | bash
# From a clone of the repo:
#   ./deploy/install-relay-macos.sh --server https://YOUR-MONITOR --secret RELAY_SECRET
# Remove:
#   … --uninstall
set -euo pipefail

SERVER="${N247_SERVER:-}"
SECRET="${N247_SECRET:-}"
PACKAGE_URL="${N247_PACKAGE_URL:-}"
SOURCE_DIR=""
NAME=""
SERVICE="imessage"
ACTION="install"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --secret) SECRET="$2"; shift 2 ;;
    --package-url) PACKAGE_URL="$2"; shift 2 ;;
    --source) SOURCE_DIR="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --service) SERVICE="$2"; shift 2 ;;
    --uninstall) ACTION="uninstall"; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

LABEL="com.news247.relay"
APP="$HOME/Library/Application Support/News247Relay"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/News247Relay.log"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

if [[ "$ACTION" == "uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  rm -rf "$APP/venv"
  say "Removed the News247 relay. (Settings kept in: $APP — delete that folder to forget them.)"
  exit 0
fi

[[ "$(uname)" == "Darwin" ]] || die "The relay runs on macOS (it sends through the Messages app)."
[[ -n "$SERVER" && -n "$SECRET" ]] || die "Missing server/secret. Copy the install command from your dashboard's Setup page."
[[ -n "$PACKAGE_URL" || -n "$SOURCE_DIR" ]] || die "Missing --package-url or --source."
case "$SERVICE" in imessage|sms|auto) ;; *) die "--service must be imessage, sms or auto" ;; esac

# ------------------------------------------------------------------ 1. Python 3.10+
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$cand" >/dev/null 2>&1 \
     && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PY="$(command -v "$cand")"; break
  fi
done
if [[ -z "$PY" ]]; then
  # No suitable Python (macOS only ships 3.9 with the developer tools): get one with uv, a small
  # self-contained Python manager from astral.sh. Nothing is installed system-wide.
  UV="$(command -v uv || true)"
  [[ -n "$UV" ]] || UV="$HOME/.local/bin/uv"
  if [[ ! -x "$UV" ]]; then
    say "Getting Python 3.12 via uv (astral.sh)…"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
  fi
  "$UV" python install 3.12 >/dev/null
  PY="$("$UV" python find 3.12)"
fi
say "Using Python: $PY"

# ------------------------------------------------------------------ 2. install News247
mkdir -p "$APP" "$(dirname "$LOG")" "$HOME/Library/LaunchAgents"
if [[ ! -x "$APP/venv/bin/python" ]] || ! "$APP/venv/bin/python" -c 'import sys' >/dev/null 2>&1; then
  rm -rf "$APP/venv"
  "$PY" -m venv "$APP/venv"
fi
"$APP/venv/bin/python" -m pip install -q --upgrade pip >/dev/null
if [[ -n "$SOURCE_DIR" ]]; then
  "$APP/venv/bin/python" -m pip install -q --upgrade "$SOURCE_DIR"
else
  curl -fsSL "$PACKAGE_URL" -o "$APP/news247.tar.gz" || die "Could not download News247 from your monitor."
  "$APP/venv/bin/python" -m pip install -q --upgrade --force-reinstall --no-deps "$APP/news247.tar.gz"
  "$APP/venv/bin/python" -m pip install -q "$APP/news247.tar.gz"
fi
say "Installed $("$APP/venv/bin/python" -c 'import news247; print("News247", news247.__version__)')"

# ------------------------------------------------------------------ 3. settings (private file)
[[ -n "$NAME" ]] || NAME="$(scutil --get ComputerName 2>/dev/null || hostname -s)"
(
  umask 077
  cat > "$APP/relay.env" <<ENV
RELAY_SERVER=$SERVER
RELAY_SECRET=$SECRET
RELAY_NAME=$NAME
RELAY_SERVICE=$SERVICE
ENV
)
REAL_PY="$("$APP/venv/bin/python" -c 'import os, sys; print(os.path.realpath(sys.executable))')"

# ------------------------------------------------------------------ 4. background service
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP/venv/bin/python</string>
    <string>-m</string><string>news247.relay.agent</string>
    <string>run</string>
    <string>--env-file</string><string>$APP/relay.env</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PL
open -g -a Messages || true
START=$(wc -l < "$LOG" 2>/dev/null || echo 0)
say "Starting the relay. If macOS asks whether \"python\" may control \"Messages\", click OK."
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL" 2>/dev/null || true

CONNECTED=""
for _ in $(seq 1 20); do
  if tail -n +"$((START + 1))" "$LOG" 2>/dev/null | grep -q "connected to"; then CONNECTED=1; break; fi
  sleep 1
done
if [[ -n "$CONNECTED" ]]; then
  say "Connected to your monitor."
else
  warn "Not connected yet. The check below shows why; the log is $LOG"
fi

# ------------------------------------------------------------------ 5. check everything
echo
"$APP/venv/bin/python" -m news247.relay.agent doctor --env-file "$APP/relay.env" || true
echo
if ! "$APP/venv/bin/python" -c "import sqlite3,os; sqlite3.connect('file:'+os.path.expanduser('~/Library/Messages/chat.db')+'?mode=ro', uri=True).execute('select 1 from message limit 1')" >/dev/null 2>&1; then
  printf '%s' "$REAL_PY" | pbcopy 2>/dev/null || true
  cat <<FDA
Optional, recommended: let the relay confirm delivery and take text commands ("pause 2h").
  1. System Settings > Privacy & Security > Full Disk Access  (opening it now)
  2. Click +, press Cmd+Shift+G, paste this path (it's on your clipboard) and click Open:
       $REAL_PY
  3. Then restart the relay:  launchctl kickstart -k gui/$(id -u)/$LABEL
FDA
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" 2>/dev/null || true
  echo
fi

cat <<DONE
News247 relay installed on "$NAME". It starts at login and reconnects by itself.
  Status:     your dashboard's Setup page (or: tail -f "$LOG")
  Test:       press "Send test message" on the Setup page
  Restart:    launchctl kickstart -k gui/$(id -u)/$LABEL
  Uninstall:  curl … | bash -s -- --uninstall    (or delete $PLIST)

Tips
  • Sign Messages on this Mac into a separate Apple ID (not the one on your iPhone): texts
    from your own Apple ID land in a "note to self" thread with no notification.
  • Save that Apple ID as a contact on your iPhone so alerts never get filtered as unknown.
  • For true 24/7, keep this Mac plugged in; the relay keeps it from idle-sleeping.
DONE
