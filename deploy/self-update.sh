#!/usr/bin/env bash
# Pull the latest code and reinstall. Launched detached (via systemd-run) by the
# web UI's "Update" action so it survives the service restart that install.sh does.
# Safe to run by hand too:  sudo bash deploy/self-update.sh /path/to/repo
set -euo pipefail
REPO="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# The service runs as root but the repo is usually owned by the user who cloned
# it; without this, git refuses with "detected dubious ownership".
git config --global --add safe.directory "$REPO" 2>/dev/null || true

cd "$REPO"
echo "self-update: pulling in $REPO"
git pull --ff-only
# Deps rarely change between updates; skip the apt step so updates are fast and
# don't burn cellular data. install.sh still refreshes the venv package + assets.
SKIP_APT=1 exec "$REPO/install.sh"
