# ingest-notebooklm

> **Esto no es un cliente de NotebookLM.** Es una capa operativa sobre
> [`notebooklm-py`](https://github.com/teng-lin/notebooklm-py), de Teng Lin,
> que hace todo el trabajo pesado. Ver [NOTICE](NOTICE).

Tus notebooks de NotebookLM, espejados a tu vault de Obsidian — y tus notas del vault, subidas como fuentes.

## Requisitos

- macOS (probado en Darwin 25) · Python 3.10+ · bash
- El CLI [`notebooklm-py`](https://github.com/teng-lin/notebooklm-py) con los extras `browser`, `mcp` y `markdown`
- Un vault de Obsidian (o cualquier carpeta de markdown)

## Instalación (3 comandos)

```bash
uv tool install "notebooklm-py[browser,mcp,markdown]"
notebooklm login --fresh --browser chrome
./install.sh --vault "/ruta/a/tu/vault" --skill
```

Para instalación desatendida (por un agente), agregá `--yes`: nunca abre un
prompt, y si falta un dato falla con un mensaje accionable.

## Cómo se ve

```
$ nlm.py doctor
ingest-notebooklm doctor
──────────────────────────────────────────────────────────────
✓ env              PASS  /Users/tu-usuario/vault
✓ state-dir        PASS  ~/.config/ingest-notebooklm modo 0700
✓ notebooklm-bin   PASS  ~/.local/bin/notebooklm (v0.8.1)
✓ auth             PASS  sesión válida y verificada
✓ vault-writable   PASS  vault/External Inputs/NotebookLM
✓ log-writable     PASS  ~/.config/ingest-notebooklm/pipeline.jsonl
✓ sentinel-age     PASS  último espejo hace 0.4 h
✓ failure-rate     PASS  0/11 corridas con fallo
✓ scheduler        PASS  LaunchAgent cargado
──────────────────────────────────────────────────────────────
9/9 en verde

$ nlm.py mirror
📚 397 notebooks · 392 con cambios · procesando hasta 25
  ✓ Software 3.0: El Inglés como Nuevo Lenguaje — 1 fuentes, 1 textos
  ...
⏭️  Quedan 367 notebooks pendientes para la próxima corrida.
✅ 25 espejados · 31 fuentes · 29 textos · 0 fallos · 284.1s
```

## Qué hace

| Comando | Qué hace |
|---|---|
| `mirror` | NotebookLM → vault. Incremental, reanudable, con texto completo de cada fuente. |
| `push` | vault → NotebookLM. Sube archivos como fuentes; auditado; `--dry-run`. |
| `doctor` | 9 chequeos de solo lectura; cada fallo imprime una acción concreta. |
| `fix` | Repara lo reparable, guía lo demás. `--quiet` para hooks. |

El espejo escribe en `External Inputs/NotebookLM/` (una nota por notebook, con
frontmatter y wikilinks) y en `External Inputs/NotebookLM/fulltext/<notebook>/`
(el texto completo de cada fuente).

## Qué NO hace

- **No se auto-autentica.** La sesión es un perfil de navegador, no un token OAuth.
  Cuando venza hay que correr `notebooklm login --fresh --browser chrome` a mano.
- **No es la fuente de verdad.** El espejo es regenerable; lo canónico vive en NotebookLM.
- **No borra nada** en NotebookLM.
- **No barre los notebooks de una sola vez.** Procesa de a tandas (`--max-notebooks`,
  25 por defecto) y deja el resto para la corrida siguiente. Con cientos de
  notebooks, la convergencia lleva varias corridas — a propósito.
- **No es multiusuario ni multiplataforma.** Un perfil, una máquina, macOS
  (en otras plataformas corre a mano; el agendamiento es launchd).
- **No reconcilia borrados.** Si borrás una fuente en NotebookLM, su archivo
  de texto completo queda huérfano en el vault. Conocido, ver `PANEL-REVIEW.md`.

## Personalización

| Variable | Default | Para qué |
|---|---|---|
| `NOTEBOOKLM_VAULT_DIR` | — (obligatoria) | Raíz del vault |
| `NOTEBOOKLM_STATE_DIR` | `~/.config/ingest-notebooklm` | Estado, logs, cursor |
| `NOTEBOOKLM_MAX_NOTEBOOKS` | `25` | Notebooks por corrida |
| `NOTEBOOKLM_RATE_LIMIT` | `1.5` | Segundos entre llamadas |
| `NOTEBOOKLM_MAX_PAGES` | `4` | Tope de paginación |
| `NOTEBOOKLM_CALL_TIMEOUT` | `180` | Timeout por llamada |
| `NOTEBOOKLM_PROFILE` | `default` | Perfil de `notebooklm-py` |
| `NOTEBOOKLM_MIRROR_SUBDIR` | `External Inputs/NotebookLM` | Subcarpeta del espejo |
| `NOTEBOOKLM_FULLTEXT` | `true` | Bajar el texto completo de cada fuente |
| `NOTEBOOKLM_STRIKE_DECAY_DAYS` | `7` | Días hasta reintentar un notebook en cuarentena |

Las mismas claves viven en `~/.config/ingest-notebooklm/config.json`
(`max_notebooks_per_run`, `rate_limit_seconds`, `mirror_subdir`, …).
**Precedencia: variable de entorno > `config.json` > default.**

## Desarrollo

```bash
./smoke-test.sh
```

Siete chequeos que no escriben nada. El primero es `import nlm`, porque
`py_compile` no ejecuta `re.compile` y deja pasar módulos que ni cargan.

## Advertencia

Se apoya en **APIs no documentadas de Google** a través de `notebooklm-py`. Pueden
romperse sin aviso, y el uso automatizado intenso puede acarrear bloqueo de la
cuenta. Los defaults son conservadores por eso. Si la cuenta que usás sostiene
otras cosas (correo, Drive), tenelo presente antes de subir el ritmo.

## Créditos

El acceso a NotebookLM —ingeniería inversa de las APIs internas de Google,
manejo de sesión, CLI y servidor MCP— es obra de
**[Teng Lin](https://github.com/teng-lin/notebooklm-py)** y son ~114.000
líneas. Este repositorio son ~1.300: el espejo hacia el vault, los chequeos
de salud, la auto-reparación, el registro de auditoría y el agendamiento.

Si esto te sirve, la estrella va a [notebooklm-py](https://github.com/teng-lin/notebooklm-py).

## Para agentes

¿Sos un agente de IA instalando esto? Leé [AGENTS.md](AGENTS.md): instalación
desatendida, el paso que requiere una persona, y las reglas al operarlo.

## Licencia

MIT — ver [LICENSE](LICENSE) y [NOTICE](NOTICE).
