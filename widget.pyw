#!/usr/bin/env pythonw
"""Floating always-on-top box that shows all running Claude Code sessions.

Reads ``~/.claude/session-status/*.json`` (written by ``hook.py``) twice a
second and renders one row per session with a color-coded status:

    Permission (blue)  - Claude is blocked, waiting for you to approve a tool
    Action!    (amber) - Claude finished / asked a question, waiting for a reply
    Working    (green) - Claude is actively working

Features:
  - frameless, always-on-top, draggable by the header
  - remembers where you put it and how wide you made it
  - drag the right edge to change the width
  - a stale status (one nothing has refreshed for a while) is dimmed and dated
  - click a row to bring that session's terminal/editor window to the front
  - optional sound when a session starts waiting for you (toggle with the note)
  - collapse to a thin strip (chevron in the header)
  - right-click for a small menu
  - one instance only, and laid out for the display's DPI

Windows only (uses Win32 APIs via ctypes and ``winsound``).
"""
import os
import re
import sys
import json
import math
import time
import threading
import ctypes
from ctypes import wintypes
import winsound
import tkinter as tk
import tkinter.font as tkfont

# ---------------------------------------------------------------------------
# DPI. This has to happen before Tk starts, so it sits above everything else.
# ---------------------------------------------------------------------------
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_gdi32 = ctypes.windll.gdi32
# A second handle to kernel32, opened so ctypes preserves GetLastError across
# the call. Only the single-instance check needs it; ctypes.windll does not.
_k32err = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.ReleaseDC.restype = ctypes.c_int
_gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
_gdi32.GetDeviceCaps.restype = ctypes.c_int

LOGPIXELSY = 90


def enable_dpi_awareness() -> None:
    """Tell Windows we lay out in real pixels, before Tk reads the DPI.

    Without this a display at 125% gets a widget laid out for 96 DPI and then
    bitmap-stretched by Windows: blurry text in a box the wrong size. Tries
    per-monitor v2 first and falls back through the older APIs for the Windows
    versions that don't have it - each is simply absent rather than failing, so
    a missing one raises AttributeError.
    """
    try:
        _user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        _user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # SYSTEM_DPI_AWARE
        return
    except (AttributeError, OSError):
        pass
    try:
        _user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def dpi_scale() -> float:
    """Real pixels this display draws per layout pixel.

    ``CLAUDE_MONITOR_SCALE`` overrides it, both for a display where the
    automatic value isn't the size you want the box to be and to exercise the
    scaled layout on a machine that runs at 96 DPI.
    """
    override = os.environ.get("CLAUDE_MONITOR_SCALE")
    if override:
        try:
            return max(0.5, min(4.0, float(override)))
        except ValueError:
            pass
    try:
        dc = _user32.GetDC(None)
        if dc:
            try:
                dpi = _gdi32.GetDeviceCaps(dc, LOGPIXELSY)
            finally:
                _user32.ReleaseDC(None, dc)
            if dpi > 0:
                return max(1.0, dpi / 96.0)
    except OSError:
        pass
    return 1.0


enable_dpi_awareness()
SCALE = dpi_scale()


def px(value) -> int:
    """A layout measurement in the real pixels of this display."""
    return max(1, int(round(value * SCALE)))


HOME = os.path.expanduser("~")
# The override exists so a test can point the hook and the widget at a throwaway
# directory instead of the one a live session is writing to (see install.ps1).
STATUS_DIR = (os.environ.get("CLAUDE_MONITOR_STATUS_DIR")
              or os.path.join(HOME, ".claude", "session-status"))
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
CONFIG_PATH = os.path.join(HOME, ".claude", "session-monitor-config.json")

# --- Behavior ---------------------------------------------------------------
REFRESH_MS = 500        # how often to re-read the status files (a poll of the
                        # status dir measures ~0.15 ms, so this is free and
                        # halves how long a change waits to be shown)
BLINK_MS = 650          # blink interval for attention states
MAX_AGE_SEC = 24 * 3600  # drop sessions older than this (crash safety net)
GRACE_NEW = 25          # don't clean up a session in its first N seconds
MISSING_DWELL = 6       # a project window must be gone N s before we remove it
GHOST_AFTER = 180       # a session with no transcript is an orphan after N s
DUP_DELETE_AFTER = 60   # a shadowed duplicate's file is deleted after N s
STALE_AFTER = 120       # a status nothing refreshed for N s is shown as stale

# --- Layout -----------------------------------------------------------------
# Everything here is in 96-DPI pixels and put through px(), so the box keeps its
# proportions on a scaled display instead of growing text out of a fixed frame.
MARGIN = px(14)
HEADER_H = px(30)
DEFAULT_WIDTH = px(250)
MIN_WIDTH = px(170)     # narrower than this and the status label owns the row
MAX_WIDTH = px(640)
GRIP_W = px(5)          # width of the drag-to-resize strip on the right edge

# Row paddings. Kept as constants because the name column is measured against
# them to decide where to cut a too-long project name.
ROW_PAD_LEFT = px(10)   # left of the status dot
ROW_DOT_GAP = px(8)     # dot -> name
ROW_NAME_GAP = px(8)    # name -> age -> status
ROW_PAD_RIGHT = px(12)  # right of the status label
ROW_PAD_Y = px(5)       # above and below a row
ELLIPSIS = "…"
EYE = "\U0001F441"      # header counter icon ("sessions watching you back")

# --- Dark color scheme ------------------------------------------------------
BG = "#0d1117"
BG_HEADER = "#161b22"
BG_HOVER = "#1c2432"
BORDER = "#30363d"
FG = "#e6edf3"
FG_DIM = "#8b949e"
ACCENT = "#58a6ff"
FADE = 0.55             # how far a stale row's colors are pulled toward BG

# Status metadata. ``attn`` = needs the user -> blinks, plays a sound on entry,
# and sorts to the top (lower ``prio`` = higher in the list).
STATUS_META = {
    "permission": {"label": "Permission!", "color": "#58a6ff", "prio": 0, "attn": True},
    "action":     {"label": "Action!",     "color": "#e3b341", "prio": 1, "attn": True},
    "working":    {"label": "Working",     "color": "#3fb950", "prio": 2, "attn": False},
    "done":       {"label": "Done",        "color": "#6e7681", "prio": 3, "attn": False},
}
UNKNOWN = {"label": "?", "color": FG_DIM, "prio": 4, "attn": False}


