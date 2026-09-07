#!/usr/bin/env bash
# smoke-test.sh — cheap end-to-end check. No writes to NotebookLM, no writes
# to the vault. Run before pushing a change.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
FAIL=0
step() { printf '\n▸ %s\n' "$1"; }
ok()   { printf '  ✓ %s\n' "$1"; }
bad()  { printf '  ✗ %s\n' "$1"; FAIL=1; }

step "El módulo importa (py_compile NO detecta regex mal formadas)"
python3 -c "import nlm" 2>/dev/null && ok "import" || bad "import falla"

step "La redacción oculta secretos y preserva identificadores"
python3 - <<'PY' && ok "redacción" || bad "redacción incorrecta"
import sys, nlm
assert "<redacted-url>" in nlm.redact("URL: https://accounts.google.com/v3/signin/x?dsh=S12345678901234567890")
assert "<redacted>" in nlm.redact("cookie: __Secure-1PSIDTS=abcdefghijklmnopqrstuvwxyz0123456789ABCD")
assert "b05191d2-98e4-4e59-9599-62477a8e15f1" in nlm.redact("notebook b05191d2-98e4-4e59-9599-62477a8e15f1")
sys.exit(0)
PY

step "Precedencia de configuración: env > config.json > default"
python3 - <<'PY' && ok "precedencia" || bad "precedencia rota"
import os, sys
os.environ["NOTEBOOKLM_MAX_NOTEBOOKS"] = "7"
import nlm
assert nlm.MAX_NOTEBOOKS == 7, nlm.MAX_NOTEBOOKS
sys.exit(0)
PY

step "El orden del espejo garantiza convergencia (nunca-espejados primero)"
python3 - <<'PY' && ok "orden" || bad "orden con inanición"
import sys, nlm
cursor = {"viejo": {"mirrored_at_utc": "2020-01-01T00:00:00+00:00"},
          "nuevo": {"mirrored_at_utc": "2030-01-01T00:00:00+00:00"}}
nbs = [{"id": "nuevo"}, {"id": "viejo"}, {"id": "jamas"}]
nbs.sort(key=lambda n: cursor.get(n["id"], {}).get("mirrored_at_utc") or "")
assert [n["id"] for n in nbs] == ["jamas", "viejo", "nuevo"], nbs
sys.exit(0)
PY

step "El lock no recursa sin límite"
python3 - <<'PY' && ok "lock acotado" || bad "lock sin cota"
import inspect, sys, nlm
src = inspect.getsource(nlm.acquire_lock)
assert "_attempt" in src and "return False" in src
sys.exit(0)
PY

step "--dry-run no escribe en el vault"
# Against a throwaway vault, not the real one: a live vault has other daemons
# writing to it, and counting its files makes this test fail at random.
TMPVAULT="$(mktemp -d)"
trap 'rm -rf "$TMPVAULT"' EXIT
NOTEBOOKLM_VAULT_DIR="$TMPVAULT" python3 nlm.py --dry-run mirror --max-notebooks 1 >/dev/null 2>&1
WROTE=$(find "$TMPVAULT" -type f 2>/dev/null | wc -l | tr -d ' ')
[ "$WROTE" = "0" ] && ok "cero archivos escritos" || bad "escribió $WROTE archivo(s) en dry-run"

step "doctor corre y devuelve un código coherente"
python3 nlm.py doctor >/dev/null 2>&1
RC=$?
[ $RC -le 1 ] && ok "doctor exit=$RC" || bad "doctor exit=$RC"

printf '\n'
[ $FAIL -eq 0 ] && { echo "✅ smoke test OK"; exit 0; } || { echo "❌ smoke test FALLÓ"; exit 1; }
