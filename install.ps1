# install.ps1 - register Claude Session Monitor hooks in ~/.claude/settings.json
#
# Adds the hook entries Claude Code needs to report each session's status to the
# widget. Existing settings (and unrelated hooks) are preserved. Safe to re-run.
#
# Note: Windows PowerShell's ConvertTo-Json unwraps single-element arrays, which
# would produce the wrong hook shape. We therefore serialize the surrounding
# settings normally but inject each event's hook array as pre-built JSON.
$ErrorActionPreference = "Stop"

$wrapper = Join-Path $PSScriptRoot "hook-wrapper.ps1"
$settingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"

function New-HookArrayJson([string]$status, [string]$matcher) {
    $raw = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$wrapper`" $status"
    $esc = $raw.Replace('\', '\\').Replace('"', '\"')
    $inner = "{`"type`":`"command`",`"command`":`"$esc`"}"
    if ($matcher) {
        return "[{`"matcher`":`"$matcher`",`"hooks`":[$inner]}]"
    }
    return "[{`"hooks`":[$inner]}]"
}

# Event -> (status, matcher). See README "How it works".
$events = [ordered]@{
    SessionStart      = @{ status = "action";     matcher = $null }
    UserPromptSubmit  = @{ status = "working";    matcher = $null }
    PostToolUse       = @{ status = "working";    matcher = "*"  }
    PermissionRequest = @{ status = "permission"; matcher = "*"  }
    Notification      = @{ status = "permission"; matcher = $null }
    Stop              = @{ status = "action";     matcher = $null }
    SessionEnd        = @{ status = "end";        matcher = $null }
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

# Put unique placeholders in for each event, serialize, then swap the
# placeholders for real hook arrays (guarantees array shape).
$placeholders = [ordered]@{}
foreach ($e in $events.Keys) { $placeholders[$e] = "@@$e@@" }
$settings | Add-Member -NotePropertyName hooks -NotePropertyValue $placeholders -Force

$json = $settings | ConvertTo-Json -Depth 20
foreach ($e in $events.Keys) {
    $arr = New-HookArrayJson $events[$e].status $events[$e].matcher
    $json = $json.Replace("`"@@$e@@`"", $arr)
}

[IO.File]::WriteAllText($settingsPath, $json, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Installed hooks into $settingsPath" -ForegroundColor Green
Write-Host "Wrapper: $wrapper"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1) Reload Claude Code so it picks up the hooks"
Write-Host "     (VS Code: Ctrl+Shift+P -> Developer: Reload Window)."
Write-Host "  2) Start the widget: double-click start-monitor.vbs"
