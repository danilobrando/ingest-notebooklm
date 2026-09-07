# Instrucciones para agentes

Este archivo es para un agente de IA que instala o mantiene este conector en
la máquina de una persona. Escrito para ejecutarse sin supervisión: cada paso
es idempotente, verificable y falla con una acción concreta.

## Qué es

Una capa operativa sobre [`notebooklm-py`](https://github.com/teng-lin/notebooklm-py).
Espeja notebooks de NotebookLM a un vault de Obsidian como markdown y sube
notas del vault como fuentes. Ver `NOTICE` para la atribución.

## Instalación desatendida

```bash
uv tool install "notebooklm-py[browser,mcp,markdown]"
git clone https://github.com/danilobrando/ingest-notebooklm.git ~/dev/ingest-notebooklm
cd ~/dev/ingest-notebooklm && ./install.sh --vault "<RUTA_DEL_VAULT>" --skill --yes
```

`--yes` garantiza que nunca se abra un prompt interactivo: si falta un dato,
sale con código distinto de cero y un mensaje accionable.

**El extra `markdown` no es opcional.** Sin él, el espejo del texto completo
falla en cada fuente y solo lo vas a ver en `pipeline.jsonl`, no en pantalla.

## El paso que NO podés automatizar

La autenticación es un **perfil de navegador**, no OAuth. Requiere que una
persona entre con su cuenta en una ventana real:

```bash
notebooklm login --fresh --browser chrome
```

No cierres la ventana: se cierra sola al detectar el login. Ningún script
puede hacer esto por ella, y ningún script puede renovarlo después.
**La sesión dura ~2 semanas**, no lo que declare la cookie.

## Verificación (corré esto, no asumas)

```bash
python3 nlm.py doctor        # 9 chequeos; cada fallo imprime su acción
./smoke-test.sh              # 7 chequeos, no escribe nada
python3 nlm.py --dry-run mirror --max-notebooks 3
```

`doctor` sale 0 si nada está en FAIL, 1 si algo lo está. Un WARN de
`scheduler` es normal hasta que la persona autorice el agendamiento.

## Reglas al operar esto

1. **Nunca corras `mirror --force` sobre una biblioteca grande** sin avisar:
   son cientos de llamadas a Google y sube el riesgo de bloqueo de la cuenta.
2. **Toda escritura tiene `--dry-run`.** Usalo primero, siempre.
3. **No subas el ritmo** (`NOTEBOOKLM_RATE_LIMIT`) sin que la persona lo pida.
   Los defaults son conservadores a propósito: la cuenta en juego suele ser
   la misma que sostiene su correo y su Drive.
4. **El agendamiento no se instala solo.** `install.sh` genera el LaunchAgent
   pero no lo carga; eso exige autorización explícita:
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ingest-notebooklm.plist`
5. **Si algo falla, corré `nlm.py fix --quiet` antes de responder.** El skill
   instalado con `--skill` lleva la política completa.

## Al modificar el código

- `python3 -c "import nlm"` **antes** que `py_compile`: py_compile no ejecuta
  `re.compile` y deja pasar módulos que no cargan.
- `./smoke-test.sh` debe quedar en verde.
- Toda operación de escritura nueva honra `--dry-run` y escribe en `audit.jsonl`.
- Nada de rutas absolutas de usuario en el repositorio.

## Límites que importan

| Concepto | Plan gratuito | Pro |
|---|---|---|
| Notebooks por cuenta | 100 | 500 |
| Fuentes por notebook | 50 | 300 |
| Audio overviews por día | 3 | 20 |

El tope de notebooks es de **inventario**, no mensual: borrar libera cupo.
`server_info(include_account=True)` del MCP devuelve los límites reales.
