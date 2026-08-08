<#
.SYNOPSIS
    Remove the rumble-chat-chart scheduled task. Captured data is left alone.
#>
[CmdletBinding()]
param()

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $here "install-service.ps1") -Uninstall
