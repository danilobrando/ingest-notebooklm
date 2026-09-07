#!/usr/bin/env python3
"""ingest-notebooklm — bidirectional NotebookLM <-> Obsidian vault connector.

Subcommands:
  mirror   NotebookLM -> vault (read-only, incremental, resumable)
  push     vault -> NotebookLM (write, --dry-run aware, audited)
  doctor   8 read-only health checks
  fix      idempotent auto-repair + guided manual steps
  version  print version

State lives in $NOTEBOOKLM_STATE_DIR (default ~/.config/ingest-notebooklm), mode 0700.
Auth is a browser profile owned by the `notebooklm` CLI; this connector never
handles cookies directly and cannot re-authenticate on its own.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"
# The notebooklm-py release this connector was built and tested against.
# Its JSON output shape is what mirror/push parse.
TESTED_NLM_MIN = (0, 8, 1)

# ---------------------------------------------------------------- config (env)
STATE_DIR = Path(os.environ.get(
    "NOTEBOOKLM_STATE_DIR", str(Path.home() / ".config" / "ingest-notebooklm")))
VAULT_DIR = Path(os.environ.get("NOTEBOOKLM_VAULT_DIR", "")).expanduser()
NLM_BIN = os.environ.get("NOTEBOOKLM_BIN", str(Path.home() / ".local/bin/notebooklm"))
PROFILE = os.environ.get("NOTEBOOKLM_PROFILE", "default")
CONFIG_PATH_EARLY = STATE_DIR / "config.json"



def _read_config_file() -> dict:
    """config.json in the state dir. Env vars win; this is the middle layer."""
    path = STATE_DIR / "config.json"
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        # A malformed config must not stop a scheduled run; doctor reports it.
        pass
    return {}


_CFG = _read_config_file()


def _setting(env_key: str, cfg_key: str, default, cast):
    """Precedence: environment > config.json > built-in default."""
    raw = os.environ.get(env_key)
    if raw is None and cfg_key in _CFG:
        raw = _CFG[cfg_key]
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


MAX_NOTEBOOKS = _setting("NOTEBOOKLM_MAX_NOTEBOOKS", "max_notebooks_per_run", 25, int)
RATE_LIMIT = _setting("NOTEBOOKLM_RATE_LIMIT", "rate_limit_seconds", 1.5, float)
MAX_PAGES = _setting("NOTEBOOKLM_MAX_PAGES", "max_pages", 4, int)
CALL_TIMEOUT = _setting("NOTEBOOKLM_CALL_TIMEOUT", "call_timeout_seconds", 180, int)
MIRROR_SUBDIR = _setting("NOTEBOOKLM_MIRROR_SUBDIR", "mirror_subdir",
                         "External Inputs/NotebookLM", str)
MIRROR_FULLTEXT = _setting("NOTEBOOKLM_FULLTEXT", "mirror_fulltext", True,
                           lambda v: str(v).lower() not in ("0", "false", "no"))
STRIKE_DECAY_DAYS = _setting("NOTEBOOKLM_STRIKE_DECAY_DAYS", "strike_decay_days", 7, int)

CONFIG_PATH = STATE_DIR / "config.json"
LOG_PATH = STATE_DIR / "pipeline.jsonl"
AUDIT_PATH = STATE_DIR / "audit.jsonl"
LOCK_PATH = STATE_DIR / "pipeline.lock"
CURSOR_PATH = STATE_DIR / "mirror-cursor.json"
SENTINEL_SUCCESS = STATE_DIR / "pipeline.last-success"
SENTINEL_MIRROR = STATE_DIR / "pipeline.last-mirror"

LOCK_STALE_AGE = 2 * 3600      # steal a lock older than 2h (crashed run)
COOKIE_WARN_DAYS = 30
MIRROR_STALE_H = 48            # doctor FAIL threshold for mirror sentinel
POISON_MAX_STRIKES = 3         # no-progress strikes before aborting the loop

QUIET = False
DRY_RUN = False


def out(msg: str = "") -> None:
    if not QUIET:
        print(msg)


def err(msg: str) -> None:
    print(redact(msg), file=sys.stderr)


# ------------------------------------------------------------- error catalog
# Known failure signatures -> (human description, concrete NEXT ACTION).
ERROR_CATALOG: list[tuple[str, str, str]] = [
    (r"storage file not found|not authenticated|run 'notebooklm login'",
     "La sesión de NotebookLM no existe todavía.",
     "Corré: notebooklm login --fresh --browser chrome"),
    (r"401|unauthorized|sid cookie|session expired|invalid credentials",
     "La sesión de Google expiró o fue invalidada.",
     "Corré: notebooklm login --fresh --browser chrome  (no cierres la ventana "
     "hasta que el login se detecte solo)"),
    (r"429|rate.?limit|too many requests|quota exceeded",
     "Google está limitando la cantidad de peticiones.",
     "Esperá ~15 min y subí NOTEBOOKLM_RATE_LIMIT (ej. 4.0) antes de reintentar."),
    (r"no such notebook|notebook not found|\b404\b",
     "El notebook indicado no existe o no es accesible con esta cuenta.",
     "Verificá el ID con: notebooklm list"),
    (r"source limit|too many sources|source quota",
     "El notebook llegó al tope de fuentes que permite NotebookLM.",
     "Borrá fuentes obsoletas o creá un notebook nuevo para el resto."),
    (r"stuck in preparing|status[\s:=]+preparing",
     "Hay fuentes huérfanas atascadas en estado 'preparing' (ocupan cuota).",
     "Revisá con: notebooklm source list --status preparing"),
    (r"timed out|timeout",
     "NotebookLM no respondió a tiempo.",
     "Reintentá; si persiste subí NOTEBOOKLM_CALL_TIMEOUT."),
    (r"browser window was closed",
     "La ventana del navegador se cerró antes de completar el login.",
     "Cerrá Google Chrome por completo y corré: notebooklm login --fresh --browser chrome"),
]


def explain(text: str) -> tuple[str, str] | None:
    low = (text or "").lower()
    for pattern, desc, action in ERROR_CATALOG:
        if re.search(pattern, low):
            return desc, action
    return None


class AuthExpired(RuntimeError):
    """Session died mid-run. Cannot self-heal: auth is a browser profile."""


class RateLimited(RuntimeError):
    pass


# --------------------------------------------------------------- state helpers
def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)


def log_event(event: str, status: str, **fields) -> None:
    """Append one structured event. Never raises — logging must not kill a run."""
    try:
        ensure_state_dir()
        fields = {k: (redact(v) if isinstance(v, str) else v)
                  for k, v in fields.items()}
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event,
               "status": status, "pid": os.getpid(), **fields}
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def audit(action: str, **fields) -> None:
    """Append one write-action record. Forensic trail for everything we mutate."""
    try:
        ensure_state_dir()
        fields = {k: (redact(v) if isinstance(v, str) else v)
                  for k, v in fields.items()}
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "action": action,
               "dry_run": DRY_RUN, "pid": os.getpid(), **fields}
        with AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# Anything the notebooklm CLI prints can carry an OAuth URL, a token or a
# cookie value. Every string that reaches a log file, an audit record or the
# terminal passes through here first.
_REDACTIONS = [
    # Any Google auth URL — these carry codes and tokens in the query string.
    (re.compile(r"https://accounts\.google\.com/\S{20,}", re.I), "<redacted-url>"),
    # Sensitive query parameters anywhere else.
    (re.compile(r"([?&](?:access_token|id_token|code|oauth_token|state)=)[^\s&]+", re.I),
     r"\1<redacted>"),
    # name=value / name: value for credential-shaped names.
    (re.compile(r"\b(cookie|token|password|secret|authorization|api[_-]?key)"
                r"(s?\s*[=:]\s*)(\S+)", re.I), r"\1\2<redacted>"),
    # Bare high-entropy blobs (cookie values, bearer tokens). UUIDs are 36
    # chars, so a 40-char floor keeps notebook and source ids readable.
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), "<redacted>"),
]


def redact(text) -> str:
    """Strip credential-shaped substrings. Applied to every logged string."""
    out_s = str(text)
    for pattern, repl in _REDACTIONS:
        out_s = pattern.sub(repl, out_s)
    return out_s


def sha8(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:8]


def load_cursor() -> dict:
    if CURSOR_PATH.exists():
        try:
            return json.loads(CURSOR_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cursor(cur: dict) -> None:
    ensure_state_dir()
    tmp = CURSOR_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CURSOR_PATH)


# ------------------------------------------------------- lock (O_EXCL + steal)
_lock_held = False


def acquire_lock(_attempt: int = 0) -> bool:
    """Bounded: two racing processes must not recurse into each other."""
    global _lock_held
    if _attempt > 3:
        log_event("lock", "contended", attempts=_attempt)
        return False
    ensure_state_dir()
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        _lock_held = True
        return True
    except FileExistsError:
        try:
            age = time.time() - LOCK_PATH.stat().st_mtime
        except FileNotFoundError:
            return acquire_lock(_attempt + 1)
        if age > LOCK_STALE_AGE:
            log_event("lock", "stolen", age_s=int(age))
            LOCK_PATH.unlink(missing_ok=True)
            return acquire_lock(_attempt + 1)
        return False


def release_lock() -> None:
    global _lock_held
    if _lock_held:
        LOCK_PATH.unlink(missing_ok=True)
        _lock_held = False


# ----------------------------------------------------------- notebooklm bridge
def nlm(*args: str, timeout: int | None = None, check: bool = True) -> str:
    """Run the notebooklm CLI once. Rate-limited, error-classified.

    Auth is a browser profile we do not own, so a mid-run expiry cannot be
    refreshed here — it raises AuthExpired and the caller stops cleanly with
    partial progress persisted.
    """
    cmd = [NLM_BIN, "-p", PROFILE, *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout or CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out: {' '.join(args[:3])}")
    finally:
        time.sleep(RATE_LIMIT)

    if proc.returncode != 0:
        blob = f"{proc.stdout}\n{proc.stderr}".strip()
        low = blob.lower()
        if re.search(r"401|unauthorized|session expired|storage file not found|sid cookie", low):
            raise AuthExpired(blob[:400])
        if re.search(r"429|rate.?limit|too many requests", low):
            raise RateLimited(blob[:400])
        if check:
            raise RuntimeError(blob[:400] or f"exit {proc.returncode}")
    return proc.stdout


def nlm_json(*args: str, timeout: int | None = None):
    raw = nlm(*args, "--json", timeout=timeout)
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Some subcommands emit a human preamble before the JSON body.
        start = min((i for i in (raw.find("{"), raw.find("[")) if i != -1), default=-1)
        if start != -1:
            try:
                return json.loads(raw[start:])
            except json.JSONDecodeError:
                pass
        raise RuntimeError(f"respuesta no-JSON de notebooklm: {raw[:200]}")


def call_with_backoff(fn, *a, **kw):
    """Retry a bridged call on rate limiting with exponential backoff."""
    delay = max(RATE_LIMIT * 4, 5.0)
    for attempt in range(3):
        try:
            return fn(*a, **kw)
        except RateLimited:
            if attempt == 2:
                raise
            log_event("backoff", "warn", attempt=attempt + 1, sleep_s=round(delay, 1))
            out(f"  ⏳ rate limit — esperando {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
    return None


# ------------------------------------------------------------------ vault i/o
def slugify(text: str, maxlen: int = 60) -> str:
    text = (text or "sin-titulo").strip()
    text = re.sub(r"[\\/:*?\"<>|#^\[\]]", "-", text)
    text = re.sub(r"\s+", " ", text).strip(" .-")
    return (text[:maxlen].rstrip(" .-") or "sin-titulo")


def mirror_root() -> Path:
    if not VAULT_DIR or str(VAULT_DIR) == ".":
        raise RuntimeError(
            "NOTEBOOKLM_VAULT_DIR no está definido. NEXT ACTION: exportalo en "
            "~/.zshrc o corré install.sh")
    return VAULT_DIR.joinpath(*Path(MIRROR_SUBDIR).parts)


def strip_fulltext_preamble(raw: str) -> str:
    """The CLI prefixes fulltext with Source/Title/Characters/Content: headers.

    Only the body belongs in the vault file — the metadata already lives in the
    note's frontmatter. Falls back to the raw text if the marker is absent.
    """
    marker = "\nContent:\n"
    idx = raw.find(marker)
    if idx != -1:
        return raw[idx + len(marker):]
    return raw


def _pipe_safe(text: str | None) -> str:
    """Escape pipes so a source title cannot break the markdown table."""
    return (text or "").replace("|", "\\|")


def yaml_escape(text: str) -> str:
    return '"' + (text or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_notebook_note(nb: dict, sources: list[dict], notes: list[dict],
                        fulltext_map: dict[str, str]) -> Path:
    """Render one notebook as a markdown note. Bare wikilinks only (vault rule 14)."""
    root = mirror_root()
    root.mkdir(parents=True, exist_ok=True)
    title = nb.get("title") or "(sin título)"
    path = root / f"{slugify(title)} — {nb['id'][:8]}.md"

    lines = [
        "---",
        f"creationDate: {(nb.get('created_at') or '')[:10]}",
        "type: notebook",
        "source: notebooklm",
        f"notebook_id: {nb['id']}",
        f"modified_at: {nb.get('modified_at') or ''}",
        f"source_count: {len(sources)}",
        f"note_count: {len(notes)}",
        f"aliases: [{yaml_escape(title)}]",
        "---",
        "",
        f"> Espejo de solo lectura de un notebook de NotebookLM. Regenerable — "
        f"la fuente de verdad vive en NotebookLM.",
        "",
        "## Fuentes",
        "",
    ]

    if sources:
        lines += ["| # | Título | Tipo | Estado | Texto completo |",
                  "|---|---|---|---|---|"]
        for s in sources:
            ft = fulltext_map.get(s["id"])
            link = f"[[{Path(ft).stem}]]" if ft else "—"
            lines.append(
                f"| {s.get('index', '')} | {_pipe_safe(s.get('title'))} "
                f"| {s.get('type', '')} | {s.get('status', '')} | {link} |")
    else:
        lines.append("*(sin fuentes)*")

    lines += ["", "## Notas", ""]
    if notes:
        for n in notes:
            lines.append(f"### {n.get('title') or '(sin título)'}")
            lines.append("")
            lines.append((n.get("content") or n.get("text") or "").strip())
            lines.append("")
    else:
        lines.append("*(sin notas)*")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_fulltext(nb_id: str, src: dict, body: str) -> Path:
    root = mirror_root() / "fulltext" / nb_id
    root.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"\.(md|markdown|txt|pdf|docx?|epub|html?)$", "",
                  (src.get("title") or src["id"]), flags=re.I)
    path = root / f"{slugify(stem, 70)} — {src['id'][:8]}.md"
    header = [
        "---",
        "type: notebook-source",
        "source: notebooklm",
        f"notebook_id: {nb_id}",
        f"source_id: {src['id']}",
        f"source_type: {src.get('type', '')}",
        f"aliases: [{yaml_escape(src.get('title') or '')}]",
        "---",
        "",
    ]
    path.write_text("\n".join(header) + (body or "").strip() + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------ stage A: mirror
def fetch_notebooks() -> list[dict]:
    """List notebooks, honouring the pagination cap with an explicit warning."""
    data = call_with_backoff(nlm_json, "list")
    items = data.get("notebooks", []) if isinstance(data, dict) else (data or [])
    # The CLI returns the whole library in one response, so this cap never
    # paginates — it only truncates. Truncating the INVENTORY would silently
    # exclude notebooks from the mirror forever (the per-run budget is
    # --max-notebooks, which is the real throttle). So warn loudly and keep
    # every notebook; the cap is a tripwire, not a limit.
    cap = MAX_PAGES * 250
    if len(items) >= cap * 0.9:
        err(f"⚠️  {len(items)} notebooks — cerca del umbral de aviso ({cap}). "
            f"NEXT ACTION: subí NOTEBOOKLM_MAX_PAGES si el inventario sigue creciendo.")
        log_event("inventory", "warn", returned=len(items), threshold=cap)
    return items


def quarantine_expired(prev: dict) -> bool:
    """Give a quarantined notebook another chance after STRIKE_DECAY_DAYS.

    Strikes come from any failure, including a transient network blip. Making
    quarantine permanent means three bad moments exile a notebook from the
    mirror for good — silently, since only doctor mentions it.
    """
    struck = parse_iso(prev.get("struck_at_utc"))
    if struck is None:
        return True                      # pre-existing entry: let it retry once
    age_days = (datetime.now(timezone.utc) - struck).total_seconds() / 86400
    return age_days >= STRIKE_DECAY_DAYS


def mirror_one(nb: dict, want_fulltext: bool) -> dict:
    """Mirror a single notebook. Returns per-notebook stats."""
    nb_id = nb["id"]
    call_with_backoff(nlm, "use", nb_id)

    sdata = call_with_backoff(nlm_json, "source", "list") or {}
    sources = sdata.get("sources", [])
    try:
        ndata = call_with_backoff(nlm_json, "note", "list") or {}
        notes = ndata.get("notes", [])
    except RuntimeError:
        notes = []          # notes are optional; never fail a mirror over them

    fulltext_map: dict[str, str] = {}
    ft_written = ft_failed = 0
    if want_fulltext:
        for s in sources:
            if s.get("status") not in (None, "ready"):
                continue
            try:
                body = strip_fulltext_preamble(
                    call_with_backoff(nlm, "source", "fulltext", s["id"],
                                      "--format", "markdown") or "")
                if DRY_RUN:
                    fulltext_map[s["id"]] = f"(dry-run) {s['id'][:8]}"
                else:
                    fulltext_map[s["id"]] = str(write_fulltext(nb_id, s, body))
                ft_written += 1
            except AuthExpired:
                raise
            except Exception as exc:
                ft_failed += 1
                log_event("fulltext", "error", notebook_id=nb_id,
                          source_id=s["id"], reason=str(exc)[:200])

    if not DRY_RUN:
        write_notebook_note(nb, sources, notes, fulltext_map)

    return {"sources": len(sources), "notes": len(notes),
            "fulltext": ft_written, "fulltext_failed": ft_failed}


def cmd_mirror(args) -> int:
    """Incremental, resumable mirror. Never sweeps all notebooks in one run."""
    if not acquire_lock():
        err("⚠️  Otra corrida de ingest-notebooklm está activa. NEXT ACTION: esperá "
            "a que termine, o borrá el lock si el proceso murió: "
            f"rm {LOCK_PATH}")
        log_event("mirror", "skipped", reason="lock_held")
        return 0

    started = time.time()
    stats = {"scanned": 0, "mirrored": 0, "skipped": 0, "failed": 0,
             "sources": 0, "fulltext": 0}
    try:
        # Preflight: a dead session costs one cheap check here instead of
        # failing partway through a multi-minute run.
        st, detail, _days = auth_status()
        if st != "PASS":
            d = explain(detail) or ("La sesión de Google no es válida.",
                                    "notebooklm login --fresh --browser chrome")
            err(f"✗ {d[0]}\n   NEXT ACTION: {d[1]}")
            log_event("mirror", "auth_expired", stage="preflight")
            return 2
        notebooks = fetch_notebooks()
        cursor = load_cursor()
        limit = args.max_notebooks if args.max_notebooks is not None else MAX_NOTEBOOKS

        # Only notebooks whose modified_at moved (or never seen) need work.
        pending = []
        for nb in notebooks:
            prev = cursor.get(nb["id"], {})
            if prev.get("strikes", 0) >= POISON_MAX_STRIKES and \
                    not quarantine_expired(prev):
                continue                                  # poison: quarantined
            if args.force or needs_mirror(nb, prev):
                pending.append(nb)
        # Oldest-mirrored first, never-mirrored before everything. Sorting by
        # modified_at descending (the obvious choice) starves the tail: with a
        # per-run budget smaller than the daily edit rate, notebooks that are
        # never touched would never reach the front of the queue and would
        # never be mirrored at all. This ordering guarantees convergence.
        pending.sort(key=lambda n: cursor.get(n["id"], {}).get("mirrored_at_utc") or "")

        stats["scanned"] = len(notebooks)
        stats["skipped"] = len(notebooks) - len(pending)
        backlog = max(0, len(pending) - limit)
        out(f"📚 {len(notebooks)} notebooks · {len(pending)} con cambios · "
            f"procesando hasta {limit}")
        if DRY_RUN:
            out("   (--dry-run: no se escribe nada en el vault)")

        no_progress = 0
        for nb in pending[:limit]:
            title = (nb.get("title") or "(sin título)")[:58]
            try:
                res = mirror_one(nb, want_fulltext=MIRROR_FULLTEXT and not args.no_fulltext)
                stats["mirrored"] += 1
                stats["sources"] += res["sources"]
                stats["fulltext"] += res["fulltext"]
                no_progress = 0
                if not DRY_RUN:
                    cursor[nb["id"]] = {
                        "modified_at": nb.get("modified_at"),
                        "source_count": res["sources"],
                        "mirrored_at_utc": datetime.now(timezone.utc).isoformat(),
                        "strikes": 0,
                    }
                    save_cursor(cursor)      # persist per item: resumable
                out(f"  ✓ {title} — {res['sources']} fuentes, "
                    f"{res['fulltext']} textos")
            except AuthExpired as exc:
                d = explain(str(exc)) or ("La sesión de Google expiró.",
                                          "Corré: notebooklm login --fresh --browser chrome")
                err(f"✗ Sesión expirada a mitad de corrida. {d[0]}\n   NEXT ACTION: {d[1]}")
                log_event("mirror", "auth_expired", mirrored=stats["mirrored"])
                return 2
            except Exception as exc:
                stats["failed"] += 1
                no_progress += 1
                prev = cursor.get(nb["id"], {})
                prev["strikes"] = prev.get("strikes", 0) + 1
                prev["struck_at_utc"] = datetime.now(timezone.utc).isoformat()
                prev.setdefault("modified_at", None)
                cursor[nb["id"]] = prev
                save_cursor(cursor)
                d = explain(str(exc))
                hint = f" — {d[1]}" if d else ""
                err(f"  ✗ {title}: {str(exc)[:120]}{hint}")
                log_event("mirror_item", "error", notebook_id=nb["id"],
                          strikes=prev["strikes"], reason=str(exc)[:200])
                if prev["strikes"] >= POISON_MAX_STRIKES:
                    err(f"     ⚠️  en cuarentena tras {POISON_MAX_STRIKES} fallos; "
                        f"se omitirá en las próximas corridas")
                if no_progress >= POISON_MAX_STRIKES:
                    err("⚠️  Tres fallos seguidos sin progreso — abortando la corrida.")
                    log_event("mirror", "aborted", reason="no_progress")
                    break

        if backlog:
            out(f"⏭️  Quedan {backlog} notebooks pendientes para la próxima corrida.")
            log_event("backlog", "warn", pending=backlog)

        dur = round(time.time() - started, 1)
        log_event("mirror", "ok" if not stats["failed"] else "partial",
                  duration=dur, **stats)
        if not DRY_RUN:
            ensure_state_dir()
            SENTINEL_MIRROR.touch()
            SENTINEL_SUCCESS.touch()
        out(f"✅ {stats['mirrored']} espejados · {stats['sources']} fuentes · "
            f"{stats['fulltext']} textos · {stats['failed']} fallos · {dur}s")
        return 0 if not stats["failed"] else 1
    finally:
        release_lock()


# -------------------------------------------------------------- stage B: push
def collect_push_files(args) -> list[Path]:
    files: list[Path] = []
    if args.file:
        for f in args.file:
            p = Path(f).expanduser()
            if not p.is_file():
                raise RuntimeError(f"no existe el archivo: {p}")
            files.append(p)
    if args.folder:
        base = Path(args.folder).expanduser()
        if not base.is_dir():
            raise RuntimeError(f"no existe la carpeta: {base}")
        files.extend(sorted(p for p in base.glob(args.glob) if p.is_file()))
    return files


def cmd_push(args) -> int:
    """vault -> NotebookLM. Every write is audited; --dry-run changes nothing."""
    if not acquire_lock():
        err(f"⚠️  Otra corrida está activa. NEXT ACTION: esperá, o borrá {LOCK_PATH} "
            "si el proceso murió.")
        return 0
    try:
        files = collect_push_files(args)
        if not files:
            err("No hay archivos que subir. NEXT ACTION: pasá --file <ruta> o "
                "--folder <carpeta> [--glob '*.md']")
            return 1

        cap = MAX_PAGES * 25
        if len(files) > cap:
            err(f"⚠️  {len(files)} archivos supera el tope de {cap} por corrida; "
                f"se suben los primeros {cap}. NEXT ACTION: acotá --glob o repetí.")
            log_event("push", "warn", reason="cap_hit", found=len(files), cap=cap)
            files = files[:cap]

        out(f"📤 {len(files)} archivo(s) → notebook {args.notebook}")
        if DRY_RUN:
            out("   (--dry-run: no se sube nada)")

        sent = failed = 0
        for p in files:
            try:
                body = p.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                failed += 1
                err(f"  ✗ {p.name}: ilegible ({exc})")
                continue
            digest = sha8(body)
            title = args.title or p.stem
            if DRY_RUN:
                out(f"  ▸ subiría {p.name} como «{title}» (sha {digest})")
                audit("push_source", notebook_id=args.notebook, file=str(p),
                      source_title=title, content_sha8=digest, result="dry-run")
                sent += 1
                continue
            try:
                call_with_backoff(nlm, "source", "add", str(p),
                                  "-n", args.notebook, "--title", title)
                sent += 1
                audit("push_source", notebook_id=args.notebook, file=str(p),
                      source_title=title, content_sha8=digest, result="ok")
                out(f"  ✓ {p.name}")
            except AuthExpired as exc:
                d = explain(str(exc)) or ("Sesión expirada.",
                                          "notebooklm login --fresh --browser chrome")
                err(f"✗ Sesión expirada. NEXT ACTION: {d[1]}")
                audit("push_source", notebook_id=args.notebook, file=str(p),
                      source_title=title, content_sha8=digest, result="auth-expired")
                log_event("push", "auth_expired", sent=sent)
                return 2
            except Exception as exc:
                failed += 1
                d = explain(str(exc))
                hint = f" — {d[1]}" if d else ""
                err(f"  ✗ {p.name}: {str(exc)[:120]}{hint}")
                audit("push_source", notebook_id=args.notebook, file=str(p),
                      source_title=title, content_sha8=digest,
                      result="error", reason=str(exc)[:200])

        log_event("push", "ok" if not failed else "partial",
                  notebook_id=args.notebook, sent=sent, failed=failed)
        if not DRY_RUN and sent:
            ensure_state_dir()
            SENTINEL_SUCCESS.touch()
        out(f"✅ {sent} subidas · {failed} fallos")
        return 0 if not failed else 1
    finally:
        release_lock()


# ------------------------------------------------------------------- doctor
def parse_iso(ts: str | None) -> datetime | None:
    """Parse NotebookLM's ISO-8601 timestamps; tolerate a trailing Z."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def needs_mirror(nb: dict, prev: dict, tolerance_s: int = 180) -> bool:
    """Decide whether a notebook changed since we last mirrored it.

    Critical subtlety: `notebooklm use` bumps the notebook's own modified_at,
    so the act of mirroring invalidates a naive modified_at cursor and the
    mirror would never converge. We therefore compare the listed modified_at
    against the wall-clock instant our last mirror finished, with a tolerance
    that absorbs the bump and any clock skew between us and Google.
    """
    if not prev:
        return True
    done = parse_iso(prev.get("mirrored_at_utc"))
    mod = parse_iso(nb.get("modified_at"))
    if done is None or mod is None:
        return True
    return (mod - done).total_seconds() > tolerance_s


