# Launch the floating widget with the windowless Python interpreter.
# Resolves paths relative to this script, so it works from any location.
$widget = Join-Path $PSScriptRoot "widget.pyw"

$pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pyw) { $pyw = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $pyw) {
    Write-Error "Python not found on PATH. Install Python 3 and try again."
    exit 1
}

Start-Process -FilePath $pyw -ArgumentList "`"$widget`""