def blend(color: str, other: str, t: float) -> str:
    """Mix two ``#rrggbb`` colors: ``t=0`` gives ``color``, ``t=1`` ``other``.

    Tk has no per-widget opacity, so fading a row means computing the faded
    color rather than turning anything translucent.
    """
    if t <= 0:
        return color
    a, b = int(color[1:], 16), int(other[1:], 16)
    out = 0
    for shift in (16, 8, 0):
        ca, cb = (a >> shift) & 0xFF, (b >> shift) & 0xFF
        out |= int(round(ca + (cb - ca) * t)) << shift
    return "#%06x" % out


def format_age(seconds: float) -> str:
    """Compact age of a status that has not been refreshed: ``90s``, ``4m``, ``2h``."""
    if seconds < 60:
        return "%ds" % int(seconds)
    if seconds < 3600:
        return "%dm" % int(seconds // 60)
    return "%dh" % int(seconds // 3600)


def clamp_width(value) -> int:
    """Coerce a stored or dragged width into the allowed range."""
    try:
        width = int(value)
    except (TypeError, ValueError):
        return DEFAULT_WIDTH
    return max(MIN_WIDTH, min(MAX_WIDTH, width))


def fit_text(text: str, font, max_px: int) -> str:
    """Shorten ``text`` to ``max_px`` pixels, ending with an ellipsis.

    Tk labels have no text-overflow, so a long project name would otherwise be
    clipped mid-letter at the window edge with no hint that anything is missing.
    """
    if max_px <= 0:
        return ELLIPSIS
    if font.measure(text) <= max_px:
        return text
    room = max_px - font.measure(ELLIPSIS)
    if room <= 0:
        return ELLIPSIS
    kept = ""
    for ch in text:
        if font.measure(kept + ch) > room:
            break
        kept += ch
    return kept.rstrip() + ELLIPSIS


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def update_config(**values) -> None:
    """Merge ``values`` into the config file.

    A plain overwrite would be enough for a single setting, but the sound
    toggle, the position and the width are each saved from a different place -
    and whichever wrote last would drop the other two.
    """
    cfg = load_config()
    cfg.update(values)
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception:
        pass


def play_alert() -> None:
    """Play a cheerful ascending two-note chime (G5 -> C6) off the UI thread."""
    def run():
        try:
            winsound.Beep(784, 90)    # G5
            winsound.Beep(1047, 130)  # C6
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Win32 helpers: find and focus the terminal/editor window of a session.
# ---------------------------------------------------------------------------
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# Anything returning a HANDLE or HWND needs an explicit restype: ctypes defaults
# to c_int and would truncate the upper 32 bits on 64-bit Windows. The values
# Windows hands out are small enough that it works anyway, which is what makes
# this worth pinning down rather than leaving to luck.
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                          ctypes.c_void_p, wintypes.UINT]
_user32.SystemParametersInfoW.restype = wintypes.BOOL

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SPI_GETWORKAREA = 0x0030


class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

# Process names treated as a terminal/editor when matching a window by title.
_TERM_PROC = {
    "code.exe", "code - insiders.exe", "windowsterminal.exe", "wt.exe",
    "powershell.exe", "pwsh.exe", "cmd.exe", "conhost.exe",
    "alacritty.exe", "wezterm-gui.exe", "hyper.exe", "cursor.exe",
    "windsurf.exe", "openconsole.exe",
}


def virtual_screen():
    """Bounds of the whole desktop as ``(x, y, w, h)``.

    Tk's ``winfo_screenwidth`` only knows the primary monitor, so it would call
    a perfectly good saved position on a second screen out of bounds - and it
    has no way at all to express the negative coordinates of a monitor placed
    to the left of the primary one.
    """
    g = _user32.GetSystemMetrics
    return (g(SM_XVIRTUALSCREEN), g(SM_YVIRTUALSCREEN),
            g(SM_CXVIRTUALSCREEN), g(SM_CYVIRTUALSCREEN))


def work_area():
    """Primary monitor minus the taskbar, as ``(left, top, right, bottom)``.

    Replaces a hardcoded taskbar height, which was 8px out on the machine this
    was written on and is wrong by a lot more whenever the taskbar is moved to
    the side, auto-hidden, or drawn at a different scale.
    """
    rect = _RECT()
    if _user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)


_instance_mutex = None
MUTEX_NAME = "Local\\ClaudeSessionMonitor.Widget"


def claim_single_instance() -> bool:
    """False if another widget already holds the lock.

    A named mutex rather than a pid file: Windows releases it however the
    process dies, so a crash can never leave a stale lock that stops the widget
    from starting again. ``Local\\`` scopes it to the logon session, so two
    users sharing a machine get one widget each.

    It matters more than it used to. Two widgets used to at least be visibly
    two; now that the position is remembered they land exactly on top of each
    other, so a Startup shortcut plus one impatient double-click looks like a
    box that will not close.
    """
    global _instance_mutex
    ERROR_ALREADY_EXISTS = 183
    try:
        _k32err.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                         wintypes.LPCWSTR]
        _k32err.CreateMutexW.restype = wintypes.HANDLE
        handle = _k32err.CreateMutexW(None, True, MUTEX_NAME)
    except OSError:
        return True                      # cannot tell -> let it run
    if not handle:
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return False
    _instance_mutex = handle             # held for the life of the process
    return True


def _proc_name(pid: int) -> str:
    """Return the executable name for a PID, or "" if it cannot be queried."""
    try:
        h = _kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        ok = _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        _kernel32.CloseHandle(h)
        return os.path.basename(buf.value) if ok else ""
    except Exception:
        return ""


def _enum_titled_windows():
    """Return [(hwnd, pid, title)] for every visible window with a title."""
    out = []

    def cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        wpid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        out.append((hwnd, wpid.value, buf.value))
        return True

    _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return out


def _focus_hwnd(hwnd) -> None:
    """Bring a window to the foreground, restoring it only if minimized.

    We restore only when the window is minimized so a maximized (full-screen)
    window stays maximized instead of being shrunk to a normal window. The
    AttachThreadInput dance is needed to bypass Windows' foreground-lock.
    """
    SW_RESTORE = 9
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)
    fg = _user32.GetForegroundWindow()
    tid_fg = _user32.GetWindowThreadProcessId(fg, None)
    tid_tg = _user32.GetWindowThreadProcessId(hwnd, None)
    cur = _kernel32.GetCurrentThreadId()
    _user32.AttachThreadInput(cur, tid_fg, True)
    _user32.AttachThreadInput(cur, tid_tg, True)
    _user32.BringWindowToTop(hwnd)
    _user32.SetForegroundWindow(hwnd)
    _user32.AttachThreadInput(cur, tid_fg, False)
    _user32.AttachThreadInput(cur, tid_tg, False)


