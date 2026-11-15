"""What the panel says. The wording shared by the Windows panel (widget.py) and the Omarchy bar
plugin, and `build()`, the plain data the plugin draws. No UI, covered by `tests/`.
"""

import sessions
import usage

ERROR_SHORT = {"login": "Login expired", "rate_limited": "Rate limited", "network": "Offline",
               "format": "Unexpected data"}
CONTEXT_WARNING_AT = 60  # same thresholds as claude-context-status
CONTEXT_CRITICAL_AT = 80
LOADING = "Claude usage: loading"
WINDOWS_HINT = "Click: go to its terminal (or copy its resume command if it has none). Right-click: more."
OMARCHY_HINT = ("Enter or click: go to its terminal · C or right-click: copy its resume command · "
                "O or middle-click: open its folder")


def status_text(limit):
    if limit.percent >= 100:
        return "Limit reached"
    return {"warning": "Near limit", "critical": "Almost out"}.get(limit.severity)


def context_severity(percent):
    if percent is None:
        return "stale"
    return "critical" if percent >= CONTEXT_CRITICAL_AT else "warning" if percent >= CONTEXT_WARNING_AT else "normal"


def state_text(s, now):
    text = sessions.STATE_LABELS[s.state]
    if s.since:
        text += " " + sessions.format_elapsed((now - s.since).total_seconds())
    if s.waiting_for:
        text += f" · {s.waiting_for}"
    if s.compacted:
        text += " · compacted"
    if s.background:
        text += " · bg"
    return text


def session_tip(s, now, hint=WINDOWS_HINT):
    """A session in full: the hover text of its row on Windows, the details under the list on
    Omarchy."""
    lines = [s.title or s.name, s.cwd]
    lines.append(f"{sessions.STATE_LABELS[s.state]}" + (f": {s.waiting_for}" if s.waiting_for else "")
                 + (f", last transcript write {sessions.format_elapsed((now - s.last_write).total_seconds())} ago"
                    if s.last_write else ""))
    if s.compacted:
        lines.append("Context: compacted since the last reply")
    elif s.context is None:
        lines.append("Context: no reply yet")
    else:
        lines.append(f"Context: {s.context:,} of {s.window:,} tokens ({s.context_percent:.1f}%)")
    lines.append(f"Window from {s.window_source}")
    t = s.main + s.subagents
    lines.append(f"Session total: {t.total:,} tokens")
    lines.append(f"  input {t.input:,}, output {t.output:,}, cache read {t.cache_read:,}, cache write {t.cache_write:,}")
    if s.subagents.total:
        lines.append(f"  of which subagents {s.subagents.total:,}")
    details = [s.model, s.entrypoint, f"PID {s.pid}"]
    if s.started_at:
        details.insert(0, f"started {s.started_at.astimezone():%a} {usage.format_clock(s.started_at)}")
    lines.append(", ".join(d for d in details if d))
    lines.append(s.session_id)
    lines.append(hint)
    return "\n".join(lines)


def attention_count(session_list):
    return sum(s.state in sessions.ATTENTION for s in session_list)


def summary_lines(data, error, session_list, now):
    """The icon's hover text, a line each: sessions that need you, the first two limits with
    their reset times, then what is wrong. Empty before the first numbers arrive."""
    lines = []
    n = attention_count(session_list)
    if n:
        lines.append(f"{n} session{'s' if n > 1 else ''} need{'' if n > 1 else 's'} you")
    for limit in (data.limits[:2] if data else []):
        reset = limit.resets_at
        when = ""
        if reset:
            left = (reset - now).total_seconds()
            when = f", resets {reset.astimezone():%a} " if left >= 86400 else ", resets "
            when += usage.format_clock(reset)
        # "Session" and "Weekly" alone; a model's weekly limit keeps its name.
        plain = limit.kind in usage.SESSION_KINDS + usage.WEEKLY_KINDS
        lines.append(f"{limit.label.split(' (')[0] if plain else limit.label} {round(limit.percent)}%{when}")
    if error:
        lines.append(ERROR_SHORT.get(error.kind, "Error"))
    return lines


def sessions_note(session_list):
    """(text, urgent) for the Sessions header."""
    n = attention_count(session_list)
    if n:
        return f"{n} need{'s' if n == 1 else ''} you", True
    n = len(session_list)
    return (f"{n} session{'s' if n != 1 else ''}" if n else ""), False


def limit_row(limit, error, now):
    status = None if error else status_text(limit)
    return {"label": limit.label, "percent": min(max(limit.percent, 0), 100),
            "percentText": f"{round(limit.percent)}%", "severity": "stale" if error else limit.severity,
            "reset": usage.format_reset(limit.resets_at, now), "status": status or ""}


def session_row(s, now):
    percent = None if s.compacted else s.context_percent
    return {"pid": s.pid, "name": s.name, "state": s.state, "stateText": state_text(s, now),
            "total": f"{sessions.format_tokens(s.total)} tokens" if s.total else "",
            "percent": None if percent is None else round(percent),
            "percentText": "" if percent is None else f"{round(percent)}%",
            "contextSeverity": context_severity(percent), "details": session_tip(s, now, OMARCHY_HINT)}


def build(data, error, session_list, now, flash=""):
    """Everything the Omarchy plugin draws, as JSON-ready data: `data` is the last good Usage or
    None, `error` the UsageError of the last fetch or None. Stale numbers stay on show, greyed."""
    headline = data.headline if data else None
    if error:
        note = ERROR_SHORT.get(error.kind, "Error")
    else:
        note = f"updated {usage.format_age(data.fetched_at, now)}" if data else ""
    message = ""
    if not data or (error and error.kind == "login"):
        message = str(error) if error else "Loading..."
    text, urgent = sessions_note(session_list)
    return {
        "state": "ready" if data else "error" if error else "starting",
        "bar": {"text": f"{round(headline.percent)}%" if headline else "",
                "severity": "stale" if error or not headline else headline.severity,
                "attention": attention_count(session_list),
                "tooltip": "\n".join(summary_lines(data, error, session_list, now) or [LOADING])},
        "note": note, "noteStrong": bool(error), "message": message,
        "limits": [limit_row(limit, error, now) for limit in (data.limits if data else [])],
        "breakdown": ("This week: " + ", ".join(f"{name} {round(pct)}%" for name, pct in data.breakdown)
                      if data and data.breakdown else ""),
        "sessionsNote": text, "sessionsUrgent": urgent,
        "sessions": [session_row(s, now) for s in session_list],
        "flash": flash,
    }