def _nearest_existing(path: Path) -> Path:
    """Closest existing ancestor — lets doctor probe writability without creating."""
    for cand in [path, *path.parents]:
        if cand.is_dir():
            return cand
    return Path("/")


@dataclass
class Check:
    name: str
    status: str          # PASS | WARN | FAIL
    detail: str
    action: str = ""


def auth_status() -> tuple[str, str, int | None]:
    """Returns (status, detail, days_to_expiry)."""
    try:
        # `--test` is mandatory: without it the CLI reports "Authentication is
        # valid" purely from cookie presence/expiry metadata, which stays true
        # long after Google has invalidated the session server-side. Only the
        # token fetch that --test performs proves the session actually works.
        proc = subprocess.run([NLM_BIN, "-p", PROFILE, "auth", "check", "--test", "--json"],
                              capture_output=True, text=True, timeout=90)
        blob = proc.stdout.strip()
        days = None
        if blob:
            try:
                data = json.loads(blob)
                flat = json.dumps(data)
                m = re.search(r"(20\d\d-\d\d-\d\dT[\d:]+)", flat)
                if m:
                    exp = time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
                    days = int((exp - time.time()) / 86400)
            except json.JSONDecodeError:
                pass
        blob_all = f"{proc.stdout}\n{proc.stderr}"
        if proc.returncode != 0 or re.search(
                r"expired or invalid|token fetch failed|re-authenticate", blob_all, re.I):
            reason = "sesión vencida (el token fetch falló)"
            return "FAIL", reason, days
        return "PASS", "sesión válida y verificada", days
    except Exception as exc:
        return "FAIL", str(exc)[:160], None


