---
name: ingest-notebooklm
description: Conector bidireccional entre NotebookLM (Gemini Notebook) y el vault de Obsidian. Subcomandos - mirror (NotebookLM → vault, incremental, con texto completo), push (vault → NotebookLM, auditado, con --dry-run), doctor (9 chequeos de solo lectura), fix (auto-reparación idempotente), version. **AUTO-RECOVERY TRIGGER: cuando el usuario reporte CUALQUIER problema relacionado con NotebookLM, notebook, notebooks, Gemini Notebook, el espejo de notebooks, la sincronización de notebooks — incluyendo frases vagas como "notebooklm no funciona", "no me sincroniza", "no veo mis notebooks", "no está bajando", "está raro lo del notebook", "no puedo subir", "error con notebooklm", "algo no anda con los notebooks" — este skill DEBE invocarse PRIMERO y el orquestador DEBE correr en silencio `python3 ~/dev/ingest-notebooklm/nlm.py fix --quiet` ANTES de responderle al usuario. El agente interpreta la salida y o bien confirma la auto-reparación, o bien acompaña al usuario por los pasos manuales de forma conversacional.** El mirror escribe en `External Inputs/NotebookLM/` del vault y es idempotente por notebook. Los subcomandos de escritura soportan --dry-run y dejan rastro en audit.jsonl.
---

# ingest-notebooklm

Espeja notebooks de NotebookLM al vault y sube notas del vault como fuentes.

## AUTO-RECOVERY POLICY

Cuando Danny reporte cualquier síntoma relacionado con NotebookLM, **antes de responder**:

```bash
python3 ~/dev/ingest-notebooklm/nlm.py fix --quiet
```

Luego interpretá el resultado y respondé en lenguaje natural. Nunca le muestres la salida cruda.

| Salida | Qué significa | Qué le decís |
|---|---|---|
| exit 0, sin stderr | Estaba todo bien | "Revisé y está sano. ¿Qué síntoma viste?" |
| exit 0, con reparaciones | Se arregló solo | "Ya lo arreglé — era [causa en palabras simples]." |
| exit 1 | Necesita acción humana | Guialo paso a paso, uno a la vez |

## Frases gatillo

**Español:** "notebooklm no funciona", "no me sincroniza", "no veo mis notebooks", "no está bajando", "está raro lo del notebook", "no puedo subir", "error con notebooklm", "algo no anda con los notebooks", "se quedó pegado el espejo".
**Inglés:** "notebooklm is broken", "notebooks aren't syncing", "mirror stuck", "can't push to notebook".

## El caso que NO se auto-repara

La autenticación es **un perfil de navegador**, no un token OAuth. `fix` **no puede** re-autenticar solo. Cuando la sesión venza, guialo así:

> "Se venció la sesión de Google de NotebookLM. Corré esto y entrá con tu cuenta:
> `notebooklm login --fresh --browser chrome`
> Importante: no cierres la ventana — se cierra sola cuando detecta el login."

Si falla con *"browser window was closed"*: pedile que cierre Chrome por completo y reintente.

## Cómo traducir los pasos manuales

**Bien:** "Se venció la sesión de Google. Corré este comando y entrá con tu cuenta: `notebooklm login --fresh --browser chrome`"
**Mal:** "El check de auth falló: SID cookie inválida, storage_state.json expirado."

## Qué NO hacer

- No le muestres la salida cruda del `fix` ni trazas de error.
- No le preguntes "¿qué comando corro?" — vos ya sabés cuál.
- No menciones términos internos: token, cookie, OAuth, sentinel, cursor, lock.
- No corras `mirror --force` sobre los 391 notebooks sin avisarle: son horas de llamadas y sube el riesgo de bloqueo de la cuenta.

## Después de una recuperación exitosa

Ofrecele rehacer lo que estaba intentando: "Ya quedó. ¿Querés que corra el espejo ahora?"

## Escalamiento

Solo después de: (1) corriste `fix`, (2) él siguió los pasos manuales, (3) sigue roto. Ahí sí mostrá el detalle técnico y `tail ~/.config/ingest-notebooklm/pipeline.jsonl`.

## Uso normal

```bash
# Espejo incremental (lo que corre el LaunchAgent cada 6 h)
python3 ~/dev/ingest-notebooklm/nlm.py mirror

# Ensayo sin escribir nada
python3 ~/dev/ingest-notebooklm/nlm.py --dry-run mirror --max-notebooks 5

# Subir notas del vault a un notebook (siempre ensayar primero)
python3 ~/dev/ingest-notebooklm/nlm.py --dry-run push --notebook <id> --folder "<carpeta>" --glob '*.md'

# Salud
python3 ~/dev/ingest-notebooklm/nlm.py doctor
```
