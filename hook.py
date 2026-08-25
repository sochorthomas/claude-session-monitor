#!/usr/bin/env python3
"""Claude Code hook -> writes a session's status to a shared directory.

Invoked from ``~/.claude/settings.json`` on various Claude Code hook events.
The first CLI argument is the status to record:

    working | action | permission | notify | done | end

``notify`` is resolved to ``permission`` or ``action`` from the payload, because
Claude Code's ``Notification`` event covers both asking for approval and simply
having gone quiet. The full hook payload (session id, cwd, event name, ...) is
read as JSON from stdin. Each session is written to
``~/.claude/session-status/<session_id>.json``; the ``end`` status deletes that
file so the session disappears from the widget.

This runs on every tool call, so it is kept cheap: no subprocesses, and the
owning ``claude`` process is found by walking the parent chain directly rather
than enumerating every process on the machine.

It must also never fail in a way that blocks Claude Code, so every side effect
is wrapped in try/except and the process always exits 0. Set
``CLAUDE_MONITOR_DEBUG=1`` to have it record what it did in
``~/.claude/session-monitor-hook.log``, which is the only way to tell a hook
that ran and failed from one Claude Code never managed to start.
"""
import sys
import os
import json
import time
import ctypes
from ctypes import wintypes

# The override exists so install.ps1 can smoke-test the hook against a
# throwaway directory instead of the one live sessions are writing to.
# ``widget.pyw`` reads the same variable, so setting it permanently just moves
# the status files somewhere else.
STATUS_DIR = (os.environ.get("CLAUDE_MONITOR_STATUS_DIR")
              or os.path.join(os.path.expanduser("~"), ".claude", "session-status"))

# Opt-in diagnostics. This hook must never break Claude Code, so it swallows
# every error and always exits 0 - which means a broken install looks exactly
# like "no sessions running". Set CLAUDE_MONITOR_DEBUG=1 to leave a trail: a
# line per invocation says the hook ran, and the *absence* of one says Claude
# Code never got as far as starting it (a hook command it cannot parse, say).
DEBUG = bool(os.environ.get("CLAUDE_MONITOR_DEBUG"))
LOG_PATH = os.path.join(os.path.expanduser("~"), ".claude", "session-monitor-hook.log")
LOG_MAX_BYTES = 1 << 20

MAX_ANCESTRY_DEPTH = 8
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

# Prototypes matter for correctness, not speed: without an explicit restype
# ctypes assumes c_int and truncates the 64-bit HANDLE that OpenProcess
# returns. (Windows hands out small handle values, so the bug stays latent -
# which is exactly why it is worth pinning down.)
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
_k32.QueryFullProcessImageNameW.restype = wintypes.BOOL


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    """Only ``InheritedFromUniqueProcessId`` is of interest here.

    The pointer-sized fields are declared as ``c_void_p`` so the layout is
    correct on 64-bit, where ``ULONG_PTR`` is 8 bytes.
    """
    _fields_ = [
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
    ]


_ntdll.NtQueryInformationProcess.argtypes = [
    wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
    ctypes.POINTER(ctypes.c_ulong),
]
_ntdll.NtQueryInformationProcess.restype = ctypes.c_long


def debug_log(message: str) -> None:
    """Append one line to the debug log. A no-op unless ``DEBUG`` is set.

    Starts the file over once it passes ``LOG_MAX_BYTES`` instead of rotating:
    a diagnostic log is read right after reproducing the problem, so the recent
    lines are the only ones worth keeping, and an unbounded file on a hook that
    runs per tool call is a worse failure than a lost history.
    """
    if not DEBUG:
        return
    try:
        mode = "a"
        try:
            if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                mode = "w"
        except OSError:
            pass
        with open(LOG_PATH, mode, encoding="utf-8") as f:
            f.write("%s pid=%d %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                        os.getpid(), message))
    except Exception:
        pass


def parent_and_name(pid: int):
    """Return ``(parent_pid, exe_name_lower)`` for ``pid``, or ``(0, "")``.

    Two queries against one handle: ``NtQueryInformationProcess`` for the
    parent (Win32 has no GetParentProcessId) and ``QueryFullProcessImageNameW``
    for the name. Both need only PROCESS_QUERY_LIMITED_INFORMATION, which a
    process gets for anything running as the same user.
    """
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0, ""
    try:
        ppid = 0
        info = _PROCESS_BASIC_INFORMATION()
        written = ctypes.c_ulong()
        # 0 = ProcessBasicInformation
        if _ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(info),
                                           ctypes.sizeof(info),
                                           ctypes.byref(written)) == 0:
            ppid = int(info.InheritedFromUniqueProcessId or 0)

        name = ""
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            name = os.path.basename(buf.value).lower()
        return ppid, name
    finally:
        _k32.CloseHandle(handle)


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
_k32.Process32First.restype = wintypes.BOOL
_k32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
_k32.Process32Next.restype = wintypes.BOOL

_INVALID_HANDLE = wintypes.HANDLE(-1).value


