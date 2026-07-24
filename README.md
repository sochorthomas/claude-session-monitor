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

- **Live status per session** — Permission (blue), Action! (amber), Working (green)
- **Sorted by urgency** — sessions that need you float to the top
- **Click to jump** — click a row to bring that session's terminal/editor window
  to the front (restores it full-screen if it was minimized)
- **Sound alert** — a gentle chime when a session starts waiting for you;
  toggle it with the `♪` icon in the header
- **Auto-cleanup** — closed sessions disappear even when Claude Code can't run
  its exit hook (e.g. you closed the window)
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

`install.ps1` registers the hooks in `~/.claude/settings.json` (your other
settings are preserved). Then:

1. **Reload Claude Code** so it picks up the new hooks
   (VS Code: `Ctrl+Shift+P` → *Developer: Reload Window*).
2. **Start the widget** — double-click `start-monitor.vbs`.

New sessions now show up in the box automatically.

### Launch at login (optional)

Press `Win+R`, type `shell:startup`, and drop a shortcut to `start-monitor.vbs`
into that folder.

## How it works

Claude Code fires [hooks](https://docs.claude.com/en/docs/claude-code/hooks) on
lifecycle events. Each hook runs `hook-wrapper.ps1`, which calls `hook.py` to
write that session's status to `~/.claude/session-status/<session_id>.json`. The
widget (`widget.pyw`) reads those files once per second and renders the box.

| Claude Code event   | Status       | Color        |
| ------------------- | ------------ | ------------ |
| `UserPromptSubmit`, `PostToolUse` | **Working** | 🟢 green |
| `Stop`, `SessionStart` | **Action!** (waiting for your reply) | 🟡 amber |
| `PermissionRequest`, `Notification` | **Permission** (waiting for approval) | 🔵 blue |
| `SessionEnd`        | *(removed from the list)* | — |

The sound plays only when an already-known session transitions into a
"waiting for you" state, so opening a new session doesn't beep.

## Controls

- **Drag** the header to move the box.
- **Click a row** to focus that session's window.
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
- **Double-clicking `start-monitor.vbs` does nothing.** Make sure Python is
  installed and on `PATH`. As a fallback you can run
  `powershell -File .\start-monitor.ps1`.
- **Note on the Microsoft Store build of Python:** its process appears as
  `pythonw3.13.exe` (not `pythonw.exe`) in Task Manager — that's expected.

## License

[MIT](LICENSE) © Tomáš Sochor
