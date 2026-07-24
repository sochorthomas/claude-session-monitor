#!/usr/bin/env pythonw
"""Floating always-on-top box that shows all running Claude Code sessions.

Reads ``~/.claude/session-status/*.json`` (written by ``hook.py``) once per
second and renders one row per session with a color-coded status:

    Permission (blue)  - Claude is blocked, waiting for you to approve a tool
    Action!    (amber) - Claude finished / asked a question, waiting for a reply
    Working    (green) - Claude is actively working

Features:
  - frameless, always-on-top, draggable by the header
  - click a row to bring that session's terminal/editor window to the front
  - optional sound when a session starts waiting for you (toggle with the note)
  - collapse to a thin strip (chevron in the header)
  - right-click for a small menu

Windows only (uses Win32 APIs via ctypes and ``winsound``).
"""
import os
import json
import time
import threading
import ctypes
from ctypes import wintypes
import winsound
import tkinter as tk
import tkinter.font as tkfont

HOME = os.path.expanduser("~")
STATUS_DIR = os.path.join(HOME, ".claude", "session-status")
CONFIG_PATH = os.path.join(HOME, ".claude", "session-monitor-config.json")

# --- Behavior ---------------------------------------------------------------
REFRESH_MS = 1000       # how often to re-read the status files
BLINK_MS = 650          # blink interval for attention states
MAX_AGE_SEC = 24 * 3600  # drop sessions older than this (crash safety net)
GRACE_NEW = 25          # don't clean up a session in its first N seconds
MISSING_DWELL = 6       # a project window must be gone N s before we remove it

# --- Layout -----------------------------------------------------------------
MARGIN = 14
TASKBAR = 56            # approximate taskbar height (initial placement only)
WIDTH = 250

# --- Dark color scheme ------------------------------------------------------
BG = "#0d1117"
BG_HEADER = "#161b22"
BG_HOVER = "#1c2432"
BORDER = "#30363d"
FG = "#e6edf3"
FG_DIM = "#8b949e"
ACCENT = "#58a6ff"

# Status metadata. ``attn`` = needs the user -> blinks, plays a sound on entry,
# and sorts to the top (lower ``prio`` = higher in the list).
STATUS_META = {
    "permission": {"label": "Permission", "color": "#58a6ff", "prio": 0, "attn": True},
    "action":     {"label": "Action!",    "color": "#e3b341", "prio": 1, "attn": True},
    "working":    {"label": "Working",     "color": "#3fb950", "prio": 2, "attn": False},
    "done":       {"label": "Done",        "color": "#6e7681", "prio": 3, "attn": False},
}
UNKNOWN = {"label": "?", "color": FG_DIM, "prio": 4, "attn": False}


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
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
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# Process names treated as a terminal/editor when matching a window by title.
_TERM_PROC = {
    "code.exe", "code - insiders.exe", "windowsterminal.exe", "wt.exe",
    "powershell.exe", "pwsh.exe", "cmd.exe", "conhost.exe",
    "alacritty.exe", "wezterm-gui.exe", "hyper.exe", "cursor.exe",
    "windsurf.exe", "openconsole.exe",
}


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


