# uninstall.ps1 - remove Claude Session Monitor hooks from ~/.claude/settings.json
#
# Removes only the hook events created by this tool (those whose command points
# at hook-wrapper.ps1); all other settings and hooks are left untouched.
$ErrorActionPreference = "Stop"

$settingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"
if (-not (Test-Path $settingsPath)) {
    Write-Host "No settings.json found - nothing to do."
    exit 0
}

$settings = Get-Content $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $settings.hooks) {
    Write-Host "No hooks configured - nothing to do."
    exit 0
}

$events = "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
          "PermissionRequest", "Notification", "Stop", "SessionEnd"
$removed = 0
foreach ($e in $events) {
    if ($settings.hooks.PSObject.Properties.Name -contains $e) {
        $json = $settings.hooks.$e | ConvertTo-Json -Depth 20
        if ($json -match "hook-wrapper\.ps1") {
            $settings.hooks.PSObject.Properties.Remove($e)
            $removed++
        }
    }
}

# Drop the hooks object entirely if it is now empty.
if (($settings.hooks.PSObject.Properties | Measure-Object).Count -eq 0) {
    $settings.PSObject.Properties.Remove("hooks")
}

$json = $settings | ConvertTo-Json -Depth 20
[IO.File]::WriteAllText($settingsPath, $json, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Removed $removed hook event(s) from $settingsPath" -ForegroundColor Green
Write-Host "Reload Claude Code to apply. The widget can be closed with its x button."