def run_checks(deep: bool = True) -> list[Check]:
    checks: list[Check] = []

    # 1. env
    if not VAULT_DIR or str(VAULT_DIR) == ".":
        checks.append(Check("env", "FAIL", "NOTEBOOKLM_VAULT_DIR sin definir",
                            "exportalo en ~/.zshrc o corré install.sh"))
    elif not VAULT_DIR.is_dir():
        checks.append(Check("env", "FAIL", f"no existe {VAULT_DIR}",
                            "corregí NOTEBOOKLM_VAULT_DIR en ~/.zshrc"))
    else:
        checks.append(Check("env", "PASS", str(VAULT_DIR)))

    # 2b. config parse (a silent fallback to defaults is a support nightmare)
    cfg_path = STATE_DIR / "config.json"
    if cfg_path.is_file() and not _CFG:
        try:
            json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as exc:
            checks.append(Check("config", "WARN",
                                f"config.json ilegible ({str(exc)[:60]}); usando defaults",
                                f"corregí el JSON en {cfg_path}"))

    # 2. state dir + mode
    if STATE_DIR.is_dir():
        mode = stat.S_IMODE(STATE_DIR.stat().st_mode)
        if mode == 0o700:
            checks.append(Check("state-dir", "PASS", f"{STATE_DIR} modo 0700"))
        else:
            checks.append(Check("state-dir", "WARN", f"modo {oct(mode)}, se espera 0700",
                                f"chmod 700 {STATE_DIR}  (o corré: nlm.py fix)"))
    else:
        checks.append(Check("state-dir", "FAIL", f"no existe {STATE_DIR}",
                            "corré: nlm.py fix"))

    # 3. binary (+ the version whose JSON shape we parse)
    if Path(NLM_BIN).is_file() and os.access(NLM_BIN, os.X_OK):
        ver = "?"
        try:
            proc = subprocess.run([NLM_BIN, "--version"], capture_output=True,
                                  text=True, timeout=30)
            m = re.search(r"(\d+\.\d+\.\d+)", proc.stdout or "")
            ver = m.group(1) if m else "?"
        except Exception:
            pass
        if ver != "?" and tuple(int(x) for x in ver.split(".")) < TESTED_NLM_MIN:
            checks.append(Check("notebooklm-bin", "WARN",
                                f"notebooklm {ver} < probado "
                                f"{'.'.join(map(str, TESTED_NLM_MIN))}",
                                'uv tool install --force '
                                '"notebooklm-py[browser,mcp,markdown]"'))
        else:
            checks.append(Check("notebooklm-bin", "PASS", f"{NLM_BIN} (v{ver})"))
    else:
        checks.append(Check("notebooklm-bin", "FAIL", f"no ejecutable: {NLM_BIN}",
                            'uv tool install "notebooklm-py[browser,mcp]"'))

    # 4. auth (+ expiry horizon)
    if deep:
        st, detail, days = auth_status()
        if st == "PASS" and days is not None and days < COOKIE_WARN_DAYS:
            checks.append(Check("auth", "WARN", f"la sesión expira en {days} días",
                                "notebooklm login --fresh --browser chrome"))
        elif st == "PASS":
            extra = f" (cookie declara {days} días, no es garantía)" if days is not None else ""
            checks.append(Check("auth", "PASS", detail + extra))
        else:
            d = explain(detail)
            checks.append(Check("auth", "FAIL", detail,
                                d[1] if d else "notebooklm login --fresh --browser chrome"))
    else:
        checks.append(Check("auth", "WARN", "no verificado (--shallow)", "corré sin --shallow"))

    # 5. vault writable  (read-only probe: never creates the mirror tree)
    try:
        root = mirror_root()
        probe = root if root.is_dir() else _nearest_existing(root)
        if os.access(probe, os.W_OK):
            note = "" if root.is_dir() else " (se creará en el primer espejo)"
            checks.append(Check("vault-writable", "PASS", str(root) + note))
        else:
            checks.append(Check("vault-writable", "FAIL", f"sin permiso de escritura en {probe}",
                                "verificá permisos de la carpeta del vault"))
    except Exception as exc:
        checks.append(Check("vault-writable", "FAIL", str(exc)[:140],
                            "verificá permisos de la carpeta del vault"))

    # 6. log writable  (read-only: reports, never creates the state dir)
    if not STATE_DIR.is_dir():
        checks.append(Check("log-writable", "FAIL", f"falta {STATE_DIR}",
                            "corré: nlm.py fix"))
    elif os.access(STATE_DIR, os.W_OK):
        checks.append(Check("log-writable", "PASS", str(LOG_PATH)))
    else:
        checks.append(Check("log-writable", "FAIL", f"sin permiso de escritura en {STATE_DIR}",
                            "corré: nlm.py fix"))

    # 7. sentinel age (silent-death detector)
    if SENTINEL_MIRROR.exists():
        age_h = (time.time() - SENTINEL_MIRROR.stat().st_mtime) / 3600
        if age_h > MIRROR_STALE_H:
            checks.append(Check("sentinel-age", "FAIL", f"último espejo hace {age_h:.0f} h",
                                "corré: nlm.py mirror  y revisá el LaunchAgent"))
        else:
            checks.append(Check("sentinel-age", "PASS", f"último espejo hace {age_h:.1f} h"))
    else:
        checks.append(Check("sentinel-age", "WARN", "nunca corrió un espejo",
                            "corré: nlm.py mirror --max-notebooks 5"))

    # 8. failure rate + quarantine depth
    try:
        recent = []
        if LOG_PATH.exists():
            lines = LOG_PATH.read_text(encoding="utf-8").splitlines()[-200:]
            for ln in lines:
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                if rec.get("event") in ("mirror", "push"):
                    recent.append(rec.get("status"))
        bad = sum(1 for s in recent if s in ("error", "partial", "auth_expired"))
        cur = load_cursor()
        poison = [k for k, v in cur.items() if v.get("strikes", 0) >= POISON_MAX_STRIKES]
        if recent and bad / len(recent) > 0.5:
            checks.append(Check("failure-rate", "FAIL",
                                f"{bad}/{len(recent)} corridas recientes con fallo",
                                f"revisá los últimos errores: tail {LOG_PATH}"))
        elif poison:
            checks.append(Check("failure-rate", "WARN",
                                f"{len(poison)} notebook(s) en cuarentena",
                                "revisalos y reintentá con: nlm.py mirror --force"))
        else:
            checks.append(Check("failure-rate", "PASS",
                                f"{bad}/{len(recent) or 0} corridas con fallo"))
    except Exception as exc:
        checks.append(Check("failure-rate", "WARN", str(exc)[:140], ""))

    # 9. scheduler  (launchd is macOS-only; say so instead of failing blind)
    if sys.platform != "darwin":
        checks.append(Check("scheduler", "WARN",
                            f"agendamiento no soportado en {sys.platform}",
                            "programalo con cron/systemd: nlm.py mirror"))
        return checks
    try:
        proc = subprocess.run(["launchctl", "list"], capture_output=True,
                              text=True, timeout=20)
        if "com.ingest-notebooklm" in proc.stdout:
            checks.append(Check("scheduler", "PASS", "LaunchAgent cargado"))
        else:
            checks.append(Check("scheduler", "WARN", "LaunchAgent no cargado",
                                "launchctl bootstrap gui/$(id -u) "
                                "~/Library/LaunchAgents/com.ingest-notebooklm.plist"))
    except Exception:
        checks.append(Check("scheduler", "WARN", "no se pudo consultar launchctl", ""))

    return checks


