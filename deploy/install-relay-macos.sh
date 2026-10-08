#!/bin/bash
# Install the News247 iMessage relay on this Mac from a clone of the repo.
# (Easier: copy the one-line command from your dashboard's Setup page; it needs no clone.)
#
#   ./deploy/install-relay-macos.sh --server https://your-monitor.example.com --secret RELAY_SECRET
#   ./deploy/install-relay-macos.sh --uninstall
#
# The relay secret is shown on the Setup page (or set RELAY_SECRET on the server yourself).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
exec bash "$REPO/news247/relay/install_relay.sh" --source "$REPO" "$@"
