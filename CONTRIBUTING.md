# Contribuir

## Alcance

Esto es una herramienta personal publicada por si le sirve a alguien más. Supone:

- **macOS.** El agendamiento es LaunchAgent; no hay soporte de systemd ni Windows.
- **Un vault estilo Obsidian**, con `External Inputs/` como zona de ingesta.
- **Mensajes en español.** El código y los comentarios, en inglés.
- **Un solo perfil y una sola cuenta.** No hay multi-tenancy.
- **APIs no documentadas de Google** vía `notebooklm-py`. Pueden romperse sin aviso.

## Dentro de alcance

Correcciones de fallas, mejoras de resiliencia, mejores mensajes de error,
chequeos adicionales en `doctor`, cobertura de más tipos de fuente.

## Fuera de alcance

Soporte de otros sistemas operativos, otros gestores de notas, GUI, interfaz
multiusuario, y cualquier cosa que suba el ritmo de llamadas a Google por
defecto: los defaults conservadores son una decisión, no un descuido.

## Antes de un PR

1. `python3 -m py_compile nlm.py`
2. `python3 nlm.py doctor` en verde
3. `python3 nlm.py --dry-run mirror --max-notebooks 2` sin errores
4. Toda operación de escritura nueva debe honrar `--dry-run` y escribir en `audit.jsonl`.
