# uninstall.ps1 - remove Claude Session Monitor hooks from ~/.claude/settings.json
#
# Removes only the hook entries created by this tool (those whose command points
# at hook.py, or at the scripts\hook-wrapper.ps1 indirection used by older
# versions). Your own entries on the same events, all other hook events, and
# every other setting are left untouched.
$ErrorActionPreference = "Stop"

$settingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"
if (-not (Test-Path $settingsPath)) {
    Write-Host "No settings.json found - nothing to do."
    exit 0
}

$settings = Get-Content $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not ($settings.PSObject.Properties.Name -contains "hooks") -or -not $settings.hooks) {
    Write-Host "No hooks configured - nothing to do."
    exit 0
}
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

# See install.ps1: serialize the whole document at once so nested hook arrays
# keep their shape.
$json = ConvertTo-Json -InputObject $settings -Depth 20
[IO.File]::WriteAllText($settingsPath, $json, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Removed $removed hook entr$(if ($removed -eq 1) { 'y' } else { 'ies' }) from $settingsPath" -ForegroundColor Green
Write-Host "Reload Claude Code to apply. The widget can be closed with its x button."