def process_table() -> dict:
    """Return ``{pid: (parent_pid, exe_name_lower)}`` for all processes.

    Only used as a fallback: a snapshot needs no per-process access rights, so
    it still works when a link in the chain cannot be opened (an elevated
    ``claude`` above a non-elevated hook, say). Decoding ~360 entries in a
    Python loop measured 0.4-1.4 s on a normal machine, against under a
    millisecond for the parent-chain walk, so it is far off the normal path.
    """
    table = {}
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        return table
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        ok = _k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.decode("mbcs", "replace").lower()
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), name)
            ok = _k32.Process32Next(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return table


def _walk(first_parent, name_of):
    """Walk up the parent chain, returning ``(ancestors, claude_pid)``.

    ``first_parent(pid)`` yields the parent of ``pid``; ``name_of(pid)`` its
    executable name. Bounded by ``MAX_ANCESTRY_DEPTH`` and a seen-set so a
    recycled pid cannot produce a loop.
    """
    ancestors = []
    claude_pid = None
    pid = os.getpid()
    seen = {pid}
    for _ in range(MAX_ANCESTRY_DEPTH):
        ppid = first_parent(pid)
        if not ppid or ppid <= 0 or ppid in seen:
            break
        seen.add(ppid)
        ancestors.append(ppid)
        if claude_pid is None and name_of(ppid).startswith("claude"):
            claude_pid = ppid
        pid = ppid
    return ancestors, claude_pid


def claude_ancestry():
    """Return ``(ancestors, claude_pid)`` for the session that invoked us.

    The hook runs as ``claude.exe -> <shell> -> python.exe``, so the owning
    session is the nearest ``claude*`` ancestor.

    ``claude_pid`` is what lets the widget tell a live session from an orphaned
    status file: one Claude process runs exactly one session, so two status
    files sharing a pid mean Claude Code rotated the session id and left the
    old file behind. ``ancestors`` is used when focusing a session's window.
    """
    # Fast path: one OpenProcess per level, so ~3 calls instead of a scan of
    # every process on the machine.
    try:
        cache = {}

        def info(pid):
            if pid not in cache:
                cache[pid] = parent_and_name(pid)
            return cache[pid]

        ancestors, claude_pid = _walk(lambda p: info(p)[0], lambda p: info(p)[1])
        if claude_pid is not None:
            return ancestors, claude_pid
    except Exception:
        ancestors = []

    # Fallback: a snapshot sees processes we may not be allowed to open.
    try:
        table = process_table()
        if not table:
            return ancestors, None
        return _walk(lambda p: table.get(p, (0, ""))[0],
                     lambda p: table.get(p, (0, ""))[1])
    except Exception:
        return ancestors, None


def read_stdin() -> str:
    """Read the hook payload from stdin as UTF-8 text.

    Claude Code sends UTF-8 JSON. On Windows the hook is launched through a
    shell, which may prepend a BOM to the piped stream, so we decode with
    ``utf-8-sig`` to strip it. Reading via ``sys.stdin.buffer`` (bytes) avoids
    the process locale codepage mangling non-ASCII paths in ``cwd``.
    """
    try:
        if sys.stdin is not None and hasattr(sys.stdin, "buffer"):
            return sys.stdin.buffer.read().decode("utf-8-sig", "replace")
        if sys.stdin is not None:
            return sys.stdin.read()
    except Exception:
        pass
    return ""


def resolve_status(status: str, data: dict) -> str:
    """Turn the ``notify`` placeholder into a concrete status.

    Claude Code's ``Notification`` event fires both when a tool needs approval
    and when the session has simply been idle for a while, so the event name
    alone cannot tell the two apart - the payload's ``message`` can.
    """
    if status != "notify":
        return status
    message = str(data.get("message") or "").lower()
    if "permission" in message or "approve" in message:
        return "permission"
    return "action"


def main() -> None:
    status = sys.argv[1] if len(sys.argv) > 1 else "working"

    raw = read_stdin().strip()
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        debug_log("unparseable payload (%d bytes): %.200r" % (len(raw), raw))
        data = {}
    if not isinstance(data, dict):
        data = {}

    session_id = str(data.get("session_id") or "unknown")
    cwd = str(data.get("cwd") or "")
    event = str(data.get("hook_event_name") or "")
    transcript = str(data.get("transcript_path") or "")

    os.makedirs(STATUS_DIR, exist_ok=True)
    path = os.path.join(STATUS_DIR, session_id + ".json")

    # End of session -> remove the file so it drops off the widget.
    if status == "end":
        try:
            os.remove(path)
        except OSError:
            pass
        debug_log("end session=%s" % session_id)
        return

    status = resolve_status(status, data)

    project = os.path.basename(cwd.rstrip("\\/")) if cwd else session_id[:8]
    if not project:
        project = session_id[:8]

    ancestors, claude_pid = claude_ancestry()

    record = {
        "session_id": session_id,
        "cwd": cwd,
        "project": project,
        "status": status,
        "event": event,
        "transcript_path": transcript,
        "ancestors": ancestors,
        "claude_pid": claude_pid,
        "updated_at": time.time(),
    }

    # Atomic write (temp + replace) so the widget never reads a half-written file.
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except Exception as exc:
        debug_log("write failed for %s: %r" % (path, exc))
        return

    debug_log("%s -> %s session=%s claude_pid=%s project=%s"
              % (event or "?", status, session_id, claude_pid, project))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            import traceback
            debug_log("crash:\n" + traceback.format_exc())
        except Exception:
            pass
    sys.exit(0)
