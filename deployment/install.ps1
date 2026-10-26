#requires -Version 7.2
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'run.ps1') 'install' @args
exit $LASTEXITCODE
