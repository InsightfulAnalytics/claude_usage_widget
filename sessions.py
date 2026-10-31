"""Live Claude Code sessions on this machine: what state each one is in and how many tokens it uses.

Claude Code keeps ~/.claude/sessions/<pid>.json current for every running process, with `status`
busy, idle or waiting (plus `waitingFor` when waiting). That file is internal and undocumented, so
every field is read defensively and a session whose status can't be read still shows. Tokens come
from the transcripts under ~/.claude/projects. Read-only: nothing under ~/.claude is written.
"""

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

NEEDS_YOU, STALLED, YOUR_TURN, RUNNING, UNKNOWN = "needs_you", "stalled", "your_turn", "running", "unknown"
STATE_ORDER = [NEEDS_YOU, STALLED, YOUR_TURN, RUNNING, UNKNOWN]
STATE_LABELS = {NEEDS_YOU: "Needs you", STALLED: "Stalled", YOUR_TURN: "Your turn", RUNNING: "Running",
                UNKNOWN: "Unknown"}
ATTENTION = (NEEDS_YOU, STALLED)
BUSY = (RUNNING, STALLED, NEEDS_YOU)  # a turn is under way
STALLED_AFTER = timedelta(minutes=5)
LONG_TURN = timedelta(minutes=2)
LOOKUP_RETRY_SECONDS = 30  # a session with no transcript yet (just started) is looked for again after this
SUBAGENT_RESCAN_SECONDS = 10

DEFAULT_WINDOW = 200_000
EXTENDED_WINDOW = 1_000_000
ONE_MILLION_FAMILIES = ("fable", "mythos")  # 1M is their default; Opus and Sonnet need [1m]
SYNTHETIC_MODEL = "<synthetic>"

# Most of a transcript's bytes are tool results, request blobs and file snapshots. Lines without
# one of these never matter, so they are skipped before the JSON parse.
WANTED = (b'"usage"', b"compact_boundary", b'-title"', b'"modelId"')

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def is_same_process(pid, proc_start):
    """True when `pid` is running and started at `proc_start` (FILETIME ticks, what Claude Code
    records), False when it is gone or is another process that reused the PID, None when the
    process exists but can't be inspected (elevated, say): treated as alive."""
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None if ctypes.get_last_error() == 5 else False  # ERROR_ACCESS_DENIED
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not _kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                         ctypes.byref(kernel), ctypes.byref(user)):
            return None
        if exited.dwHighDateTime or exited.dwLowDateTime:
            return False  # exited, but someone still holds a handle to it
        return proc_start is None or (created.dwHighDateTime << 32 | created.dwLowDateTime) == proc_start
    finally:
        _kernel32.CloseHandle(handle)


