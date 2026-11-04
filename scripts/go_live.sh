#!/usr/bin/env bash
# Start Génesis behind a public Cloudflare quick tunnel and point the WhatsApp numbers at it.
#   scripts/go_live.sh <intake_phone_id> <cradle_phone_id> [more cradles...]
# Without arguments it only starts server + tunnel and prints the URL.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8000}"
mkdir -p .run
uv run uvicorn genesis.server:app --host 127.0.0.1 --port "$PORT" > .run/server.log 2>&1 &
echo $! > .run/server.pid
"${CLOUDFLARED:-$(command -v cloudflared || echo $HOME/.local/bin/cloudflared)}" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" > .run/tunnel.log 2>&1 &
echo $! > .run/tunnel.pid
URL=""
for _ in $(seq 1 40); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' .run/tunnel.log | head -1 || true)
  [ -n "$URL" ] && break
  sleep 0.5
done
[ -z "$URL" ] && { echo "el túnel no dio URL; mira .run/tunnel.log"; exit 1; }
for _ in $(seq 1 30); do curl -fsS "$URL/health" >/dev/null 2>&1 && break; sleep 1; done
echo "Génesis público en: $URL"
echo "Panel: $URL/#token=<GENESIS_ADMIN_TOKEN>"
if [ "$#" -ge 2 ]; then
  intake="$1"; shift
  args=()
  for c in "$@"; do args+=(--cradle "$c"); done
  uv run python scripts/connect_numbers.py --intake "$intake" "${args[@]}" --url "$URL"
  # restart so the server picks up the new .env values
  kill "$(cat .run/server.pid)" && sleep 1
  uv run uvicorn genesis.server:app --host 127.0.0.1 --port "$PORT" > .run/server.log 2>&1 &
  echo $! > .run/server.pid
fi
echo "Para apagar: kill \$(cat .run/server.pid) \$(cat .run/tunnel.pid)"
