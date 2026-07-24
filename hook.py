#!/usr/bin/env python3
"""Claude Code hook -> writes a session's status to a shared directory.

Invoked from ``~/.claude/settings.json`` on various Claude Code hook events.
The first CLI argument is the status to record:

    working | action | permission | done | end

The full hook payload (session id, cwd, event name, ...) is read as JSON from
stdin. Each session is written to ``~/.claude/session-status/<session_id>.json``;
the ``end`` status deletes that file so the session disappears from the widget.

This script must never fail in a way that blocks Claude Code, so every side
effect is wrapped in try/except and the process always exits 0.
"""
import sys
import os
import json
import time

STATUS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "session-status")


def read_stdin() -> str:
    """Read the hook payload from stdin as UTF-8 text.

    Claude Code sends UTF-8 JSON. On Windows the hook runs through PowerShell,
    which prepends a BOM to the piped stream, so we decode with ``utf-8-sig``
    to strip it. Reading via ``sys.stdin.buffer`` (bytes) avoids the process
    locale codepage mangling non-ASCII paths in ``cwd``.
    """
    try:
        if sys.stdin is not None and hasattr(sys.stdin, "buffer"):
            return sys.stdin.buffer.read().decode("utf-8-sig", "replace")
        if sys.stdin is not None:
            return sys.stdin.read()
    except Exception:
        pass
    return ""


def main() -> None:
    status = sys.argv[1] if len(sys.argv) > 1 else "working"

    raw = read_stdin().strip()
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}

    session_id = str(data.get("session_id") or "unknown")
    cwd = str(data.get("cwd") or "")
    event = str(data.get("hook_event_name") or "")

    os.makedirs(STATUS_DIR, exist_ok=True)
    path = os.path.join(STATUS_DIR, session_id + ".json")

    # End of session -> remove the file so it drops off the widget.
    if status == "end":
        try:
            os.remove(path)
        except OSError:
            pass
        return

    project = os.path.basename(cwd.rstrip("\\/")) if cwd else session_id[:8]
    if not project:
        project = session_id[:8]

    record = {
        "session_id": session_id,
        "cwd": cwd,
        "project": project,
        "status": status,
        "event": event,
        "updated_at": time.time(),
    }

    # Atomic write (temp + replace) so the widget never reads a half-written file.
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
