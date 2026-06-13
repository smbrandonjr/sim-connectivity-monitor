#!/usr/bin/env bash
# Pull the latest code and reinstall. Launched detached (via systemd-run) by the
# web UI's "Update" action so it survives the service restart that install.sh does.
# Safe to run by hand too:  sudo bash deploy/self-update.sh /path/to/repo
set -euo pipefail
REPO="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"
echo "self-update: pulling in $REPO"
git pull --ff-only
exec "$REPO/install.sh"