def focus_session(ancestors, project: str = "") -> bool:
    """Find and focus the terminal/editor window of a session.

    Each visible window is scored:
      +2  it belongs to one of the session's ancestor PIDs (from hook.py)
      +3  its title contains the project name AND it is a terminal/editor
          (this disambiguates the right VS Code window - all VS Code windows
          share one PID - and excludes e.g. a browser tab with the same name)
    The highest-scoring window is focused. Returns True on success.
    """
    anc = set()
    for a in ancestors or []:
        try:
            anc.add(int(a))
        except (TypeError, ValueError):
            pass
    proj = (project or "").strip().lower()

    name_cache = {}

    def is_term(pid):
        if pid not in name_cache:
            name_cache[pid] = _proc_name(pid).lower()
        return name_cache[pid] in _TERM_PROC

    best = None
    best_score = 0
    for hwnd, pid, title in _enum_titled_windows():
        score = 0
        if pid in anc:
            score += 2
        if proj and proj in title.lower() and is_term(pid):
            score += 3
        if score > best_score:
            best_score = score
            best = hwnd

    if best is None:
        return False
    try:
        _focus_hwnd(best)
        return True
    except Exception:
        return False


def open_editor_titles():
    """Return lowercase titles of all open terminal/editor windows.

    Used to detect a closed session: if no editor window still has the project
    name in its title, the session is gone. This is more reliable than the
    ``SessionEnd`` hook (which does not run on a hard window close) and works
    around VS Code sharing a single PID across all its windows.
    """
    titles = []
    for _hwnd, pid, title in _enum_titled_windows():
        try:
            if _proc_name(pid).lower() in _TERM_PROC:
                titles.append(title.lower())
        except Exception:
            pass
    return titles


def remove_status(session_id) -> None:
    """Delete one session's status file (it is gone or superseded)."""
    try:
        os.remove(os.path.join(STATUS_DIR, str(session_id) + ".json"))
    except OSError:
        pass


def is_claude_pid(pid, cache=None) -> bool:
    """True if ``pid`` is still a running Claude Code process.

    The name is checked as well as mere existence, so a pid that Windows has
    recycled onto an unrelated process doesn't keep a dead session alive.
    """
    if cache is None:
        cache = {}
    if pid not in cache:
        cache[pid] = _proc_name(pid).lower()
    return cache[pid].startswith("claude")


_projects_cache = {"at": 0.0, "names": []}


def _project_dirs():
    """Cached listing of ``~/.claude/projects`` (one folder per project)."""
    now = time.time()
    if now - _projects_cache["at"] > 10:
        try:
            _projects_cache["names"] = os.listdir(PROJECTS_DIR)
        except OSError:
            _projects_cache["names"] = []
        _projects_cache["at"] = now
    return _projects_cache["names"]


def has_transcript(rec) -> bool:
    """True if this session has a transcript file on disk.

    Claude Code writes ``<session_id>.jsonl`` as soon as a session has a real
    turn, so a status file with no transcript belongs to a session id that was
    announced and then abandoned. ``hook.py`` records ``transcript_path``; for
    records written by an older hook we rebuild the path from ``cwd`` the way
    Claude Code slugifies it (``c:\\xampp\\htdocs\\x`` -> ``c--xampp-htdocs-x``).
    """
    path = rec.get("transcript_path")
    if path:
        return os.path.exists(path)
    cwd = rec.get("cwd") or ""
    sid = rec.get("session_id") or ""
    if not cwd or not sid:
        return True                      # too little info -> never call it an orphan
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    for name in {slug, slug.lower()}:
        if os.path.exists(os.path.join(PROJECTS_DIR, name, sid + ".jsonl")):
            return True
    low = slug.lower()
    for name in _project_dirs():         # project folder cased differently
        if name.lower() == low:
            return os.path.exists(os.path.join(PROJECTS_DIR, name, sid + ".jsonl"))
    return False


def prune_orphans(records):
    """Collapse the status files that produce duplicate rows for one window.

    Two things leave a stale file behind, both without a ``SessionEnd``:

    * Claude Code fires ``SessionStart`` under one session id and then runs the
      conversation under a different one. The first id never receives another
      hook and never gets a transcript.
    * A session id is rotated mid-flight (resume/compact), leaving a stale file
      that *does* have a transcript.

    The stale file keeps the project name of a window that is still open, so the
    window-title cleanup in ``Monitor.cleanup_closed`` can never catch it. These
    two rules do: no transcript and no recent hook -> orphan; and one Claude
    process runs one session, so per ``claude_pid`` only the most recently
    updated record survives.
    """
    now = time.time()
    live = []
    for rec in records:
        if now - rec.get("_updated", 0) > GHOST_AFTER and not has_transcript(rec):
            remove_status(rec.get("session_id"))
            continue
        live.append(rec)

    out = []
    by_pid = {}
    for rec in live:
        pid = rec.get("claude_pid")
        if isinstance(pid, int) and pid > 0:
            by_pid.setdefault(pid, []).append(rec)
        else:
            out.append(rec)              # older hook.py -> no pid recorded
    for group in by_pid.values():
        group.sort(key=lambda r: r.get("_updated", 0), reverse=True)
        out.append(group[0])
        # Hide the shadowed ones immediately, but delete only once they have
        # gone quiet, so we never race a session that is still writing.
        for rec in group[1:]:
            if now - rec.get("_updated", 0) > DUP_DELETE_AFTER:
                remove_status(rec.get("session_id"))
    return out


def load_sessions():
    """Read all status files, dropping ones older than ``MAX_AGE_SEC``.

    Returns the records sorted by status priority (attention first) then by
    most-recently-updated, with orphaned duplicates already pruned.
    """
    out = []
    now = time.time()
    try:
        names = os.listdir(STATUS_DIR)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or name.endswith(".tmp"):
            continue
        path = os.path.join(STATUS_DIR, name)
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                rec = json.load(f)
        except Exception:
            continue
        updated = float(rec.get("updated_at") or 0)
        if now - updated > MAX_AGE_SEC:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        rec["_updated"] = updated
        out.append(rec)

    out = prune_orphans(out)

    def key(r):
        st = STATUS_META.get(r.get("status"), UNKNOWN)
        return (st["prio"], -r.get("_updated", 0))

    out.sort(key=key)
    return out


# ---------------------------------------------------------------------------
# Notification-area ("system tray") icon.
# ---------------------------------------------------------------------------
_shell32 = ctypes.windll.shell32

WM_APP = 0x8000
TRAY_CALLBACK = WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
WM_CLOSE = 0x0010
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
SM_CXSMICON = 49
TRAY_CLASS = "ClaudeSessionMonitorTray"

