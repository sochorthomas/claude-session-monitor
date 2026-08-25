# uninstall.ps1 - remove Claude Session Monitor from this machine
#
# Four things, in order: stop the running widget, unregister the hooks, drop the
# files the tool created under ~/.claude, and remove a Startup shortcut pointing
# at this copy. Pass -KeepData to leave the status directory and the saved
# position/width/sound settings in place, for a reinstall.
#
# Only this tool's hook entries are touched (those whose command points at
# hook.py, or at the scripts\hook-wrapper.ps1 indirection used by older
# versions). Your own entries on the same events, all other hook events, and
# every other setting are left untouched.
[CmdletBinding()]
param([switch]$KeepData)

$ErrorActionPreference = "Stop"

# --- Stop the widget --------------------------------------------------------
# WM_CLOSE to its hidden tray window, so it removes its own icon from the
# notification area on the way out. Killing the process instead leaves a dead
# icon there until something makes the shell notice. Only if it will not go
# quietly do we kill it.
if (-not ("CSM.Win" -as [type])) {
    Add-Type -Namespace CSM -Name Win -MemberDefinition @'
[DllImport("user32.dll", CharSet = CharSet.Unicode)]
public static extern IntPtr FindWindowW(string cls, string title);
[DllImport("user32.dll")]
public static extern bool PostMessageW(IntPtr h, uint msg, IntPtr w, IntPtr l);
'@
}

function Get-WidgetProcess {
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match "widget\.pyw" }
}

$widget = @(Get-WidgetProcess)
if ($widget.Count -eq 0) {
    Write-Host "Widget:      not running"
} else {
    # Both class *and* title, because PowerShell marshals $null into a [string]
    # parameter as "" - and FindWindow then looks for a window whose title is
    # empty, finds nothing, and we silently fall through to killing the process.
    $hwnd = [CSM.Win]::FindWindowW("ClaudeSessionMonitorTray", "ClaudeSessionMonitorTray")
    if ($hwnd -ne [IntPtr]::Zero) {
        [CSM.Win]::PostMessageW($hwnd, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
    }
    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deadline -and @(Get-WidgetProcess).Count -gt 0) {
        Start-Sleep -Milliseconds 200
    }
    $left = @(Get-WidgetProcess)
    foreach ($proc in $left) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($left.Count -gt 0) {
        Write-Host "Widget:      did not respond, killed it (a stale tray icon may"
        Write-Host "             linger until you mouse over the notification area)"
    } else {
        Write-Host "Widget:      closed"
    }
}

# --- Unregister the hooks ---------------------------------------------------
# Every step below reports and carries on rather than exiting: a half-present
# install (hooks already gone, files still there) is exactly the state someone
# re-runs this in, and stopping early would strand the rest.
$settingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"
$settings = $null
if (Test-Path $settingsPath) {
    $settings = Get-Content $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
}

if (-not $settings) {
    Write-Host "Hooks:       no settings.json"
} elseif (-not ($settings.PSObject.Properties.Name -contains "hooks") -or -not $settings.hooks) {
    Write-Host "Hooks:       none configured"
} else {
    $hooks = $settings.hooks
    $OURS = "hook-wrapper\.ps1|hook\.py"
    $removed = 0

    foreach ($evt in @($hooks.PSObject.Properties.Name)) {
        $groups = @($hooks.$evt)
        $keep = @($groups | Where-Object {
            $_ -and -not ((ConvertTo-Json -InputObject $_ -Depth 20 -Compress) -match $OURS)
        })
        $removed += ($groups.Count - $keep.Count)

        if ($keep.Count -eq 0) {
            $hooks.PSObject.Properties.Remove($evt)
        } elseif ($keep.Count -ne $groups.Count) {
            $hooks | Add-Member -NotePropertyName $evt -NotePropertyValue $keep -Force
        }
    }

    # Drop the hooks object entirely if it is now empty.
    if (($hooks.PSObject.Properties | Measure-Object).Count -eq 0) {
        $settings.PSObject.Properties.Remove("hooks")
    }

    # See install.ps1: serialize the whole document at once so nested hook
    # arrays keep their shape.
    $json = ConvertTo-Json -InputObject $settings -Depth 20
    [IO.File]::WriteAllText($settingsPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Hooks:       removed $removed entr$(if ($removed -eq 1) { 'y' } else { 'ies' })"
}

# --- Files the tool created -------------------------------------------------
if ($KeepData) {
    Write-Host "Files:       kept (-KeepData)"
} else {
    $claude = Join-Path $env:USERPROFILE ".claude"
    $gone = @()
    foreach ($path in @((Join-Path $claude "session-status"),
                        (Join-Path $claude "session-monitor-config.json"),
                        (Join-Path $claude "session-monitor-hook.log"))) {
        if (Test-Path $path) {
            Remove-Item $path -Recurse -Force -ErrorAction SilentlyContinue
            if (-not (Test-Path $path)) { $gone += (Split-Path $path -Leaf) }
        }
    }
    if ($gone.Count -eq 0) {
        Write-Host "Files:       nothing left to remove"
    } else {
        Write-Host "Files:       removed $($gone -join ', ')"
    }
}

# --- Startup shortcut -------------------------------------------------------
# Only ones pointing at this copy of start-monitor.vbs: leaving it behind would
# bring the widget back at the next login, which is not what "uninstall" means.
$vbs = Join-Path $PSScriptRoot "start-monitor.vbs"
$startup = [Environment]::GetFolderPath("Startup")
$shell = New-Object -ComObject WScript.Shell
$unlinked = 0
foreach ($lnk in @(Get-ChildItem $startup -Filter *.lnk -ErrorAction SilentlyContinue)) {
    try { $target = $shell.CreateShortcut($lnk.FullName).TargetPath } catch { continue }
    if ($target -and $target -eq $vbs) {
        Remove-Item $lnk.FullName -Force -ErrorAction SilentlyContinue
        $unlinked++
    }
}
if ($unlinked) {
    Write-Host "Startup:     removed $unlinked shortcut$(if ($unlinked -eq 1) { '' } else { 's' })"
} else {
    Write-Host "Startup:     no shortcut to this copy"
}

Write-Host ""
Write-Host "Uninstalled." -ForegroundColor Green
Write-Host "Reload Claude Code so it stops trying to run the hooks."
Write-Host "The repository itself is untouched - delete the folder to finish."
