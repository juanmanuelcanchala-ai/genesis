#!/usr/bin/env bash
# Ask for each key without echoing it and store it in .env (mode 600, gitignored).
# Empty answer keeps the current value.
set -euo pipefail
cd "$(dirname "$0")/.."
touch .env && chmod 600 .env
for k in MODEL_API_KEY WA_TOKEN WABA_ID WA_APP_ID WA_APP_SECRET MP_ACCESS_TOKEN; do
  read -rsp "$k: " v; echo
  [ -z "$v" ] && continue
  if grep -q "^$k=" .env; then
    python3 - "$k" "$v" <<'PY'
import re, sys, pathlib
k, v = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env"); t = p.read_text()
p.write_text(re.sub(rf"^{k}=.*$", lambda m: f"{k}={v}", t, flags=re.M))
PY
  else
    printf '%s=%s\n' "$k" "$v" >> .env
  fi
done
echo "Guardado en .env:"; cut -d= -f1 .env | sed 's/^/  /'
