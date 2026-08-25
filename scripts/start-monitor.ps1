# Launch the floating widget with the windowless Python interpreter.
#
# Normally started by start-monitor.vbs in the repository root (which runs this
# hidden, without a console window). Run it directly only to see errors while
# troubleshooting. Paths resolve relative to this script, so the tool works from
# any location.
$widget = Join-Path (Split-Path $PSScriptRoot -Parent) "widget.pyw"

$pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pyw) { $pyw = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $pyw) {
    Write-Error "Python not found on PATH. Install Python 3 and try again."
    exit 1
}

Start-Process -FilePath $pyw -ArgumentList "`"$widget`""
