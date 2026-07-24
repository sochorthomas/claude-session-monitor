param([string]$status = "working")

# Reliable hook launcher. On Windows, Claude Code runs hooks through PowerShell;
# invoking the Python hook via the call operator (&) runs it reliably and lets
# it inherit stdin (the JSON payload) directly. Paths are resolved relative to
# this script ($PSScriptRoot), so the tool works wherever it is cloned.

$hook = Join-Path $PSScriptRoot "hook.py"

# Prefer the windowless interpreter (no console flash); fall back to python.
$pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pyw) { $pyw = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $pyw) { exit 0 }  # Python not found - fail silently, never block Claude

& $pyw $hook $status