_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", ctypes.c_wchar * 128), ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD), ("szInfo", ctypes.c_wchar * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", ctypes.c_wchar * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HICON)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


_user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
_user32.RegisterClassW.restype = wintypes.ATOM
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = _LRESULT
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.DestroyWindow.restype = wintypes.BOOL
_user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
_user32.RegisterWindowMessageW.restype = wintypes.UINT
_user32.CreateIconIndirect.argtypes = [ctypes.POINTER(_ICONINFO)]
_user32.CreateIconIndirect.restype = wintypes.HICON
_user32.DestroyIcon.argtypes = [wintypes.HICON]
_user32.DestroyIcon.restype = wintypes.BOOL
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.GetCursorPos.restype = wintypes.BOOL
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(_BITMAPINFOHEADER), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
_gdi32.CreateDIBSection.restype = wintypes.HBITMAP
_gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT,
                                wintypes.UINT, ctypes.c_void_p]
_gdi32.CreateBitmap.restype = wintypes.HBITMAP
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteObject.restype = wintypes.BOOL
_shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                       ctypes.POINTER(_NOTIFYICONDATAW)]
_shell32.Shell_NotifyIconW.restype = wintypes.BOOL


def make_dot_icon(color: str, size: int):
    """A filled circle in ``color`` as an HICON, or ``None`` if GDI refuses.

    Drawn by hand into a 32-bit DIB rather than shipped as .ico files: there is
    one icon per status and the tray asks for a different size at every display
    scale, so generating it is both less to carry and always the right size.
    Coverage is computed per pixel so the edge stays smooth at 16px, and the
    colour is premultiplied by it, which is what an alpha bitmap handed to
    CreateIconIndirect has to be.
    """
    red, green, blue = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    centre = (size - 1) / 2.0
    radius = size / 2.0 - 0.75
    pixels = (ctypes.c_uint32 * (size * size))()
    for y in range(size):
        for x in range(size):
            cov = radius + 0.5 - math.hypot(x - centre, y - centre)
            cov = min(1.0, max(0.0, cov))
            pixels[y * size + x] = ((int(255 * cov) << 24)
                                    | (int(red * cov) << 16)
                                    | (int(green * cov) << 8)
                                    | int(blue * cov))

    head = _BITMAPINFOHEADER()
    head.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    head.biWidth = size
    head.biHeight = -size            # negative = top-down, matching the loop
    head.biPlanes = 1
    head.biBitCount = 32
    head.biCompression = 0           # BI_RGB
    bits = ctypes.c_void_p()
    colour_bmp = _gdi32.CreateDIBSection(None, ctypes.byref(head), 0,
                                         ctypes.byref(bits), None, 0)
    if not colour_bmp:
        return None
    ctypes.memmove(bits, pixels, ctypes.sizeof(pixels))

    # An all-zero AND mask means "take every pixel from the colour bitmap", so
    # the alpha channel above is what actually shapes the circle. Rows of a
    # 1bpp bitmap are WORD-aligned.
    mask_bytes = ((size + 15) // 16) * 2 * size
    mask_bmp = _gdi32.CreateBitmap(size, size, 1, 1,
                                   (ctypes.c_ubyte * mask_bytes)())
    info = _ICONINFO(True, 0, 0, mask_bmp, colour_bmp)
    icon = _user32.CreateIconIndirect(ctypes.byref(info))
    _gdi32.DeleteObject(colour_bmp)
    _gdi32.DeleteObject(mask_bmp)
    return icon or None


class Tray:
    """A notification-area icon mirroring the aggregate status of the box.

    Windows delivers tray clicks as window messages, so this owns a hidden
    window to receive them. It deliberately does not run its own message loop:
    Tk pumps the whole thread's message queue, so the callbacks arrive on the
    Tk thread and can touch the widget directly, with no second thread and
    nothing to lock.

    Every failure is soft. A tray icon is a convenience, and a machine where
    the shell refuses one should still get the floating box.
    """

    def __init__(self, monitor):
        self.monitor = monitor
        self.hwnd = None
        self.icon = None
        self.shown = False
        self.state = None                       # last (color, tip) pushed
        self.size = _user32.GetSystemMetrics(SM_CXSMICON) or px(16)
        self._proc = _WNDPROC(self._wndproc)    # must outlive the window
        # Explorer restarting takes every tray icon with it and then broadcasts
        # this, which is the only chance to put ours back.
        self._taskbar_created = _user32.RegisterWindowMessageW("TaskbarCreated")
        try:
            self._create_window()
        except Exception:
            self.hwnd = None

    def _create_window(self):
        module = _kernel32.GetModuleHandleW(None)
        cls = _WNDCLASSW()
        cls.lpfnWndProc = self._proc
        cls.hInstance = module
        cls.lpszClassName = TRAY_CLASS
        self._cls = cls                         # keep the class struct alive
        _user32.RegisterClassW(ctypes.byref(cls))   # fails harmlessly if re-run
        self.hwnd = _user32.CreateWindowExW(0, TRAY_CLASS, TRAY_CLASS, 0,
                                            0, 0, 0, 0, None, None, module, None)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        # A ctypes callback that raises prints a traceback and returns garbage,
        # so nothing is allowed out of here.
        try:
            if msg == TRAY_CALLBACK:
                event = lparam & 0xFFFF
                if event == WM_LBUTTONUP:
                    self.monitor.toggle_visible()
                elif event == WM_RBUTTONUP:
                    self.monitor.popup_menu_at_cursor()
                return 0
            if msg == WM_CLOSE:
                # How uninstall.ps1 (or anything else) asks the widget to go
                # away. Deferred onto the Tk loop rather than run here, so the
                # window is not destroyed while it is handling its own message.
                self.monitor.root.after(0, self.monitor.quit)
                return 0
            if msg and msg == self._taskbar_created:
                self.shown = False
                self.state = None
                self.monitor.refresh_tray()
                return 0
        except Exception:
            pass
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _data(self, icon, tip):
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = TRAY_CALLBACK
        data.hIcon = icon
        data.szTip = tip[:127]
        return data

    def update(self, color: str, tip: str) -> None:
        """Add or re-colour the icon. Cheap to call on every refresh."""
        if not self.hwnd or (color, tip) == self.state:
            return
        icon = make_dot_icon(color, self.size)
        if not icon:
            return
        data = self._data(icon, tip)
        action = NIM_MODIFY if self.shown else NIM_ADD
        ok = _shell32.Shell_NotifyIconW(action, ctypes.byref(data))
        if not ok:
            # Our idea of whether the icon exists can be wrong in either
            # direction - the shell may have dropped it (an Explorer restart we
            # missed) or still hold it when we thought it was gone - and either
            # way giving up here leaves a tray icon that never updates again.
            other = NIM_ADD if action == NIM_MODIFY else NIM_MODIFY
            ok = _shell32.Shell_NotifyIconW(other, ctypes.byref(data))
        if not ok:
            _user32.DestroyIcon(icon)
            return
        self.shown = True
        self.state = (color, tip)
        # Only now is the old icon certainly unused by the shell.
        if self.icon:
            _user32.DestroyIcon(self.icon)
        self.icon = icon

    def destroy(self) -> None:
        """Take the icon out of the tray. Skipping this leaves a ghost behind."""
        try:
            if self.shown and self.hwnd:
                data = _NOTIFYICONDATAW()
                data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
                data.hWnd = self.hwnd
                data.uID = 1
                _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
            self.shown = False
            if self.icon:
                _user32.DestroyIcon(self.icon)
                self.icon = None
            if self.hwnd:
                _user32.DestroyWindow(self.hwnd)
                self.hwnd = None
        except Exception:
            pass


class Tip:
    """One reusable tooltip window, shown for rows whose name was shortened."""

    DELAY_MS = 400

    def __init__(self, root, font):
        self.root = root
        self.font = font
        self.win = None
        self.label = None
        self.timer = None

    def schedule(self, text: str, x: int, y: int) -> None:
        self.hide()
        self.timer = self.root.after(self.DELAY_MS,
                                     lambda: self._show(text, x, y))

    def _show(self, text: str, x: int, y: int) -> None:
        self.timer = None
        try:
            if self.win is None:
                self.win = tk.Toplevel(self.root)
                self.win.overrideredirect(True)
                self.win.attributes("-topmost", True)
                self.win.configure(bg=BORDER)
                self.label = tk.Label(self.win, bg=BG_HEADER, fg=FG, padx=px(7),
                                      pady=px(3), font=self.font, justify="left")
                self.label.pack(padx=1, pady=1)
            self.label.config(text=text)
            self.win.geometry(f"+{x + px(12)}+{y + px(18)}")
            self.win.deiconify()
        except tk.TclError:
            pass

    def hide(self) -> None:
        if self.timer is not None:
            try:
                self.root.after_cancel(self.timer)
            except tk.TclError:
                pass
            self.timer = None
        if self.win is not None:
            try:
                self.win.withdraw()
            except tk.TclError:
                pass


class Monitor:
    """The floating widget window and its update loop."""

    def __init__(self, root):
        self.root = root
        self.blink_on = True
        self.last_signature = None
        self.prev_status = {}     # session_id -> last seen status (edge detection)
        self.missing_since = {}   # session_id -> time its project window vanished
        self.collapsed = False
        self.has_action = False
        self.attn_color = STATUS_META["action"]["color"]
        self.drag = (0, 0)
        self.resizing = (0, DEFAULT_WIDTH)
        self.rows = []            # one dict per rendered row, see build_rows
        self.action_dots = []     # [(dot_label, color)] of attention rows to blink

        self.counts = (0, 0, 0)   # permission, action, working - for the tray

        cfg = load_config()
        self.sound_on = bool(cfg.get("sound_on", True))
        self.hidden = bool(cfg.get("hidden", False))
        self.width = clamp_width(cfg.get("width", DEFAULT_WIDTH))
        self.anchor = self._load_anchor(cfg)

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.96)
        except tk.TclError:
            pass
        root.configure(bg=BORDER)

        # Tk sizes point-based fonts from its own scaling factor. Setting it
        # from SCALE rather than leaving Tk to work it out keeps the text in
        # step with the layout constants - and it belongs here, next to the
        # fonts it governs, rather than in main(), where the two could drift
        # apart for anyone building a Monitor directly.
        root.tk.call("tk", "scaling", SCALE * 96.0 / 72.0)

        self.f_title = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.f_proj = tkfont.Font(family="Segoe UI", size=10)
        self.f_status = tkfont.Font(family="Segoe UI Semibold", size=9, weight="bold")
        self.f_small = tkfont.Font(family="Segoe UI", size=8)
        self.f_count = tkfont.Font(family="Segoe UI", size=9)
        self.f_icon = tkfont.Font(family="Segoe UI", size=12)

        self.outer = tk.Frame(root, bg=BORDER)
        self.outer.pack(fill="both", expand=True, padx=1, pady=1)

        # Packed before the header and body so it claims a full-height column
        # on the right; pack fills the remaining cavity with everything else.
        self._build_grip()
        self._build_header()

        self.body = tk.Frame(self.outer, bg=BG)
        self.body.pack(fill="both", expand=True)

        self.tip = Tip(root, self.f_small)

        self._build_menu()
        self.tray = Tray(self)

        self.reposition()
        if self.hidden:
            root.withdraw()       # started in tray-only mode
        self.refresh()
        self.blink()

    # ---------- Geometry ----------
    def _default_anchor(self):
        """Bottom-left corner of the primary monitor's work area."""
        left, _top, _right, bottom = work_area()
        return left + MARGIN, bottom - MARGIN

    def _load_anchor(self, cfg):
        """The saved bottom-left corner, or the default one.

        The *bottom*-left corner is what gets stored, because the box grows
        upward: a new session adds its row above the bottom edge instead of
        pushing the widget down off the screen and shifting the rows you were
        already looking at.

        A saved corner is only honoured if it is still on a connected monitor -
        otherwise unplugging a second screen would leave the widget marooned
        off-desktop with no way to drag it back.
        """
        try:
            x, bottom = int(cfg["x"]), int(cfg["bottom"])
        except (KeyError, TypeError, ValueError):
            return self._default_anchor()
        vx, vy, vw, vh = virtual_screen()
        on_screen = (vw > 0 and vh > 0
                     and vx <= x <= vx + vw - MIN_WIDTH
                     and vy < bottom <= vy + vh)
        return (x, bottom) if on_screen else self._default_anchor()

    def reposition(self):
        """Resize to fit the rows, keeping the remembered bottom-left corner.

        Called after every rebuild, so it must not move the box. Pinning it back
        to the screen corner here is what used to undo a drag the moment any
        session changed status.
        """
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        x, bottom = self.anchor
        y = bottom - h
        _vx, vy, _vw, vh = virtual_screen()
        if vh > 0 and y < vy:
            # Enough sessions to reach the top of the desktop: stop there rather
            # than scrolling the header out of reach. The anchor is left alone,
            # so the box drops back to it as soon as rows go away.
            y = vy
        self.root.geometry("%dx%d+%d+%d" % (self.width, h, x, y))

    def remember_geometry(self):
        """Persist the corner and width the user just dragged the box to."""
        self.root.update_idletasks()
        self.anchor = (self.root.winfo_x(),
                       self.root.winfo_y() + self.root.winfo_height())
        update_config(x=self.anchor[0], bottom=self.anchor[1], width=self.width)

    def reset_geometry(self):
        """Put the box back in its default corner at its default width."""
        self.width = DEFAULT_WIDTH
        self.anchor = self._default_anchor()
        self.reposition()
        self.fit_names()
        update_config(x=self.anchor[0], bottom=self.anchor[1], width=self.width)

    # ---------- Header ----------
    def _build_header(self):
        self.header = tk.Frame(self.outer, bg=BG_HEADER, height=HEADER_H)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)

        self.chevron = tk.Label(self.header, text="▾", bg=BG_HEADER, fg=FG_DIM,
                                font=self.f_title, cursor="hand2")
        self.chevron.pack(side="left", padx=(px(8), px(2)))
        self.chevron.bind("<Button-1>", lambda e: self.toggle_collapse())

        self.dot = tk.Label(self.header, text="●", bg=BG_HEADER, fg=ACCENT,
                            font=self.f_title)
        self.dot.pack(side="left", padx=(px(2), px(6)))

        self.title = tk.Label(self.header, text="Claude Sessions", bg=BG_HEADER,
                              fg=FG, font=self.f_title)
        self.title.pack(side="left")

        self.close_btn = tk.Label(self.header, text="×", bg=BG_HEADER, fg=FG_DIM,
                                  font=self.f_title, cursor="hand2")
        self.close_btn.pack(side="right", padx=(0, px(10)))
        self.close_btn.bind("<Button-1>", lambda e: self.quit())
        self.close_btn.bind("<Enter>", lambda e: self.close_btn.config(fg="#f85149"))
        self.close_btn.bind("<Leave>", lambda e: self.close_btn.config(fg=FG_DIM))

        # Sound toggle (a note; struck through = muted)
        self.sound_btn = tk.Label(self.header, text="♪", bg=BG_HEADER,
                                  font=self.f_icon, cursor="hand2")
        self.sound_btn.pack(side="right", padx=(0, px(8)))
        self.sound_btn.bind("<Button-1>", lambda e: self.toggle_sound())
        self._update_sound_btn()

        # Counts only the sessions that are waiting for you (permission +
        # action) - a "working" count says nothing you have to act on.
        self.count_lbl = tk.Label(self.header, text="", bg=BG_HEADER, fg=FG_DIM,
                                  font=self.f_count)
        self.count_lbl.pack(side="right", padx=(0, px(8)))

        for w in (self.header, self.title, self.dot):
            w.bind("<Button-1>", self.start_drag)
            w.bind("<B1-Motion>", self.on_drag)
            w.bind("<ButtonRelease-1>", self.end_drag)
            w.bind("<Double-Button-1>", lambda e: self.toggle_collapse())

    def _build_grip(self):
        """The drag-to-resize strip down the right edge.

        Coloured like the border so it reads as part of the frame rather than as
        an extra element, and lit up on hover so it is discoverable - a frameless
        window gives Windows nothing to draw a resize edge on.
        """
        self.grip = tk.Frame(self.outer, bg=BORDER, width=GRIP_W,
                             cursor="sb_h_double_arrow")
        self.grip.pack(side="right", fill="y")
        self.grip.pack_propagate(False)
        self.grip.bind("<Button-1>", self.start_resize)
        self.grip.bind("<B1-Motion>", self.on_resize)
        self.grip.bind("<ButtonRelease-1>", self.end_resize)
        self.grip.bind("<Enter>", lambda e: self.grip.config(bg=ACCENT))
        self.grip.bind("<Leave>", lambda e: self.grip.config(bg=BORDER))

    def _build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0, bg=BG_HEADER, fg=FG,
                            activebackground="#1f6feb", activeforeground="#ffffff",
                            bd=0)
        # Entry 0's label flips between Hide and Show, so it must stay first.
        self.menu.add_command(label="Hide box", command=self.toggle_visible)
        self.menu.add_command(label="Clear finished", command=self.clear_done)
        self.menu.add_command(label="Reset position", command=self.reset_geometry)
        self.menu.add_command(label="Refresh", command=self.refresh)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit)
        self.root.bind("<Button-3>", self.show_menu)

    # ---------- Dragging / resizing / menu ----------
    def start_drag(self, e):
        # Store the grab point relative to the window, so the cursor keeps
        # holding the same spot however far the pointer travels in one motion.
        self.drag = (e.x_root - self.root.winfo_x(),
                     e.y_root - self.root.winfo_y())

    def on_drag(self, e):
        dx, dy = self.drag
        self.root.geometry("+%d+%d" % (e.x_root - dx, e.y_root - dy))

    def end_drag(self, _e=None):
        self.remember_geometry()

    def start_resize(self, e):
        self.resizing = (e.x_root, self.width)

    def on_resize(self, e):
        x0, w0 = self.resizing
        width = clamp_width(w0 + (e.x_root - x0))
        if width == self.width:
            return
        self.width = width
        # Only the right edge moves: the anchor is the *left* corner, so the box
        # grows away from the screen edge it is usually parked against.
        self.reposition()
        self.fit_names()

    def end_resize(self, _e=None):
        self.remember_geometry()

    def show_menu(self, e):
        self._popup(e.x_root, e.y_root)

    def _popup(self, x, y):
        self.menu.entryconfigure(0, label="Show box" if self.hidden else "Hide box")
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()

    # ---------- Tray ----------
    def toggle_visible(self):
        """Hide the box into the tray, or bring it back where it was."""
        self.hidden = not self.hidden
        if self.hidden:
            self.tip.hide()
            self.root.withdraw()
        else:
            self.root.deiconify()
            self.reposition()
        update_config(hidden=self.hidden)

    def popup_menu_at_cursor(self):
        """Open the menu at the mouse, for a right-click on the tray icon."""
        point = wintypes.POINT()
        if not _user32.GetCursorPos(ctypes.byref(point)):
            return
        # A popup menu only closes on an outside click while its owner is the
        # foreground window, and the box may be hidden or unfocused - so the
        # tray's own window takes the foreground first. Without this the menu
        # can be left on screen with no way to dismiss it.
        if self.tray.hwnd:
            _user32.SetForegroundWindow(self.tray.hwnd)
        self._popup(point.x, point.y)

    def refresh_tray(self):
        """Push the aggregate status of every session into the tray icon."""
        n_perm, n_action, n_work = self.counts
        if n_perm:
            color = STATUS_META["permission"]["color"]
        elif n_action:
            color = STATUS_META["action"]["color"]
        elif n_work:
            color = STATUS_META["working"]["color"]
        else:
            color = FG_DIM
        parts = []
        if n_perm:
            parts.append("%d need approval" % n_perm)
        if n_action:
            parts.append("%d waiting for you" % n_action)
        if n_work:
            parts.append("%d working" % n_work)
        self.tray.update(color, "Claude Sessions - " +
                         (", ".join(parts) if parts else "no active sessions"))

    def quit(self):
        """Shut down, taking the tray icon with us rather than leaving a ghost."""
        self.tray.destroy()
        self.root.destroy()

    # ---------- Collapse ----------
    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.body.pack_forget()
            self.chevron.config(text="▸")
        else:
            self.body.pack(fill="both", expand=True)
            self.chevron.config(text="▾")
        self.reposition()

    # ---------- Sound ----------
    def _update_sound_btn(self):
        # note glyph: on = bright, off = dimmed and struck through
        self.f_icon.configure(overstrike=(0 if self.sound_on else 1))
        self.sound_btn.config(fg=(ACCENT if self.sound_on else FG_DIM))

    def toggle_sound(self):
        self.sound_on = not self.sound_on
        update_config(sound_on=self.sound_on)
        self._update_sound_btn()
        if self.sound_on:
            play_alert()   # short preview that sound is on

    # ---------- Menu actions ----------
    def clear_done(self):
        try:
            for name in os.listdir(STATUS_DIR):
                if not name.endswith(".json"):
                    continue
                p = os.path.join(STATUS_DIR, name)
                try:
                    with open(p, "r", encoding="utf-8-sig") as f:
                        rec = json.load(f)
                except Exception:
                    continue
                if rec.get("status") == "done":
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        except OSError:
            pass
        self.refresh()

    # ---------- Row click -> focus that session's window ----------
    def on_row_click(self, ancestors, project):
        self.tip.hide()
        if not focus_session(ancestors, project):
            # nothing to switch to - briefly flash the title as feedback
            self.title.config(fg="#f85149")
            self.root.after(250, lambda: self.title.config(fg=FG))

    def _hover(self, widgets, color):
        for w in widgets:
            try:
                w.config(bg=color)
            except tk.TclError:
                pass

    def _enter(self, cells, tip_text, event):
        self._hover(cells, BG_HOVER)
        if tip_text:
            self.tip.schedule(tip_text, event.x_root, event.y_root)

    def _leave(self, cells):
        self._hover(cells, BG)
        self.tip.hide()

    # ---------- Rendering ----------
    @staticmethod
    def row_key(s):
        """Identity of a row: the Claude process, or the session id without one.

        Keyed on the process so a session id rotating under one window updates
        the row it already has instead of churning the whole list.
        """
        return s.get("claude_pid") or s["session_id"]

    def build_rows(self, sessions):
        for w in self.body.winfo_children():
            w.destroy()
        self.action_dots = []
        self.rows = []
        self.tip.hide()   # the widget it was anchored to is gone

        if not sessions:
            tk.Label(self.body, text="No active sessions", bg=BG, fg=FG_DIM,
                     font=self.f_small, anchor="w",
                     padx=px(12), pady=px(10)).pack(fill="x")
            return

        # Count duplicate project names to disambiguate them in the list.
        seen = {}
        for s in sessions:
            seen[s["project"]] = seen.get(s["project"], 0) + 1

        for s in sessions:
            meta = STATUS_META.get(s.get("status"), UNKNOWN)

            row = tk.Frame(self.body, bg=BG, cursor="hand2")
            row.pack(fill="x")

            dot = tk.Label(row, text="●", bg=BG, fg=meta["color"], font=self.f_proj)
            dot.pack(side="left", padx=(ROW_PAD_LEFT, ROW_DOT_GAP), pady=ROW_PAD_Y)
            if meta.get("attn"):
                self.action_dots.append((dot, meta["color"]))

            name = s["project"]
            if seen.get(name, 0) > 1:
                name = f"{name} #{s['session_id'][-4:]}"

            # The status label is packed first so pack reserves its full width;
            # the name then expands into whatever is left. Packed the other way
            # round the expanding name takes the cavity first and the status
            # label is squeezed below its requested width, which silently clips
            # its text ("Permission" -> a few letters) on long project names.
            status = tk.Label(row, text=meta["label"], bg=BG, fg=meta["color"],
                              font=self.f_status, anchor="e")
            status.pack(side="right", padx=(ROW_NAME_GAP, ROW_PAD_RIGHT))

            # Packed only while it has text (see update_ages), so a fresh row
            # keeps exactly the spacing it had before there was an age column.
            age = tk.Label(row, text="", bg=BG, fg=FG_DIM, font=self.f_small,
                           anchor="e")

            proj = tk.Label(row, text=name, bg=BG, fg=FG, font=self.f_proj,
                            anchor="w")
            proj.pack(side="left", fill="x", expand=True)

            item = {"key": self.row_key(s), "frame": row, "dot": dot,
                    "proj": proj, "status": status, "age": age, "name": name,
                    "meta": meta, "tip": "", "age_text": "", "stale": False}
            self.rows.append(item)

            cells = (row, dot, proj, status, age)
            for w in cells:
                w.bind("<Button-1>",
                       lambda e, a=s.get("ancestors") or [], p=s["project"]:
                       self.on_row_click(a, p))
                # The tooltip is read from the row at event time, not captured:
                # resizing the box changes which names are shortened.
                w.bind("<Enter>",
                       lambda e, c=cells, it=item: self._enter(c, it["tip"], e))
                w.bind("<Leave>", lambda e, c=cells: self._leave(c))

        self.fit_names()

    def fit_names(self):
        """Shorten the project names to the room the rows currently have.

        Separate from ``build_rows`` because dragging the resize grip, and an
        age chip appearing, both change the room without changing the rows.
        """
        for item in self.rows:
            shown = fit_text(item["name"], self.f_proj, self._name_room(item))
            if item["proj"].cget("text") != shown:
                item["proj"].config(text=shown)
            # Only a shortened name gets a tooltip - that is the one case where
            # the row doesn't already tell you everything.
            item["tip"] = item["name"] if shown != item["name"] else ""

    def _name_room(self, item) -> int:
        """Pixels the name label can actually draw text in.

        The row width is derived from ``self.width`` rather than read back with
        ``winfo_width``: during a resize drag Tk has not applied the new
        geometry yet, so asking it would fit every name to the previous width.
        ``winfo_reqwidth`` of a label, on the other hand, is its text plus its
        own border and padding, so the difference from the measured text is the
        chrome that has to come off the budget too.
        """
        proj, age = item["proj"], item["age"]
        outer = self.width - 2 - GRIP_W          # 1px window border either side
        chrome = max(0, proj.winfo_reqwidth() - self.f_proj.measure(proj.cget("text")))
        taken = (ROW_PAD_LEFT + item["dot"].winfo_reqwidth() + ROW_DOT_GAP
                 + ROW_NAME_GAP + item["status"].winfo_reqwidth() + ROW_PAD_RIGHT
                 + chrome)
        if item["age_text"]:
            taken += age.winfo_reqwidth() + ROW_NAME_GAP
        return outer - taken

    def update_ages(self, sessions):
        """Show how stale each row is, in place.

        A row is only *stale* if nothing is expected to refresh it: "Action!"
        and "Permission!" are states a session sits in on purpose while it waits
        for you, so ageing those would dim exactly the rows that matter. A green
        "Working" that has not been touched for minutes is the interesting case
        - either a long tool call, or a session whose hooks have quietly stopped
        arriving, and until now the widget showed both as freshly working.

        Done in place rather than by rebuilding the rows: nothing about the list
        has changed except its age, and a rebuild twice a second would flicker
        and keep dropping the tooltip.
        """
        now = time.time()
        by_key = {self.row_key(s): s for s in sessions}
        refit = False
        for item in self.rows:
            s = by_key.get(item["key"])
            if s is None:
                continue
            age = now - s.get("_updated", now)
            stale = (not item["meta"].get("attn")) and age >= STALE_AFTER
            text = format_age(age) if stale else ""

            if text != item["age_text"]:
                item["age_text"] = text
                item["age"].config(text=text)
                if text:
                    item["age"].pack(side="right", after=item["status"],
                                     padx=(ROW_NAME_GAP, 0))
                else:
                    item["age"].pack_forget()
                refit = True
            if stale != item["stale"]:
                item["stale"] = stale
                self._paint(item)
        if refit:
            self.fit_names()

    def _paint(self, item):
        """Recolor a row for its current staleness."""
        t = FADE if item["stale"] else 0.0
        color = item["meta"]["color"]
        item["dot"].config(fg=blend(color, BG, t))
        item["status"].config(fg=blend(color, BG, t))
        item["proj"].config(fg=blend(FG, BG, t))
        item["age"].config(fg=blend(FG_DIM, BG, t))

    def cleanup_closed(self, sessions):
        """Drop sessions that are no longer running.

        A recorded ``claude_pid`` is authoritative: if that process is gone the
        session is over, even though ``SessionEnd`` never ran (hard window
        close). Records without a pid (written by an older ``hook.py``) fall
        back to matching the project name against open editor window titles,
        which needs the ``MISSING_DWELL`` delay because a window can briefly
        drop its title while reloading.
        """
        now = time.time()
        alive = []
        titles = None
        name_cache = {}
        for s in sessions:
            sid = s["session_id"]
            recent = (now - s.get("_updated", 0)) < GRACE_NEW
            pid = s.get("claude_pid")

            if isinstance(pid, int) and pid > 0:
                if recent or is_claude_pid(pid, name_cache):
                    alive.append(s)
                else:
                    remove_status(sid)
                self.missing_since.pop(sid, None)
                continue

            if titles is None:
                titles = open_editor_titles()
            if not titles:
                # could not enumerate windows -> don't remove anything
                alive.append(s)
                continue
            proj = (s.get("project") or "").lower()
            present = bool(proj) and any(proj in t for t in titles)
            if present or recent:
                self.missing_since.pop(sid, None)
                alive.append(s)
                continue
            # project window gone - wait MISSING_DWELL before deleting the file
            first = self.missing_since.get(sid)
            if first is None:
                self.missing_since[sid] = now
                alive.append(s)
            elif now - first >= MISSING_DWELL:
                remove_status(sid)
                self.missing_since.pop(sid, None)
            else:
                alive.append(s)
        return alive

    def _attn(self, status) -> bool:
        return STATUS_META.get(status, UNKNOWN).get("attn", False)

    def refresh(self):
        sessions = self.cleanup_closed(load_sessions())

        # Sound: play when a *known* session enters a "waiting for you" state
        # (e.g. working -> permission/action). New sessions don't beep.
        current = {s["session_id"]: s.get("status") for s in sessions}
        for sid, st in current.items():
            was = self.prev_status.get(sid)
            if self._attn(st) and sid in self.prev_status and not self._attn(was):
                if self.sound_on:
                    play_alert()
                break
        self.prev_status = current

        n_perm = sum(1 for s in sessions if s.get("status") == "permission")
        n_action = sum(1 for s in sessions if s.get("status") == "action")
        n_work = sum(1 for s in sessions if s.get("status") == "working")
        self.has_action = (n_perm + n_action) > 0
        self.counts = (n_perm, n_action, n_work)
        self.refresh_tray()
        # header dot blink color - permission (blue) takes precedence
        self.attn_color = (STATUS_META["permission"]["color"] if n_perm
                           else STATUS_META["action"]["color"])

        # Only rebuild rows when the set of (session, status) actually changes.
        signature = tuple((self.row_key(s), s.get("status")) for s in sessions)
        if signature != self.last_signature:
            self.last_signature = signature
            self.build_rows(sessions)
            self.reposition()

        self.update_ages(sessions)

        n_attn = n_perm + n_action
        self.count_lbl.config(text=f"{n_attn} {EYE}" if n_attn else "",
                              fg=self.attn_color if n_attn else FG_DIM)

        if not self.has_action:
            self.dot.config(fg=STATUS_META["working"]["color"] if n_work else ACCENT)

        self.root.after(REFRESH_MS, self.refresh)

    def blink(self):
        self.blink_on = not self.blink_on
        for dot, color in self.action_dots:
            try:
                dot.config(fg=(color if self.blink_on else BG))
            except tk.TclError:
                pass
        # Header dot blinks too (blue for permission, amber otherwise).
        if self.has_action:
            self.dot.config(fg=(self.attn_color if self.blink_on else BG_HEADER))
        self.root.after(BLINK_MS, self.blink)


def main():
    if not claim_single_instance():
        # No dialog: the widget that is already running is on screen, and this
        # message is only reachable when someone runs the file from a console.
        print("Claude Session Monitor is already running.", file=sys.stderr)
        return
    root = tk.Tk()
    root.title("Claude Sessions")
    monitor = Monitor(root)
    try:
        root.mainloop()
    finally:
        # Quit() already did this; the finally is for every other way the loop
        # can end, which would otherwise leave a dead icon in the tray until
        # something makes the shell notice.
        monitor.tray.destroy()


if __name__ == "__main__":
    main()