def cmd_doctor(args) -> int:
    checks = run_checks(deep=not args.shallow)
    if args.json:
        print(json.dumps([c.__dict__ for c in checks], ensure_ascii=False, indent=2))
    else:
        icons = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}
        out("ingest-notebooklm doctor")
        out("─" * 62)
        for c in checks:
            out(f"{icons[c.status]} {c.name:<16} {c.status:<5} {c.detail}")
            if c.action and c.status != "PASS":
                out(f"  └─ NEXT ACTION: {c.action}")
        n_ok = sum(1 for c in checks if c.status == "PASS")
        out("─" * 62)
        out(f"{n_ok}/{len(checks)} en verde")
    return 1 if any(c.status == "FAIL" for c in checks) else 0


# ---------------------------------------------------------------------- fix
def cmd_fix(args) -> int:
    """Idempotent repair. Cannot re-authenticate: auth is a browser profile."""
    fixed: list[str] = []
    manual: list[tuple[str, str]] = []

    if not STATE_DIR.is_dir():
        ensure_state_dir()
        fixed.append(f"creado {STATE_DIR} en modo 0700")
    elif stat.S_IMODE(STATE_DIR.stat().st_mode) != 0o700:
        os.chmod(STATE_DIR, 0o700)
        fixed.append(f"permisos de {STATE_DIR} corregidos a 0700")

    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age > LOCK_STALE_AGE:
            LOCK_PATH.unlink(missing_ok=True)
            fixed.append(f"lock añejo removido ({age / 3600:.1f} h)")

    if CURSOR_PATH.exists():
        try:
            json.loads(CURSOR_PATH.read_text(encoding="utf-8"))
        except Exception:
            CURSOR_PATH.rename(CURSOR_PATH.with_suffix(".corrupt"))
            fixed.append("cursor corrupto apartado; el próximo espejo reconstruye")

    if VAULT_DIR and VAULT_DIR.is_dir():
        try:
            mirror_root().mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            manual.append(("no se pudo crear la carpeta espejo en el vault",
                           f"revisá permisos: {exc}"))

    for c in run_checks(deep=True):
        if c.status == "FAIL" and c.name in ("env", "auth", "notebooklm-bin", "vault-writable"):
            manual.append((c.detail, c.action))

    if args.quiet or getattr(args, "quiet_fix", False):
        if manual:
            err(f"ingest-notebooklm: {len(manual)} problema(s) requieren tu intervención "
                "— corré: nlm.py fix")
            return 1
        return 0

    if fixed:
        out("🔧 Reparado automáticamente:")
        for f in fixed:
            out(f"  ✓ {f}")
    if manual:
        out("\n⚠️  Esto necesita que vos hagas algo:")
        for detail, action in manual:
            out(f"  • {detail}")
            out(f"    → {action}")
        return 1
    if not fixed:
        out("✅ Todo en orden, no había nada que reparar.")
    else:
        out("\n✅ Listo.")
    return 0


