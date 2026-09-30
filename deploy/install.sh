#!/usr/bin/env bash
# Install or update Aimsir on a Debian/Ubuntu LXC (run as root).
#
#   First time:  apt update && apt install -y git
#                git clone https://github.com/ryano365/aimsir.git /opt/aimsir
#                bash /opt/aimsir/deploy/install.sh
#   Update:      bash /opt/aimsir/deploy/install.sh     (pulls the latest code first)
set -euo pipefail

# Everything lives in main() so bash has read the whole file before `git pull` can change it.
main() {

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SVC_USER=aimsir
PORT="${PORT:-8080}"

[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }
cd "$APP_DIR"

echo "==> System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip ca-certificates >/dev/null

if [ -d .git ]; then
  echo "==> Pulling latest code"
  git config --global --add safe.directory "$APP_DIR" || true
  git pull --ff-only
fi

echo "==> Service user"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SVC_USER"
mkdir -p "$APP_DIR/data/inbox"
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR/data"

echo "==> Python environment (first run takes a few minutes)"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -c "import eccodes, h5py; print('    ecCodes', eccodes.codes_get_api_version(), '/ h5py', h5py.__version__)"

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  NEW_ENV=1
fi

echo "==> systemd service"
sed -e "s#/opt/aimsir#$APP_DIR#g" -e "s#--port 8080#--port $PORT#" deploy/aimsir.service > /etc/systemd/system/aimsir.service
systemctl daemon-reload
systemctl enable aimsir >/dev/null 2>&1
systemctl restart aimsir

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Aimsir is running: http://${IP:-<ct-ip>}:$PORT"
if [ "${NEW_ENV:-0}" = 1 ] || ! grep -qE '^MET_API_KEY=.+' .env; then
  echo
  echo "Next: add your Met Éireann API key, then restart:"
  echo "  nano $APP_DIR/.env"
  echo "  systemctl restart aimsir"
fi
echo "Logs: journalctl -u aimsir -f"
}

main "$@"
