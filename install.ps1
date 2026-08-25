# install.ps1 - register Claude Session Monitor hooks in ~/.claude/settings.json
#
# Only this tool's own hook entries are touched. Other settings, other hook
# events, and your own entries on the same events are all preserved. Safe to
# re-run: previous entries of ours are replaced, not duplicated.
#
# The interpreter path is resolved now and baked into the hook command, so each
# hook is a single process (Claude Code's shell -> python) instead of spawning
# another PowerShell to go looking for Python on every tool call. Re-run this
# script if you move or reinstall Python.
$ErrorActionPreference = "Stop"

$hook = Join-Path $PSScriptRoot "hook.py"
$settingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"

if (-not (Test-Path $hook)) {
    throw "hook.py not found next to install.ps1 (looked for $hook)"
}

# pythonw runs without a console window; python is the fallback.
$python = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) {
    throw "Python not found on PATH. Install Python 3.8+ and re-run this script."
}

# Matches our entries in settings.json - both the direct form written below and
# the hook-wrapper.ps1 indirection used by older versions, so re-running or
# uninstalling cleans those up too.
$OURS = "hook-wrapper\.ps1|hook\.py"

# Event -> (status, matcher). See README "How it works". "notify" is resolved to
# permission or action by hook.py, from the notification message.
$events = [ordered]@{
    SessionStart      = @{ status = "action";     matcher = $null }
    UserPromptSubmit  = @{ status = "working";    matcher = $null }
    PostToolUse       = @{ status = "working";    matcher = "*"  }
    PermissionRequest = @{ status = "permission"; matcher = "*"  }
    Notification      = @{ status = "notify";     matcher = $null }
    Stop              = @{ status = "action";     matcher = $null }
    SessionEnd        = @{ status = "end";        matcher = $null }
}

function New-HookGroup([string]$status, [string]$matcher) {
    # -E -s keeps a stray PYTHONPATH / user site-packages from breaking the hook.
    $cmd = "`"$python`" -E -s `"$hook`" $status"
    $entry = [PSCustomObject]@{ type = "command"; command = $cmd }
    if ($matcher) {
        return [PSCustomObject]@{ matcher = $matcher; hooks = @($entry) }
    }
    return [PSCustomObject]@{ hooks = @($entry) }
}

function Test-OurGroup($group) {
    return (ConvertTo-Json -InputObject $group -Depth 20 -Compress) -match $OURS
}

if (Test-Path $settingsPath) {
    try {
        $settings = Get-Content $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw "Existing settings.json is not valid JSON: $settingsPath"
    }
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path $settingsPath) | Out-Null
    $settings = [PSCustomObject]@{}
}

if (-not ($settings.PSObject.Properties.Name -contains "hooks")) {
    $settings | Add-Member -NotePropertyName hooks -NotePropertyValue ([PSCustomObject]@{}) -Force
}
$hooks = $settings.hooks

foreach ($evt in $events.Keys) {
    # Keep every foreign group on this event, drop our own from a previous run.
    $keep = @()
    if ($hooks.PSObject.Properties.Name -contains $evt) {
        $keep = @($hooks.$evt) | Where-Object { $_ -and -not (Test-OurGroup $_) }
    }
    $group = New-HookGroup $events[$evt].status $events[$evt].matcher
    $hooks | Add-Member -NotePropertyName $evt -NotePropertyValue (@($keep) + @($group)) -Force
}

# Serialize the whole document at once. Piping an array into ConvertTo-Json
# unwraps a single-element one, but arrays nested inside an object survive - so
# the hook arrays keep their shape as long as we never pipe them on their own.
$json = ConvertTo-Json -InputObject $settings -Depth 20
[IO.File]::WriteAllText($settingsPath, $json, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Installed hooks into $settingsPath" -ForegroundColor Green
Write-Host "Interpreter: $python"
Write-Host "Hook:        $hook"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1) Reload Claude Code so it picks up the hooks"
Write-Host "     (VS Code: Ctrl+Shift+P -> Developer: Reload Window)."
Write-Host "  2) Start the widget: double-click start-monitor.vbs"