def ms_time(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        try:
            return datetime.fromtimestamp(value / 1000, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def iso_ms(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return None


def text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


@dataclass
class SessionFile:
    pid: int
    session_id: str
    cwd: str
    name: str | None
    name_source: str | None  # "derived" for the default folder name, else set by a rename
    kind: str
    entrypoint: str | None
    status: str | None
    waiting_for: str | None
    status_since: datetime | None
    started_at: datetime | None
    proc_start: int | None
    version: str | None


def parse_session_file(data):
    """None when the file has no pid or session id; every other field may be missing."""
    if not isinstance(data, dict):
        return None
    pid, session_id = data.get("pid"), text(data.get("sessionId"))
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not session_id:
        return None
    proc_start = data.get("procStartFt", data.get("procStart"))
    try:
        proc_start = int(proc_start) if proc_start is not None else None
    except (TypeError, ValueError):
        proc_start = None
    return SessionFile(
        pid=pid, session_id=session_id, cwd=text(data.get("cwd")) or "", name=text(data.get("name")),
        name_source=text(data.get("nameSource")), kind=text(data.get("kind")) or "interactive", entrypoint=text(data.get("entrypoint")),
        status=text(data.get("status")), waiting_for=text(data.get("waitingFor")),
        status_since=ms_time(data.get("statusUpdatedAt")), started_at=ms_time(data.get("startedAt")),
        proc_start=proc_start, version=text(data.get("version")))


@dataclass
class Tokens:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def total(self):
        return self.input + self.output + self.cache_read + self.cache_write

    def __add__(self, other):
        return Tokens(self.input + other.input, self.output + other.output,
                      self.cache_read + other.cache_read, self.cache_write + other.cache_write)


def count(value):
    return max(0, int(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def is_older(a, b):
    return a is not None and b is not None and a < b


class TranscriptReader:
    """One transcript, read in full once and then only from where the last read stopped.

    Context follows claude-context-status: the context of the latest main-chain reply (input +
    cache write + cache read), where usage and compactions only move forward in time. Claude Code
    sometimes re-writes a run of old messages after newer ones, and an older reply is history,
    not the current context. Totals count every reply once: a reply split over several lines
    repeats its usage on each, and re-written messages repeat their ids.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.reset()

    def reset(self):
        self.offset = 0
        self.partial = b""
        self.mtime = None
        self.totals = Tokens()
        self.seen = set()
        self.last_usage = None  # (time ms, context tokens, model)
        self.last_compact_ms = None
        self.compacted = False  # a compaction newer than the latest reply
        self.peaks = {}  # largest context per model, so a 1M window inferred from usage survives /compact
        self.attachment = None  # (model id, marketing name) of the latest model attachment
        self.ai_title = None
        self.custom_title = None

    @property
    def title(self):
        return self.custom_title or self.ai_title

    def refresh(self):
        """Read what was added since the last call. False when the file is missing or unchanged."""
        try:
            st = self.path.stat()
        except OSError:
            return False
        if st.st_size < self.offset:
            self.reset()  # truncated or replaced
        if st.st_size == self.offset and st.st_mtime_ns == self.mtime:
            return False
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read(st.st_size - self.offset)
        except OSError:
            return False
        self.mtime = st.st_mtime_ns
        self.offset += len(chunk)
        lines = (self.partial + chunk).split(b"\n")
        self.partial = lines.pop()  # a line still being written
        for line in lines:
            self.apply_line(line)
        return True

    def apply_line(self, raw):
        if not any(w in raw for w in WANTED):
            return
        try:
            rec = json.loads(raw)
        except ValueError:
            return
        if isinstance(rec, dict):
            self.apply(rec)

    def apply(self, rec):
        kind = rec.get("type")
        main = rec.get("isSidechain") is not True
        if kind == "assistant":
            self.apply_reply(rec, main)
        elif not main:
            return
        elif kind == "system" and rec.get("subtype") == "compact_boundary":
            self.apply_compact(rec)
        elif kind == "ai-title":
            self.ai_title = text(rec.get("aiTitle")) or self.ai_title
        elif kind == "custom-title":
            self.custom_title = text(rec.get("customTitle")) or self.custom_title
        elif kind == "attachment":
            att = rec.get("attachment")
            identity = att.get("identity") if isinstance(att, dict) and att.get("type") == "model" else None
            if isinstance(identity, dict) and text(identity.get("modelId")):
                self.attachment = (text(identity.get("modelId")), text(identity.get("marketingName")))

    def apply_reply(self, rec, main):
        message = rec.get("message")
        if rec.get("isApiErrorMessage") is True or not isinstance(message, dict):
            return
        usage, model = message.get("usage"), message.get("model")
        model = model if isinstance(model, str) else ""
        if not isinstance(usage, dict) or model == SYNTHETIC_MODEL:
            return
        tokens = Tokens(count(usage.get("input_tokens")), count(usage.get("output_tokens")),
                        count(usage.get("cache_read_input_tokens")), count(usage.get("cache_creation_input_tokens")))
        key = message.get("id") or rec.get("requestId") or rec.get("uuid")
        if key not in self.seen:
            if key:
                self.seen.add(key)
            self.totals = self.totals + tokens
        context = tokens.input + tokens.cache_read + tokens.cache_write
        if not main or context <= 0:
            return
        self.peaks[model] = max(self.peaks.get(model, 0), context)
        when = iso_ms(rec.get("timestamp"))
        if is_older(when, self.last_compact_ms) or is_older(when, self.last_usage and self.last_usage[0]):
            return
        self.last_usage = (when, context, model)
        self.compacted = False

    def apply_compact(self, rec):
        when = iso_ms(rec.get("timestamp"))
        meta = rec.get("compactMetadata")
        before = count(meta.get("preTokens")) if isinstance(meta, dict) else 0
        if before and self.last_usage:
            model = self.last_usage[2]
            self.peaks[model] = max(self.peaks.get(model, 0), before)
        if is_older(when, self.last_compact_ms):
            return
        self.last_compact_ms = when
        if not is_older(when, self.last_usage and self.last_usage[0]):
            self.compacted = True

    def observed(self):
        """Largest context seen for the model of the latest reply."""
        if self.last_usage:
            return max(self.peaks.get(self.last_usage[2], 0), self.last_usage[1])
        return max(self.peaks.values(), default=0)


# Context window size. The transcript never states it; ported from claude-context-status window.ts.
BRACKETED_SIZE = re.compile(r"[(\[]\s*(\d+(?:[,_]\d+)*(?:\.\d+)?)\s*([km])\s*[)\]]", re.I)
CONTEXT_SIZE = re.compile(r"\b(\d+(?:[,_]\d+)*(?:\.\d+)?)\s*([km])\s*(?:token\s*)?context\b", re.I)
FAMILY = re.compile(r"(opus|sonnet|haiku|fable|mythos)", re.I)


def context_size(name):
    """Size named in a model string: `opus[1m]`, `claude-opus-5-5[1m]`, `Opus 5.5 (1M context)`."""
    match = (BRACKETED_SIZE.search(name) or CONTEXT_SIZE.search(name)) if name else None
    if not match:
        return None
    value = float(re.sub(r"[,_]", "", match[1]))
    return round(value * (1_000_000 if match[2].lower() == "m" else 1_000)) or None


def base_model(model_id):
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", model_id)


def model_family(model):
    match = FAMILY.search(model or "")
    return match[1].lower() if match else None


def same_family(a, b):
    fa, fb = model_family(a), model_family(b)
    return not fa or not fb or fa == fb


def resolve_window(attachment=None, reply_model=None, setting=None, observed=0):
    """(tokens, where the size came from). First hit wins: the session's model attachment when it
    names a size; the Claude Code model setting when it names one and is for the same family
    (Claude Code writes plain attachments even in 1M sessions); a family whose models are all 1M;
    else 200k. Usage above the size found raises it to 1M."""
    reply = reply_model or None
    # An attachment for another model than the latest reply is stale: /model switched since.
    if attachment and reply and base_model(attachment[0]) != reply:
        attachment = None
    found = None
    if attachment:
        size = context_size(attachment[0]) or context_size(attachment[1])
        if size:
            found = (size, f"session model {attachment[0]}")
    if not found:
        running = attachment[0] if attachment else reply
        applies = setting is not None and (not running or same_family(setting[0], running))
        size = context_size(setting[0]) if applies else None
        family = model_family(running)
        if size:
            found = (size, f"{setting[0]} from {setting[1]}")
        elif running and family in ONE_MILLION_FAMILIES:
            found = (EXTENDED_WINDOW, f"{family} models have 1M")
        else:
            found = (DEFAULT_WINDOW, "default")
    if observed > found[0] and found[0] < EXTENDED_WINDOW:
        return EXTENDED_WINDOW, f"usage above {found[0]:,} tokens"
    return found


class SettingsReader:
    """The model Claude Code starts with in a folder: env.ANTHROPIC_MODEL in the settings files,
    then the ANTHROPIC_MODEL environment variable, then `model` in the files. Files are read again
    only when their mtime or size changes."""

    def __init__(self, root):
        self.root = Path(root)
        self.cache = {}

    def read(self, path):
        try:
            st = path.stat()
        except OSError:
            return None
        cached = self.cache.get(path)
        if cached and cached[0] == (st.st_mtime_ns, st.st_size):
            return cached[1]
        value = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            env = data.get("env") if isinstance(data, dict) else None
            value = {"model": text(data.get("model")),
                     "env": text(env.get("ANTHROPIC_MODEL")) if isinstance(env, dict) else None}
        except (OSError, ValueError, AttributeError):
            pass
        self.cache[path] = ((st.st_mtime_ns, st.st_size), value)
        return value

    def model(self, cwd):
        paths = [Path(cwd) / ".claude" / "settings.local.json", Path(cwd) / ".claude" / "settings.json"] if cwd else []
        paths.append(self.root / "settings.json")
        files = [(p, self.read(p) or {}) for p in paths]
        for p, value in files:
            if value.get("env"):
                return value["env"], f"env.ANTHROPIC_MODEL in {p}"
        if text(os.environ.get("ANTHROPIC_MODEL")):
            return os.environ["ANTHROPIC_MODEL"].strip(), "ANTHROPIC_MODEL environment variable"
        for p, value in files:
            if value.get("model"):
                return value["model"], f"model in {p}"
        return None


@dataclass
class Session:
    pid: int
    session_id: str
    name: str
    title: str | None
    cwd: str
    kind: str
    entrypoint: str | None
    version: str | None
    state: str
    waiting_for: str | None
    since: datetime | None  # when it entered this state; for Stalled, the last transcript write
    status_since: datetime | None
    started_at: datetime | None
    model: str | None
    context: int | None  # tokens in the latest request; None before the first reply
    window: int
    window_source: str
    compacted: bool
    main: Tokens = field(default_factory=Tokens)
    subagents: Tokens = field(default_factory=Tokens)
    transcript: Path | None = None
    last_write: datetime | None = None

    @property
    def key(self):
        return self.pid, self.session_id

    @property
    def context_percent(self):
        return None if self.context is None else min(100.0, self.context * 100 / self.window)

    @property
    def total(self):
        return (self.main + self.subagents).total

    @property
    def background(self):
        return self.kind != "interactive"

    @property
    def folder(self):
        return Path(self.cwd).name or self.cwd


def classify(status, last_write, now, stalled_after=STALLED_AFTER):
    if status == "waiting":
        return NEEDS_YOU
    if status == "busy":
        return STALLED if last_write and now - last_write > stalled_after else RUNNING
    if status == "idle":
        return YOUR_TURN
    return UNKNOWN


def sort_key(session):
    """Most urgent state first; within a state, the one that has sat there longest."""
    since = session.since.timestamp() if session.since else float("inf")
    return STATE_ORDER.index(session.state), since, session.pid


def encode_cwd(cwd):
    """Claude Code's project folder name: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


class Tracked:
    """Readers kept for one live session between scans."""

    def __init__(self):
        self.main = None
        self.next_lookup = 0
        self.subagents = {}
        self.next_subagent_scan = 0


class Monitor:
    """Lists live sessions. `scan()` re-reads only the session files and transcripts that changed."""

    def __init__(self, root=None, now=None, alive=is_same_process, clock=time.monotonic,
                 stalled_after=STALLED_AFTER):
        self.root = Path(root) if root else config_dir()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.alive = alive
        self.clock = clock
        self.stalled_after = stalled_after
        self.files = {}  # session file path -> ((mtime, size), SessionFile or None)
        self.tracked = {}  # session id -> Tracked
        self.settings = SettingsReader(self.root)

    def scan(self):
        now = self.now()
        found = []
        try:
            paths = sorted((self.root / "sessions").glob("*.json"))
        except OSError:
            paths = []
        for path in paths:
            sf = self.read_session_file(path)
            if sf and self.alive(sf.pid, sf.proc_start) is not False:
                found.append(sf)
        current = set(paths)
        self.files = {p: v for p, v in self.files.items() if p in current}
        live = {sf.session_id for sf in found}
        self.tracked = {sid: t for sid, t in self.tracked.items() if sid in live}
        return sorted((self.build(sf, now) for sf in found), key=sort_key)

    def read_session_file(self, path):
        try:
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
            cached = self.files.get(path)
            if cached and cached[0] == stamp:
                return cached[1]
            sf = parse_session_file(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None  # vanished, or caught mid-write: read again next scan
        self.files[path] = (stamp, sf)
        return sf

    def transcript_for(self, sf, tracked):
        if tracked.main or self.clock() < tracked.next_lookup:
            return tracked.main
        projects = self.root / "projects"
        path = projects / encode_cwd(sf.cwd) / f"{sf.session_id}.jsonl"
        if not path.exists():
            # Very long folder names are shortened by Claude Code; search by session id instead.
            path = next(projects.glob(f"*/{sf.session_id}.jsonl"), None)
        if path:
            tracked.main = TranscriptReader(path)
        else:
            tracked.next_lookup = self.clock() + LOOKUP_RETRY_SECONDS
        return tracked.main

    def subagent_readers(self, tracked, sf):
        if self.clock() >= tracked.next_subagent_scan:
            tracked.next_subagent_scan = self.clock() + SUBAGENT_RESCAN_SECONDS
            folder = tracked.main.path.parent / sf.session_id / "subagents"
            try:
                for path in folder.rglob("*.jsonl"):
                    tracked.subagents.setdefault(path, TranscriptReader(path))
            except OSError:
                pass
        return tracked.subagents.values()

    def build(self, sf, now):
        tracked = self.tracked.setdefault(sf.session_id, Tracked())
        reader = self.transcript_for(sf, tracked)
        main, subagents, last_write = Tokens(), Tokens(), None
        if reader:
            reader.refresh()
            main = reader.totals
            mtimes = [reader.mtime]
            for sub in self.subagent_readers(tracked, sf):
                sub.refresh()
                subagents = subagents + sub.totals
                mtimes.append(sub.mtime)
            mtimes = [m for m in mtimes if m]
            last_write = datetime.fromtimestamp(max(mtimes) / 1e9, timezone.utc) if mtimes else None

        state = classify(sf.status, last_write, now, self.stalled_after)
        since = last_write if state == STALLED else sf.status_since
        usage = reader.last_usage if reader else None
        model = usage[2] if usage else (reader.attachment[0] if reader and reader.attachment else None)
        window, source = resolve_window(reader.attachment if reader else None, usage[2] if usage else None,
                                        self.settings.model(sf.cwd), reader.observed() if reader else 0)
        return Session(
            pid=sf.pid, session_id=sf.session_id, name=display_name(sf, reader),
            title=reader.title if reader else None, cwd=sf.cwd, kind=sf.kind, entrypoint=sf.entrypoint,
            version=sf.version, state=state, waiting_for=sf.waiting_for if state == NEEDS_YOU else None,
            since=since, status_since=sf.status_since, started_at=sf.started_at, model=model,
            context=usage[1] if usage else None, window=window, window_source=source,
            compacted=bool(reader and reader.compacted), main=main, subagents=subagents,
            transcript=reader.path if reader else None, last_write=last_write)


def display_name(sf, reader):
    """What the panel calls a session: the name it was given (/rename, --name, a rename from the
    Claude app, or a naming hook), else Claude Code's generated title, which summarises the first
    prompt. Claude Code's default name is the folder plus two characters (`markshot-c1`) and says
    nothing about the work, so it comes last."""
    renamed = sf.name if sf.name_source and sf.name_source != "derived" else None
    return ((reader.custom_title if reader else None) or renamed or (reader.ai_title if reader else None)
            or sf.name or Path(sf.cwd).name or str(sf.pid))


class Alerts:
    """Turns state changes into notifications. The first update only records what is there, so
    starting the widget (at login, say) never produces a burst for sessions already waiting."""

    def __init__(self, long_turn=LONG_TURN):
        self.long_turn = long_turn
        self.states = None  # session key -> state at the last update
        self.turn_start = {}  # session key -> when its current turn began

    def update(self, sessions, now=None):
        """[(kind, session, detail)] for this update; kind is "attention" or "finished"."""
        now = now or datetime.now(timezone.utc)
        first, previous = self.states is None, self.states or {}
        self.states = {s.key: s.state for s in sessions}
        self.turn_start = {k: v for k, v in self.turn_start.items() if k in self.states}
        changes = []
        for s in sessions:
            old = previous.get(s.key)
            if s.state in BUSY and s.key not in self.turn_start:
                # Claude Code restamps the status when a permission prompt is answered, so the turn
                # start is only taken when the turn begins (or when the widget first sees it).
                self.turn_start[s.key] = s.status_since
            if first or old == s.state:
                continue
            if s.state == NEEDS_YOU:
                changes.append(("attention", s, s.waiting_for or "Waiting for you"))
            elif s.state == STALLED:
                quiet = (now - s.since).total_seconds() if s.since else 0
                changes.append(("attention", s, f"No transcript writes for {format_elapsed(quiet)}"))
            elif s.state == YOUR_TURN and old in BUSY:
                start = self.turn_start.get(s.key)
                if start and s.status_since and s.status_since - start >= self.long_turn:
                    changes.append(("finished", s, f"Finished after {format_elapsed((s.status_since - start).total_seconds())}"))
            if s.state not in BUSY:
                self.turn_start.pop(s.key, None)
        return changes


def notification(changes):
    """(title, message) for one Windows notification covering every change, or None."""
    if not changes:
        return None
    if len(changes) == 1:
        kind, s, detail = changes[0]
        label = STATE_LABELS[s.state] if kind == "attention" else "Your turn"
        return f"{label}: {s.name}"[:63], f"{detail}\n{s.folder}"[:255]
    kinds = {kind for kind, _, _ in changes}
    n = len(changes)
    title = f"{n} sessions need you" if kinds == {"attention"} else \
        f"{n} long turns finished" if kinds == {"finished"} else f"{n} sessions changed"
    lines = [f"{s.name}: {STATE_LABELS[s.state]}" for _, s, _ in changes]
    return title, "\n".join(lines)[:255]


def format_elapsed(seconds):
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def format_tokens(n):
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= size:
            value = n / size
            return f"{value:.1f}{unit}" if value < 99.95 else f"{value:.0f}{unit}"
    return str(int(n))
