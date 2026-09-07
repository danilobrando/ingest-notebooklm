# Revisiones de panel — v0.1.0 (2026-09-07)

Dos paneles sobre el código real, no sobre el diseño. Cada hallazgo se anota con
cómo se detectaría en producción, porque un defecto que no se puede observar es
peor que uno ruidoso.

---

## Panel 1 — Endurecimiento (5 lentes)

### Charity Majors · observabilidad

| # | Hallazgo | Estado |
|---|---|---|
| C1 | `pipeline.jsonl` usaba hora local mientras el cursor usaba UTC: comparar un evento con un cursor exigía traducir zonas mentalmente. | ✅ P0 — todo en UTC ISO-8601 |
| C2 | La métrica `skipped` mezclaba "sin cambios" con "en cuarentena": dos estados con causas opuestas bajo un mismo número. | ⏳ P1 |

### Aaron Parecki · ciclo de vida de la sesión

| # | Hallazgo | Estado |
|---|---|---|
| A1 | El espejo empezaba a trabajar sin verificar la sesión y moría a mitad, dejando trabajo parcial. Verificar cuesta ~2 s. | ✅ P0 — preflight de sesión |
| A2 | No hay refresco posible: la auth es un perfil de navegador. Limitación estructural, no defecto. | 📌 Documentado |

### Simon Willison · UX pragmática

| # | Hallazgo | Estado |
|---|---|---|
| S1 | **`config.json` no se leía nunca.** El instalador lo copiaba y le decía al usuario "ajustes finos acá"; editarlo no hacía absolutamente nada. | ✅ P0 — env > config.json > default |
| S2 | El patrón `preparing` del catálogo de errores matcheaba cualquier texto que contuviera esa palabra; `not found` capturaba errores ajenos y los diagnosticaba como "notebook inexistente". | ✅ P0 — patrones anclados |
| S3 | Un `config.json` malformado caía a defaults en silencio. | ✅ P0 — `doctor` lo reporta |
| S4 | No había `--version` de nivel superior, solo el subcomando. | ✅ |

### John Allspaw · modos de falla

| # | Hallazgo | Estado |
|---|---|---|
| **L1** | **Inanición.** El orden era `modified_at` descendente: con un presupuesto por corrida menor al ritmo de edición, los notebooks viejos nunca llegaban al frente de la cola. El espejo jamás habría convergido. | ✅ P0 — nunca-espejados primero, luego el más antiguo |
| **L2** | **El tope de inventario truncaba.** `MAX_PAGES * 100` = 400 con **397 notebooks reales**: a tres de empezar a excluir notebooks del espejo de forma permanente. Y el CLI devuelve todo en una respuesta, así que el tope no paginaba nada — solo cortaba. | ✅ P0 — umbral de aviso, nunca truncado |
| L3 | **Cuarentena permanente.** Tres fallos —incluidos tres hipos de red— exiliaban un notebook para siempre; solo `doctor` lo mencionaba. | ✅ P1 — las marcas caducan a los 7 días |
| L4 | `acquire_lock` recursaba sin cota al robar un lock añejo: dos procesos en carrera podían recursar uno contra otro. | ✅ P0 — acotado a 3 intentos |
| L5 | Una fuente borrada en NotebookLM deja su archivo huérfano en el vault para siempre. No hay reconciliación de borrados. | ⏳ P1 |

### Filippo Valsorda · credenciales y PII

| # | Hallazgo | Estado |
|---|---|---|
| **F1** | **Secretos hacia los logs.** Los errores guardaban `stdout+stderr` del CLI en crudo. Ese texto trae URLs de OAuth, valores de cookie y tokens — y `pipeline.jsonl` es un archivo plano y permanente. | ✅ P0 — `redact()` en cada camino a log, audit y terminal |
| F2 | El directorio de estado ya estaba en 0700 y el espejo cae bajo `External Inputs/`, gitignoreado en el vault de destino. | ✅ Verificado |

---

## Panel 2 — Productización (3 lentes)

### Adam Wiggins · instalar siendo un extraño

| # | Hallazgo | Estado |
|---|---|---|
| W1 | `install.sh` escribía en `~/.zshrc` sin mirar el shell: un usuario de bash o fish quedaba sin la variable y sin pista de por qué. | ✅ Corregido — zsh/bash/fish, con salida explícita si no reconoce |
| W2 | Nada fijaba la versión de `notebooklm-py`, la dependencia cuyo JSON parseamos. Un cambio de esquema aguas arriba es la forma más probable de que esto se rompa. | ✅ `doctor` avisa si baja de 0.8.1 |

### Mitchell Hashimoto · supuestos tácitos

| # | Hallazgo | Estado |
|---|---|---|
| H1 | La subcarpeta del espejo estaba quemada en el código: se asumía que todo el mundo organiza su vault igual que el autor. | ✅ `NOTEBOOKLM_MIRROR_SUBDIR` / `mirror_subdir` |
| H2 | `doctor` consultaba `launchctl` en cualquier plataforma y fallaba sin explicar en Linux. | ✅ Consciente de plataforma |

### Mike McQuaid · sostenibilidad de mantenedor solo

| # | Hallazgo | Estado |
|---|---|---|
| M1 | Sin tests: cada cambio se validaba a mano. | ✅ `smoke-test.sh`, 7 chequeos |
| M2 | El README no mostraba qué se ve al correrlo. | ✅ Salida de ejemplo |
| M3 | CHANGELOG, CONTRIBUTING, LICENSE, semver y aviso alpha ya estaban. | ✅ |

---

## El defecto que ningún panel encontró

Al aplicar el P0 de Filippo escribí una expresión regular con un paréntesis
desbalanceado. `python3 -m py_compile` pasó en verde: **no ejecuta `re.compile`**,
así que un módulo que no puede ni importarse se veía sano.

Lo cazó ejecutar `import nlm`, no leer el código. Por eso el primer paso de
`smoke-test.sh` es importar el módulo — la verificación más barata que existe
y la que este proyecto ya demostró necesitar.

## Pendientes reconocidos (P1)

- **C2** — separar "sin cambios" de "en cuarentena" en las métricas.
- **L5** — reconciliar borrados: hoy los archivos huérfanos se acumulan.
