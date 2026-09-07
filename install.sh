#!/usr/bin/env bash
# install.sh — instalador idempotente de ingest-notebooklm (6 pasos).
# No hace bootstrap de launchd: eso requiere tu autorización explícita.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${NOTEBOOKLM_STATE_DIR:-$HOME/.config/ingest-notebooklm}"
PLIST_DST="$HOME/Library/LaunchAgents/com.ingest-notebooklm.plist"

say()  { printf '%s\n' "$*"; }
fail() { printf '✗ %s\n' "$*" >&2; exit 1; }

# ── 1. Dependencias ─────────────────────────────────────────────────────────
say "1/6 · Verificando dependencias"
[[ "${BASH_VERSINFO[0]}" -ge 3 ]] || fail "se requiere bash 3+"
command -v python3 >/dev/null || fail "falta python3 (3.10+)"
python3 - <<'PY' || fail "se requiere Python 3.10 o superior"
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
# The LaunchAgent must run the SAME interpreter we just validated. macOS ships
# /usr/bin/python3 at 3.9, so hardcoding it would let the daemon break silently
# while the shell keeps working.
PYBIN="$(command -v python3)"
if ! command -v notebooklm >/dev/null && [[ ! -x "$HOME/.local/bin/notebooklm" ]]; then
  fail "falta el CLI notebooklm. Instalalo con:
     uv tool install \"notebooklm-py[browser,mcp,markdown]\""
fi
say "    ✓ bash, python3 ($(python3 -V 2>&1 | cut -d\  -f2)), notebooklm"

# ── 2. Vault ────────────────────────────────────────────────────────────────
say "2/6 · Ubicando el vault"
VAULT="${NOTEBOOKLM_VAULT_DIR:-${VAULT_DIR:-}}"
if [[ -z "$VAULT" ]]; then
  for guess in "$HOME/vault" "$HOME/Obsidian" "$HOME/Documents/vault" "$HOME/Documents/second-brain" "$HOME/second-brain/second-brain"; do
    [[ -d "$guess" ]] && { VAULT="$guess"; break; }
  done
fi
if [[ -z "$VAULT" || ! -d "$VAULT" ]]; then
  read -r -p "    Ruta absoluta del vault: " VAULT
fi
[[ -d "$VAULT" ]] || fail "no existe: $VAULT"
# Write to the rc file of the shell actually in use — assuming zsh silently
# strands bash users without the env var and with no hint why.
case "$(basename "${SHELL:-}")" in
  zsh)  RC="$HOME/.zshrc" ;;
  bash) RC="$HOME/.bash_profile"; [ -f "$HOME/.bashrc" ] && RC="$HOME/.bashrc" ;;
  fish) RC="$HOME/.config/fish/config.fish" ;;
  *)    RC="" ;;
esac
if [ -z "$RC" ]; then
  say "    ! Shell no reconocido (${SHELL:-desconocido}). Exportá a mano:"
  say "        export NOTEBOOKLM_VAULT_DIR=\"$VAULT\""
elif grep -q "NOTEBOOKLM_VAULT_DIR" "$RC" 2>/dev/null; then
  say "    ✓ NOTEBOOKLM_VAULT_DIR ya estaba en $RC"
else
  mkdir -p "$(dirname "$RC")"
  if [ "$RC" = "$HOME/.config/fish/config.fish" ]; then
    printf '\n# ingest-notebooklm\nset -gx NOTEBOOKLM_VAULT_DIR "%s"\n' "$VAULT" >> "$RC"
  else
    printf '\n# ingest-notebooklm\nexport NOTEBOOKLM_VAULT_DIR="%s"\n' "$VAULT" >> "$RC"
  fi
  say "    ✓ NOTEBOOKLM_VAULT_DIR agregado a $RC"
fi

# ── 3. Directorio de estado ─────────────────────────────────────────────────
say "3/6 · Preparando el directorio de estado"
mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
[[ -f "$STATE_DIR/config.json" ]] || cp "$SCRIPT_DIR/config.example.json" "$STATE_DIR/config.json"
say "    ✓ $STATE_DIR (modo 0700)"

# ── 4. LaunchAgent ──────────────────────────────────────────────────────────
say "4/6 · Generando el LaunchAgent"
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__PYTHON__|$PYBIN|g" \
    -e "s|__SCRIPT_DIR__|$SCRIPT_DIR|g" \
    -e "s|__VAULT_DIR__|$VAULT|g" \
    -e "s|__STATE_DIR__|$STATE_DIR|g" \
    -e "s|__USER_HOME__|$HOME|g" \
    "$SCRIPT_DIR/com.ingest-notebooklm.plist.template" > "$PLIST_DST"
say "    ✓ $PLIST_DST"

# ── 5. Smoke test ───────────────────────────────────────────────────────────
say "5/6 · Chequeo de salud"
NOTEBOOKLM_VAULT_DIR="$VAULT" NOTEBOOKLM_STATE_DIR="$STATE_DIR" \
  python3 "$SCRIPT_DIR/nlm.py" doctor || true

# ── 6. Siguientes pasos ─────────────────────────────────────────────────────
cat <<EOF

6/6 · Listo. Siguientes pasos:

  1) Si el doctor marcó la sesión como vencida, autenticá:
       notebooklm login --fresh --browser chrome
     (no cierres la ventana: se cierra sola al detectar el login)

  2) Probá un espejo corto, sin escribir nada:
       python3 $SCRIPT_DIR/nlm.py --dry-run mirror --max-notebooks 3

  3) Cuando estés conforme, activá el agendamiento (cada 6 h):
       launchctl bootstrap gui/\$(id -u) $PLIST_DST

  4) Ajustes finos: $STATE_DIR/config.json
EOF
