#!/usr/bin/env bash
# install.sh — instalador idempotente de ingest-notebooklm.
#
# Uso interactivo:   ./install.sh
# Uso por un agente: ./install.sh --vault "$HOME/mi-vault" --skill --yes
#
# Con --yes nunca pregunta nada: si falta un dato, falla con un mensaje
# accionable en vez de quedarse esperando en un prompt que nadie va a contestar.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${NOTEBOOKLM_STATE_DIR:-$HOME/.config/ingest-notebooklm}"
# Overridable so a test install cannot clobber a real LaunchAgent.
LAUNCH_AGENTS_DIR="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
PLIST_DST="$LAUNCH_AGENTS_DIR/com.ingest-notebooklm.plist"
SKILL_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills/ingest-notebooklm}"

VAULT="${NOTEBOOKLM_VAULT_DIR:-}"
INSTALL_SKILL=0
NON_INTERACTIVE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --vault) VAULT="${2:-}"; shift 2 ;;
    --vault=*) VAULT="${1#*=}"; shift ;;
    --skill) INSTALL_SKILL=1; shift ;;
    --yes|-y|--non-interactive) NON_INTERACTIVE=1; shift ;;
    -h|--help)
      sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'Opción desconocida: %s\n' "$1" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
fail() { printf '✗ %s\n' "$*" >&2; exit 1; }

# ── 1. Dependencias ─────────────────────────────────────────────────────────
say "1/6 · Verificando dependencias"
command -v python3 >/dev/null || fail "falta python3 (se requiere 3.10+)"
python3 - <<'PY' || fail "se requiere Python 3.10 o superior"
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
if ! command -v notebooklm >/dev/null && [ ! -x "$HOME/.local/bin/notebooklm" ]; then
  fail "falta el CLI notebooklm. Instalalo con:
     uv tool install \"notebooklm-py[browser,mcp,markdown]\""
fi
# The LaunchAgent must run the SAME interpreter validated here: macOS ships
# /usr/bin/python3 at 3.9, so hardcoding it lets the daemon break silently.
PYBIN="$(command -v python3)"
say "    ✓ python3 ($(python3 -V 2>&1 | cut -d' ' -f2)) · notebooklm · $PYBIN"

# ── 2. Vault ────────────────────────────────────────────────────────────────
say "2/6 · Ubicando el vault"
if [ -z "$VAULT" ]; then
  for guess in "$HOME/vault" "$HOME/Obsidian" "$HOME/Documents/vault" \
               "$HOME/Documents/Obsidian" "$HOME/Notes"; do
    [ -d "$guess" ] && { VAULT="$guess"; break; }
  done
fi
if [ -z "$VAULT" ]; then
  if [ "$NON_INTERACTIVE" -eq 1 ]; then
    fail "no se detectó el vault. Pasá --vault \"/ruta/a/tu/vault\""
  fi
  read -r -p "    Ruta absoluta del vault: " VAULT
fi
VAULT="${VAULT/#\~/$HOME}"
[ -d "$VAULT" ] || fail "no existe el directorio: $VAULT"
say "    ✓ $VAULT"

case "$(basename "${SHELL:-}")" in
  zsh)  RC="$HOME/.zshrc" ;;
  bash) RC="$HOME/.bash_profile"; [ -f "$HOME/.bashrc" ] && RC="$HOME/.bashrc" ;;
  fish) RC="$HOME/.config/fish/config.fish" ;;
  *)    RC="" ;;
esac
if [ -z "$RC" ]; then
  say "    ! Shell no reconocido. Exportá a mano:"
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
[ -f "$STATE_DIR/config.json" ] || cp "$SCRIPT_DIR/config.example.json" "$STATE_DIR/config.json"
say "    ✓ $STATE_DIR (modo 0700)"

# ── 4. Agendamiento + skill ─────────────────────────────────────────────────
say "4/6 · Generando el agendamiento"
if [ "$(uname -s)" = "Darwin" ]; then
  mkdir -p "$LAUNCH_AGENTS_DIR"
  sed -e "s|__PYTHON__|$PYBIN|g" -e "s|__SCRIPT_DIR__|$SCRIPT_DIR|g" \
      -e "s|__VAULT_DIR__|$VAULT|g" -e "s|__STATE_DIR__|$STATE_DIR|g" \
      -e "s|__USER_HOME__|$HOME|g" \
      "$SCRIPT_DIR/com.ingest-notebooklm.plist.template" > "$PLIST_DST"
  say "    ✓ $PLIST_DST (no se carga solo: requiere tu autorización)"
else
  say "    ! $(uname -s): launchd es solo macOS. Agendalo con cron/systemd:"
  say "        $PYBIN $SCRIPT_DIR/nlm.py mirror"
fi

if [ "$INSTALL_SKILL" -eq 1 ]; then
  mkdir -p "$SKILL_DIR"
  sed -e "s|__SCRIPT_DIR__|$SCRIPT_DIR|g" -e "s|__VAULT_DIR__|$VAULT|g" \
      "$SCRIPT_DIR/SKILL.template.md" > "$SKILL_DIR/SKILL.md"
  say "    ✓ skill instalado en $SKILL_DIR"
fi

# ── 5. Smoke test ───────────────────────────────────────────────────────────
say "5/6 · Chequeo de salud"
NOTEBOOKLM_VAULT_DIR="$VAULT" NOTEBOOKLM_STATE_DIR="$STATE_DIR" \
  "$PYBIN" "$SCRIPT_DIR/nlm.py" doctor || true

# ── 6. Siguientes pasos ─────────────────────────────────────────────────────
cat <<EOF

6/6 · Listo. Siguientes pasos:

  1) Si el doctor marcó la sesión como vencida, autenticá:
       notebooklm login --fresh --browser chrome
     (no cierres la ventana: se cierra sola al detectar el login)

  2) Probá un espejo corto, sin escribir nada:
       $PYBIN $SCRIPT_DIR/nlm.py --dry-run mirror --max-notebooks 3

  3) Para agendarlo cada 6 h (macOS):
       launchctl bootstrap gui/\$(id -u) $PLIST_DST

  4) Ajustes finos: $STATE_DIR/config.json
EOF
