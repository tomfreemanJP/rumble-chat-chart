<#
.SYNOPSIS
    Register rumble-chat-chart as a background scheduled task, running from source.

.DESCRIPTION
    A thin wrapper so there is one implementation of the task definition: the
    registration itself lives in rumble_chat_chart.py, which the installed build
    calls the same way. Use -Uninstall to remove it (captured data is left alone).
#>
[CmdletBinding()]
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"

$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "rumble_chat_chart.py"
if (-not (Test-Path $script)) { throw "rumble_chat_chart.py not found in $here" }

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw "python was not found on PATH." }

if ($Uninstall) { $verb = "uninstall-task" } else { $verb = "install-task" }
& $python.Source $script $verb

if (-not $Uninstall) {
    Write-Host ""
    Write-Host "  set your API key:  python `"$script`" configure"
    Write-Host "  check on it:       python `"$script`" status"
    Write-Host "  the log:           Get-Content '$(Join-Path $here 'data\rumble-chat-chart.log')' -Tail 30 -Wait"
}