# ---------------------------------------------------------------------- main
def main() -> int:
    global QUIET, DRY_RUN
    p = argparse.ArgumentParser(
        prog="nlm.py", description="ingest-notebooklm — conector NotebookLM ↔ vault")
    p.add_argument("--version", action="version",
                   version=f"ingest-notebooklm {VERSION}")
    p.add_argument("--dry-run", action="store_true", help="no escribe nada; muestra qué haría")
    p.add_argument("--quiet", action="store_true", help="silencio salvo errores")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mirror", help="NotebookLM → vault (incremental)")
    m.add_argument("--max-notebooks", type=int, default=None)
    m.add_argument("--no-fulltext", action="store_true", help="solo metadata y notas")
    m.add_argument("--force", action="store_true", help="reprocesa aunque no haya cambios")
    m.set_defaults(func=cmd_mirror)

    pu = sub.add_parser("push", help="vault → NotebookLM (escritura, auditada)")
    pu.add_argument("--notebook", required=True, help="ID (acepta parciales)")
    pu.add_argument("--file", action="append", help="archivo a subir (repetible)")
    pu.add_argument("--folder", help="carpeta a subir")
    pu.add_argument("--glob", default="*.md", help="patrón dentro de --folder")
    pu.add_argument("--title", help="título fijo (por defecto, el nombre del archivo)")
    pu.set_defaults(func=cmd_push)

    d = sub.add_parser("doctor", help="8 chequeos de salud, solo lectura")
    d.add_argument("--json", action="store_true")
    d.add_argument("--shallow", action="store_true", help="omite la verificación de sesión")
    d.set_defaults(func=cmd_doctor)

    f = sub.add_parser("fix", help="repara lo reparable y guía el resto")
    # Accepted after the subcommand too (`fix --quiet`), which is how the
    # SessionStart hook and the SKILL doc invoke it. A separate dest keeps the
    # global `--quiet` from being clobbered by this one's default.
    f.add_argument("--quiet", action="store_true", dest="quiet_fix")
    f.set_defaults(func=cmd_fix)

    v = sub.add_parser("version", help="imprime la versión")
    v.set_defaults(func=lambda a: (print(f"ingest-notebooklm {VERSION}"), 0)[1])

    args = p.parse_args()
    QUIET = args.quiet
    DRY_RUN = args.dry_run
    args.quiet = getattr(args, "quiet", False)

    try:
        return args.func(args)
    except KeyboardInterrupt:
        err("\nInterrumpido. El progreso parcial quedó guardado.")
        release_lock()
        return 130
    except Exception as exc:
        d = explain(str(exc))
        err(f"✗ {exc}")
        if d:
            err(f"  {d[0]}\n  NEXT ACTION: {d[1]}")
        log_event(getattr(args, "cmd", "?"), "error", reason=str(exc)[:300])
        release_lock()
        return 1


if __name__ == "__main__":
    sys.exit(main())
