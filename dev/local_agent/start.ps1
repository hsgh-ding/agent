param(
    [string]$PythonExe = "",
    [string]$Config = "$PSScriptRoot\config.example.json",
    [string]$SessionDir = "",
    [string]$Testbench = "",
    [switch]$ShowThinking
)
$ErrorActionPreference = 'Stop'
if (-not $PythonExe) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source -notmatch 'WindowsApps') {
        $PythonExe = $pythonCommand.Source
    } else {
        $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
        if (Test-Path -LiteralPath $bundledPython) { $PythonExe = $bundledPython }
    }
}
if (-not $PythonExe) { throw 'Python 3.10+ not found. Use -PythonExe with your Python path.' }
$chatArguments = @('-B', '-X', 'utf8', 'main.py', 'chat', '--config', $Config)
if ($SessionDir) { $chatArguments += @('--session-dir', $SessionDir) }
if ($Testbench) { $chatArguments += @('--testbench', $Testbench) }
if ($ShowThinking) { $chatArguments += '--show-thinking' }
Push-Location $PSScriptRoot
try {
    & $PythonExe @chatArguments
    $runExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $runExitCode