def load_sessions():
    """Read all status files, dropping ones older than ``MAX_AGE_SEC``.

    Returns the records sorted by status priority (attention first) then by
    most-recently-updated.
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

    def key(r):
        st = STATUS_META.get(r.get("status"), UNKNOWN)
        return (st["prio"], -r.get("_updated", 0))

    out.sort(key=key)
    return out


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
        self.sound_on = bool(load_config().get("sound_on", True))
        self.drag = {"x": 0, "y": 0}
        self.action_dots = []     # [(dot_label, color)] of attention rows to blink

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.96)
        except tk.TclError:
            pass
        root.configure(bg=BORDER)

        self.f_title = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.f_proj = tkfont.Font(family="Segoe UI", size=10)
        self.f_status = tkfont.Font(family="Segoe UI Semibold", size=9, weight="bold")
        self.f_small = tkfont.Font(family="Segoe UI", size=8)
        self.f_icon = tkfont.Font(family="Segoe UI", size=12)

        self.outer = tk.Frame(root, bg=BORDER)
        self.outer.pack(fill="both", expand=True, padx=1, pady=1)

        self._build_header()

        self.body = tk.Frame(self.outer, bg=BG)
        self.body.pack(fill="both", expand=True)

        self._build_menu()

        self.reposition()
        self.refresh()
        self.blink()

    # ---------- Header ----------
    def _build_header(self):
        self.header = tk.Frame(self.outer, bg=BG_HEADER, height=30)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)

        self.chevron = tk.Label(self.header, text="▾", bg=BG_HEADER, fg=FG_DIM,
                                font=self.f_title, cursor="hand2")
        self.chevron.pack(side="left", padx=(8, 2))
        self.chevron.bind("<Button-1>", lambda e: self.toggle_collapse())

        self.dot = tk.Label(self.header, text="●", bg=BG_HEADER, fg=ACCENT,
                            font=self.f_title)
        self.dot.pack(side="left", padx=(2, 6))

        self.title = tk.Label(self.header, text="Claude Sessions", bg=BG_HEADER,
                              fg=FG, font=self.f_title)
        self.title.pack(side="left")

        self.close_btn = tk.Label(self.header, text="×", bg=BG_HEADER, fg=FG_DIM,
                                  font=self.f_title, cursor="hand2")
        self.close_btn.pack(side="right", padx=(0, 10))
        self.close_btn.bind("<Button-1>", lambda e: self.root.destroy())
        self.close_btn.bind("<Enter>", lambda e: self.close_btn.config(fg="#f85149"))
        self.close_btn.bind("<Leave>", lambda e: self.close_btn.config(fg=FG_DIM))

        # Sound toggle (a note; struck through = muted)
        self.sound_btn = tk.Label(self.header, text="♪", bg=BG_HEADER,
                                  font=self.f_icon, cursor="hand2")
        self.sound_btn.pack(side="right", padx=(0, 8))
        self.sound_btn.bind("<Button-1>", lambda e: self.toggle_sound())
        self._update_sound_btn()

        self.count_lbl = tk.Label(self.header, text="", bg=BG_HEADER, fg=FG_DIM,
                                  font=self.f_small)
        self.count_lbl.pack(side="right", padx=(0, 8))

        for w in (self.header, self.title, self.dot):
            w.bind("<Button-1>", self.start_drag)
            w.bind("<B1-Motion>", self.on_drag)
            w.bind("<Double-Button-1>", lambda e: self.toggle_collapse())

    def _build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0, bg=BG_HEADER, fg=FG,
                            activebackground="#1f6feb", activeforeground="#ffffff",
                            bd=0)
        self.menu.add_command(label="Clear finished", command=self.clear_done)
        self.menu.add_command(label="Refresh", command=self.refresh)
        self.menu.add_separator()
        self.menu.add_command(label="Close", command=self.root.destroy)
        self.root.bind("<Button-3>", self.show_menu)

    # ---------- Dragging / menu ----------
    def start_drag(self, e):
        self.drag["x"] = e.x
        self.drag["y"] = e.y

    def on_drag(self, e):
        x = self.root.winfo_x() + (e.x - self.drag["x"])
        y = self.root.winfo_y() + (e.y - self.drag["y"])
        self.root.geometry(f"+{x}+{y}")

    def show_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def reposition(self):
        """Size to content (fixed width) and pin to the bottom-left corner."""
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        sh = self.root.winfo_screenheight()
        x = MARGIN
        y = sh - h - TASKBAR - MARGIN
        if y < MARGIN:
            y = MARGIN
        self.root.geometry(f"{WIDTH}x{h}+{x}+{y}")

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
        save_config({"sound_on": self.sound_on})
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

    # ---------- Rendering ----------
    def build_rows(self, sessions):
        for w in self.body.winfo_children():
            w.destroy()
        self.action_dots = []

        if not sessions:
            tk.Label(self.body, text="No active sessions", bg=BG, fg=FG_DIM,
                     font=self.f_small, anchor="w", padx=12, pady=10).pack(fill="x")
            return

        # Count duplicate project names to disambiguate them in the list.
        seen = {}
        for s in sessions:
            seen[s["project"]] = seen.get(s["project"], 0) + 1

        for s in sessions:
            meta = STATUS_META.get(s.get("status"), UNKNOWN)
            ancestors = s.get("ancestors") or []

            row = tk.Frame(self.body, bg=BG, cursor="hand2")
            row.pack(fill="x")

            dot = tk.Label(row, text="●", bg=BG, fg=meta["color"], font=self.f_proj)
            dot.pack(side="left", padx=(10, 8), pady=5)
            if meta.get("attn"):
                self.action_dots.append((dot, meta["color"]))

            name = s["project"]
            if seen.get(name, 0) > 1:
                name = f"{name} #{s['session_id'][-4:]}"
            proj = tk.Label(row, text=name, bg=BG, fg=FG, font=self.f_proj, anchor="w")
            proj.pack(side="left", fill="x", expand=True)

            status = tk.Label(row, text=meta["label"], bg=BG, fg=meta["color"],
                              font=self.f_status, anchor="e")
            status.pack(side="right", padx=(8, 12))

            cells = (row, dot, proj, status)
            pname = s["project"]
            for w in cells:
                w.bind("<Button-1>",
                       lambda e, a=ancestors, p=pname: self.on_row_click(a, p))
                w.bind("<Enter>", lambda e, c=cells: self._hover(c, BG_HOVER))
                w.bind("<Leave>", lambda e, c=cells: self._hover(c, BG))

    def cleanup_closed(self, sessions):
        """Drop sessions whose terminal/editor window is no longer open."""
        titles = open_editor_titles()
        if not titles:
            # could not enumerate windows -> don't remove anything
            return sessions
        now = time.time()
        alive = []
        for s in sessions:
            proj = (s.get("project") or "").lower()
            present = bool(proj) and any(proj in t for t in titles)
            recent = (now - s.get("_updated", 0)) < GRACE_NEW
            sid = s["session_id"]
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
                try:
                    os.remove(os.path.join(STATUS_DIR, sid + ".json"))
                except OSError:
                    pass
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
        # header dot blink color - permission (blue) takes precedence
        self.attn_color = (STATUS_META["permission"]["color"] if n_perm
                           else STATUS_META["action"]["color"])

        # Only rebuild rows when the set of (session, status) actually changes.
        signature = tuple((s["session_id"], s.get("status")) for s in sessions)
        if signature != self.last_signature:
            self.last_signature = signature
            self.build_rows(sessions)
            self.reposition()

        parts = []
        if n_work:
            parts.append(f"{n_work} ▶")
        if n_action:
            parts.append(f"{n_action} ⚠")
        if n_perm:
            parts.append(f"{n_perm} ●")
        self.count_lbl.config(text="  ".join(parts))

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
    root = tk.Tk()
    root.title("Claude Sessions")
    Monitor(root)
    root.mainloop()


if __name__ == "__main__":
    main()
