# Claude Session Monitor

A tiny always-on-top widget for Windows that shows every running
[Claude Code](https://claude.com/claude-code) session and, at a glance, whether
each one is **working**, **needs your permission**, or is **waiting for your
reply**.

If you keep several Claude Code sessions open across different projects, it's
easy to lose track of which one is blocked on you. This widget keeps a small
box in the corner of your screen so you always know where your attention is
needed — and one click jumps you straight to that window.

![Claude Session Monitor widget](docs/screenshot.png)

## Features

- **Live status per session** — Permission! (blue), Action! (amber), Working (green)
- **Sorted by urgency** — sessions that need you float to the top
- **Click to jump** — click a row to bring that session's terminal/editor window
  to the front (restores it full-screen if it was minimized)
- **Sound alert** — a gentle chime when a session starts waiting for you;
  toggle it with the `♪` icon in the header
- **Auto-cleanup** — closed sessions disappear even when Claude Code can't run
  its exit hook (e.g. you closed the window), and one window never shows up
  twice even when Claude Code changes a session's id mid-flight
- **Unobtrusive** — frameless, draggable, collapsible to a thin strip
- No dependencies beyond Python's standard library

## Requirements

- Windows 10/11
- [Claude Code](https://claude.com/claude-code) (CLI or the VS Code extension)
- Python 3.8+ on your `PATH` (`python --version`)

## Install

```powershell
git clone https://github.com/sochorthomas/claude-session-monitor.git
cd claude-session-monitor
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

`install.ps1` registers the hooks in `~/.claude/settings.json`. It only touches
its own entries — your other settings, your other hooks, and even your own hooks
on the same events are left alone — and it is safe to re-run. Then:

1. **Reload Claude Code** so it picks up the new hooks
   (VS Code: `Ctrl+Shift+P` → *Developer: Reload Window*).
2. **Start the widget** — double-click `start-monitor.vbs`.

New sessions now show up in the box automatically.

### Launch at login (optional)

Press `Win+R`, type `shell:startup`, and drop a shortcut to `start-monitor.vbs`
into that folder.

## How it works

Claude Code fires [hooks](https://docs.claude.com/en/docs/claude-code/hooks) on
lifecycle events. Each hook runs `hook.py`, which writes that session's status
to `~/.claude/session-status/<session_id>.json`. The widget (`widget.pyw`) reads
those files twice a second and renders the box.

| Claude Code event   | Status       | Color        |
| ------------------- | ------------ | ------------ |
| `UserPromptSubmit`, `PostToolUse` | **Working** | 🟢 green |
| `Stop`, `SessionStart` | **Action!** (waiting for your reply) | 🟡 amber |
| `PermissionRequest` | **Permission!** (waiting for approval) | 🔵 blue |
| `Notification` | either of the two above, see below | 🔵 / 🟡 |
| `SessionEnd`        | *(removed from the list)* | — |

`Notification` fires both when a tool needs approving and when the session has
just gone quiet, so the event name alone can't tell those apart. `hook.py` reads
the notification message to decide, instead of labelling every idle session as
waiting for approval.

The sound plays only when an already-known session transitions into a
"waiting for you" state, so opening a new session doesn't beep.

Hooks run on every tool call, so `hook.py` is kept cheap: it does no
subprocesses, and it finds the owning `claude` process by walking the parent
chain (a few `OpenProcess` calls) rather than enumerating every process on the
machine — which measured 0.4–1.4 s against under a millisecond. `install.ps1`
also resolves the interpreter up front and writes it straight into the hook
command, so a hook is one process instead of a PowerShell that goes looking for
Python each time.

### One row per window

Claude Code doesn't always keep one session id for the life of a window: it can
announce a `SessionStart` under one id and then run the conversation under
another, or rotate the id on resume/compact — in both cases without a
`SessionEnd`, leaving an abandoned status file that would show up as a second
row for the same project. The hook therefore also records the pid of the
`claude` process it was fired from, and the widget uses it to keep the list
honest:

- **one Claude process = one session** — if two status files share a pid, only
  the most recently updated one is shown, the other is deleted;
- **no transcript = never really started** — a session that has no
  `~/.claude/projects/<project>/<session_id>.jsonl` and has received no hook for
  `GHOST_AFTER` seconds (3 min) is dropped;
- **dead process = finished session** — the row goes away when the `claude`
  process exits, which is more reliable than matching window titles (that
  fallback is still used for status files written by an older `hook.py`).

Consequence worth knowing: a window you open but never type into disappears
from the list after ~3 minutes, and comes back the moment you send a prompt.

### Repository layout

The three things you run yourself sit in the root; everything they call lives in
`scripts/`.

```
install.ps1               register the hooks
uninstall.ps1             remove them again
start-monitor.vbs         start the widget (double-click this)
widget.pyw                the widget itself
hook.py                   what Claude Code runs on every hook event
scripts/start-monitor.ps1 finds Python, launches widget.pyw
```

## Controls

- **Drag** the header to move the box.
- **Click a row** to focus that session's window.
- **Hover a shortened name** (one ending in `…`) to see the full project name in
  a tooltip. The status label always keeps its space, so it stays readable no
  matter how long the project name is.
- **`♪`** toggles the notification sound (struck through = muted). The choice is
  remembered in `~/.claude/session-monitor-config.json`.
- **Chevron `▾/▸`** (or double-click the header) collapses the box to a strip.
- **`×`** closes the widget. **Right-click** for a small menu.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
```

This removes only this tool's hooks from `~/.claude/settings.json`, then reload
Claude Code. Close the widget with its `×` button.

## Troubleshooting

- **The box shows nothing.** Hooks load when a session starts — reload Claude
  Code (or open a new window). Confirm the hooks are registered by running
  `/hooks` inside Claude Code.
- **Still nothing after reloading.** Check `install.ps1` completed without errors
  and that `python`/`pythonw` are on your `PATH`.
- **It stopped working after I moved or reinstalled Python.** The interpreter
  path is baked into the hook command, so re-run `install.ps1`. Same after
  moving the repository itself.
- **Double-clicking `start-monitor.vbs` does nothing.** Make sure Python is
  installed and on `PATH`. To see the error the `.vbs` swallows, run the
  launcher directly: `powershell -File .\scripts\start-monitor.ps1`.
- **Note on the Microsoft Store build of Python:** its process appears as
  `pythonw3.13.exe` (not `pythonw.exe`) in Task Manager — that's expected.

## License

[MIT](LICENSE) © Tomáš Sochor
