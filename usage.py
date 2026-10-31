"""Fetch and shape Claude plan usage (the numbers behind Settings > Usage in the Claude app).

Reads the OAuth token Claude Code keeps in ~/.claude/.credentials.json and calls the same
undocumented endpoint Claude Code's /usage uses. Read-only: the token is never refreshed
here, because rotating it would log Claude Code out. Running Claude Code refreshes it.
"""

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"

KIND_LABELS = {"session": "Session (5h)", "weekly_all": "Weekly (all models)"}
# Fallback when the response has no usable `limits` list (older shape).
WINDOW_LABELS = {
    "five_hour": "Session (5h)",
    "seven_day": "Weekly (all models)",
    "seven_day_opus": "Weekly (Opus)",
    "seven_day_sonnet": "Weekly (Sonnet)",
}
SESSION_KINDS = ("session", "five_hour")
WEEKLY_KINDS = ("weekly_all", "seven_day")
SEVERITY_ORDER = ["normal", "warning", "critical"]
WARNING_AT = 75
CRITICAL_AT = 90


class UsageError(Exception):
    """A fetch that produced no usable numbers. `kind` is login, rate_limited, network or format."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


@dataclass
class Limit:
    kind: str
    label: str
    percent: float
    resets_at: datetime | None
    severity: str


@dataclass
class Usage:
    limits: list
    breakdown: list  # (display_name, percent) pairs for this week, largest first
    fetched_at: datetime

    @property
    def session(self):
        return next((l for l in self.limits if l.kind in SESSION_KINDS), None)

    @property
    def headline(self):
        """The limit the tray icon shows: the 5-hour session, or the weekly one on a plan without a
        session limit, such as Ultra. The endpoint lists only the plan's limits (`"five_hour":
        null` and no session entry on Ultra), so this follows a plan change by itself."""
        weekly = next((l for l in self.limits if l.kind in WEEKLY_KINDS), self.limits[0])
        return self.session or weekly


def credentials_path():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    return Path(base) / ".credentials.json"


def read_token(path=None, now=None):
    path = path or credentials_path()
    try:
        oauth = json.loads(Path(path).read_text(encoding="utf-8"))["claudeAiOauth"]
        token, expires_ms = oauth["accessToken"], float(oauth.get("expiresAt") or 0)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise UsageError("login", f"No Claude Code login found ({e.__class__.__name__}).")
    if expires_ms and expires_ms / 1000 < (now or time.time()):
        raise UsageError("login", "Login expired. Run Claude Code once to refresh it.")
    return token


def fetch_raw(token, timeout=15):
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "Accept": "application/json",
        "User-Agent": "claude-usage-widget/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise UsageError("login", "Login rejected. Run Claude Code once to refresh it.")
        if e.code == 429:
            raise UsageError("rate_limited", "Usage endpoint rate limited; backing off.")
        raise UsageError("network", f"HTTP {e.code} from the usage endpoint.")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UsageError("network", f"Offline or unreachable ({e.__class__.__name__}).")
    except ValueError:
        raise UsageError("format", "The usage endpoint sent something that isn't JSON.")


def fetch_usage():
    return parse_usage(fetch_raw(read_token()))


def severity_for(percent, api_severity=None):
    by_threshold = "critical" if percent >= CRITICAL_AT else "warning" if percent >= WARNING_AT else "normal"
    if api_severity and api_severity != "normal":
        # Unknown non-normal values from the API still count as at least a warning.
        api = api_severity if api_severity in SEVERITY_ORDER else "warning"
        return max(by_threshold, api, key=SEVERITY_ORDER.index)
    return by_threshold


def parse_time(value):
    """ISO time rounded to the minute: the API sends one reset as 06:59:59.586 and another as
    07:00:00, which would otherwise show as 5:59 pm and 6:00 pm side by side."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt + timedelta(seconds=30)).replace(second=0, microsecond=0)


def display_name(value):
    if isinstance(value, dict):
        return value.get("display_name")
    return value if isinstance(value, str) else None


def limit_label(item):
    kind = item.get("kind") or "limit"
    scope = item.get("scope")
    scope = scope if isinstance(scope, dict) else {}
    scoped_name = display_name(scope.get("model")) or display_name(scope.get("surface"))
    if scoped_name:
        prefix = "Weekly" if item.get("group") == "weekly" else kind.replace("_", " ").capitalize()
        return f"{prefix} ({scoped_name})"
    return KIND_LABELS.get(kind, kind.replace("_", " ").capitalize())


def parse_limit(item):
    percent = float(item.get("percent") or 0)
    return Limit(str(item.get("kind") or "limit"), limit_label(item), percent,
                 parse_time(item.get("resets_at")), severity_for(percent, item.get("severity")))


def parse_usage(data, now=None):
    """Skips any limit it can't read rather than failing the whole fetch: the endpoint is
    undocumented, so a new field shape must not blank the widget."""
    if not isinstance(data, dict):
        raise UsageError("format", "The usage endpoint sent an unexpected shape.")
    limits = []
    for item in data.get("limits") if isinstance(data.get("limits"), list) else []:
        try:
            limits.append(parse_limit(item))
        except (AttributeError, TypeError, ValueError):
            continue
    if not limits:
        for key, label in WINDOW_LABELS.items():
            window = data.get(key)
            try:
                percent = float(window["utilization"])
            except (KeyError, TypeError, ValueError):
                continue
            limits.append(Limit(key, label, percent, parse_time(window.get("resets_at")), severity_for(percent)))
    if not limits:
        raise UsageError("format", "No usage numbers in the response.")

    breakdown = []
    rows = data.get("seven_day_breakdown")
    rows = rows.get("rows") if isinstance(rows, dict) else None
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        name, percent = r.get("display_name") or r.get("key"), r.get("percent")
        if isinstance(name, str) and isinstance(percent, (int, float)) and percent > 0:
            breakdown.append((name, percent))
    breakdown.sort(key=lambda r: -r[1])
    return Usage(limits, breakdown, now or datetime.now(timezone.utc))


def format_clock(dt):
    local = dt.astimezone()
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'am' if local.hour < 12 else 'pm'}"


def format_duration(seconds):
    minutes = max(0, int(seconds // 60))
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def format_reset(resets_at, now=None):
    if resets_at is None:
        return ""
    now = now or datetime.now(timezone.utc)
    left = (resets_at - now).total_seconds()
    if left <= 0:
        return "Resetting now"
    if left < 86400:
        return f"Resets in {format_duration(left)} ({format_clock(resets_at)})"
    return f"Resets {resets_at.astimezone():%a} {format_clock(resets_at)} (in {format_duration(left)})"


def format_age(fetched_at, now=None):
    seconds = ((now or datetime.now(timezone.utc)) - fetched_at).total_seconds()
    return "just now" if seconds < 60 else f"{format_duration(seconds)} ago"
