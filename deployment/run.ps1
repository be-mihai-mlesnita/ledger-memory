#requires -Version 7.2
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$forward = @($args)
$python = $null

function Test-Interpreter([string] $candidate) {
    try {
        & $candidate -I -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

if ($env:BEMIND_DEPLOY_PYTHON) {
    if (-not (Test-Interpreter $env:BEMIND_DEPLOY_PYTHON)) {
        Write-Error 'BEMIND_DEPLOY_PYTHON must point to a working Python 3.11+.'
        exit 1
    }
    $python = $env:BEMIND_DEPLOY_PYTHON
}

if (-not $python) {
    $uv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($uv) {
        $candidate = (& $uv.Source --no-config python find --no-project --system --no-python-downloads --offline '>=3.11' 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $candidate -and (Test-Interpreter $candidate)) {
            $python = $candidate
        }
    }
}

if (-not $python) {
    foreach ($name in @('python3.13', 'python3.12', 'python3.11', 'python3', 'python')) {
        $candidate = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($candidate -and $candidate.Source -notlike '*\Microsoft\WindowsApps\*' -and (Test-Interpreter $candidate.Source)) {
            $python = $candidate.Source
            break
        }
    }
}

if (-not $python) {
    Write-Error 'Python 3.11+ is required. With uv installed, run: uv python install 3.13. Nothing was installed by this launcher.'
    exit 1
}

& $python -I -B (Join-Path $PSScriptRoot 'bemind_deploy.py') @forward
exit $LASTEXITCODE
