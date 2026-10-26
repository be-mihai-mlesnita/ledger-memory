#requires -Version 7.2
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'run.ps1') 'uninstall' @args
exit $LASTEXITCODE
