# Changelog

Formato: [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) · versionado [SemVer](https://semver.org/lang/es/).

> **Alpha.** Antes de v1.0 cualquier release menor puede romper compatibilidad.
> Se apoya en APIs no documentadas de Google que pueden cambiar sin aviso.

## [0.1.0] — 2026-08-25

### Agregado
- `mirror`: espejo incremental NotebookLM → vault con texto completo, cursor
  reanudable por notebook y cuarentena de notebooks que fallan repetidamente.
- `push`: vault → NotebookLM con `--dry-run` y rastro en `audit.jsonl`.
- `doctor`: 9 chequeos de solo lectura, cada fallo con su siguiente acción.
- `fix`: reparación idempotente; declara explícitamente que no puede re-autenticar.
- Catálogo de 8 errores conocidos → descripción humana + acción concreta.
- Lock `O_EXCL` con robo a las 2 h, sentinels, `pipeline.jsonl`, `audit.jsonl`.
- `install.sh` de 6 pasos y plantilla de LaunchAgent (cada 6 h).
- `SKILL.template.md` con política de auto-recuperación.

### Endurecimiento (paneles de revisión, ver `PANEL-REVIEW.md`)
- **Redacción de credenciales** en todo lo que llega a un log, al audit o a la
  terminal: la salida cruda del CLI trae URLs de OAuth y valores de cookie.
- **Orden anti-inanición**: nunca-espejados primero, después el más antiguo. El
  orden por `modified_at` descendente jamás habría convergido.
- **El tope de inventario ya no trunca**: con 397 notebooks el tope de 400
  estaba a punto de excluir notebooks del espejo en silencio.
- **`config.json` se lee de verdad** (env > config > default). Antes el
  instalador lo copiaba y editarlo no hacía nada.
- Preflight de sesión antes de cada espejo; cuarentena que caduca a los 7 días;
  lock acotado; patrones del catálogo anclados; todo en UTC.
- `install.sh` detecta el shell (zsh/bash/fish) en vez de asumir zsh.
- Subcarpeta del espejo configurable; `doctor` consciente de plataforma y de la
  versión de `notebooklm-py`; `--version` de nivel superior; `smoke-test.sh`.

### Notas de esta versión
- El espejo compara contra el instante del último espejo, no contra `modified_at`
  a secas: abrir un notebook actualiza su `modified_at`, así que un cursor ingenuo
  nunca convergería.
- `push` verificado end-to-end el 2026-09-07 (subida real confirmada del lado
  de NotebookLM).
- `doctor` verifica la sesión con `auth check --test`: sin ese flag el CLI
  reporta "valid" leyendo solo la fecha de la cookie, y esa fecha no refleja la
  validez real (una sesión que declaraba 365 días murió en 13).
