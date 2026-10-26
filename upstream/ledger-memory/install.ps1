#requires -Version 7.2
<#
Install dependencies in this extracted package's .venv. Requires uv on PATH.
No memory initialization, client configuration, PATH changes or model downloads.
Use -SemanticDependencies to install the optional inference libraries only.
#>
[CmdletBinding()]
param(
    [switch] $SemanticDependencies,
    [ValidateRange(1, 3600)]
    [int] $TimeoutSeconds = 1200
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'This installer requires Windows and PowerShell 7.2+.' }
$root = [System.IO.Path]::GetFullPath($PSScriptRoot)
foreach ($name in @('pyproject.toml', 'uv.lock')) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $name) -PathType Leaf)) {
        throw "Package is incomplete: missing $name."
    }
}
$environmentPath = Join-Path $root '.venv'
$item = $null
try {
    $item = Get-Item -LiteralPath $environmentPath -Force -ErrorAction Stop
} catch [System.Management.Automation.ItemNotFoundException] {
    # A missing environment is allowed; inspection/access errors are not.
} catch {
    throw 'Cannot inspect package .venv; installation refused.'
}
if ($null -ne $item) {
    if (-not $item.PSIsContainer -or
        ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw 'Package .venv must be a local directory, not a link or file.'
    }
}
$uv = Get-Command -Name uv -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -eq $uv) { throw 'uv is required. Install uv from its official distribution, then retry.' }

$info = [System.Diagnostics.ProcessStartInfo]::new()
$info.FileName = $uv.Source
$info.WorkingDirectory = $root
$info.UseShellExecute = $false
# Inherit output streams: waiting on undrained redirected pipes can deadlock.
$info.RedirectStandardOutput = $false
$info.RedirectStandardError = $false
foreach ($name in @('VIRTUAL_ENV', 'PYTHONHOME', 'PYTHONPATH', 'UV_PROJECT',
                    'UV_WORKING_DIR', 'UV_PYTHON', 'UV_PYTHON_ENVIRONMENT')) {
    [void] $info.Environment.Remove($name)
}
$info.Environment['UV_PROJECT_ENVIRONMENT'] = $environmentPath
foreach ($argument in @('sync', '--frozen', '--inexact', '--no-dev', '--project', $root)) {
    [void] $info.ArgumentList.Add($argument)
}
if ($SemanticDependencies) {
    [void] $info.ArgumentList.Add('--extra')
    [void] $info.ArgumentList.Add('semantic')
}

$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $info
$started = $false
try {
    $started = $process.Start()
    if (-not $started) { throw 'Dependency installation could not start.' }
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        throw 'Dependency installation exceeded its deadline; no model was requested.'
    }
    if ($process.ExitCode -ne 0) {
        throw "Dependency installation failed (exit $($process.ExitCode)); inspect uv output above."
    }
} finally {
    try {
        if ($started -and -not $process.HasExited) {
            try { $process.Kill($true) } catch {
                # Exit may race with Kill; still attempt the bounded reap below.
            }
            if (-not $process.WaitForExit(5000)) {
                throw 'Installer process could not be reaped; inspect running processes before retrying.'
            }
        }
    } finally {
        $process.Dispose()
    }
}

foreach ($name in @('ledger.exe', 'ledger-mcp.exe')) {
    $executable = Join-Path $environmentPath "Scripts\$name"
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "Installed command missing: $name. Installation is incomplete."
    }
    Write-Output $executable
}
Write-Output 'Dependencies installed. No memory, client settings or PATH were changed.'
Write-Output 'Model download remains a separate explicit opt-in: ledger semantic enable.'
Write-Output 'Native Windows, corporate proxy and antivirus acceptance remain unverified.'
