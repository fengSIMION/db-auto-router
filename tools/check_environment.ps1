# -*- coding: utf-8 -*-
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"
if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$checker = Join-Path $PSScriptRoot "check_environment.py"

function Get-EnvironmentReport {
    $json = & $PythonExe $checker --project-root $ProjectRoot
    if ($LASTEXITCODE -ne 0) {
        throw "环境检查器执行失败（退出码 $LASTEXITCODE）"
    }
    return ($json | ConvertFrom-Json)
}

try {
    $report = Get-EnvironmentReport
} catch {
    Write-Host "[错误] 无法检查 Python 环境：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

if (-not $report.python.compatible) {
    Write-Host "[错误] 当前 Python $($report.python.version)，项目要求 Python >= $($report.python.minimum)。" -ForegroundColor Red
    Write-Host "       请安装新版 Python 后重新双击 start_datasource_manager.bat。"
    exit 1
}

$missing = @($report.missing)
if ($missing.Count -eq 0) {
    exit 0
}

Write-Host ""
Write-Host "检测到缺少以下 Python 依赖：" -ForegroundColor Yellow
foreach ($item in $missing) {
    Write-Host "  - $($item.pip_spec)  [$($item.reason)]"
}
Write-Host ""
$answer = Read-Host "是否使用当前 Python 自动安装？[Y/N]"
if ($answer -notmatch '^(?i:y|yes)$') {
    Write-Host "已取消安装。请安装上述依赖后重新启动。" -ForegroundColor Yellow
    Write-Host "Python: $PythonExe"
    exit 2
}

$packages = @($missing | ForEach-Object { $_.pip_spec } | Select-Object -Unique)
Write-Host ""
Write-Host "正在安装依赖，使用解释器：$PythonExe" -ForegroundColor Cyan
& $PythonExe -m pip install --disable-pip-version-check @packages
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] pip 安装失败（退出码 $LASTEXITCODE）。请检查网络、代理或 Python 权限。" -ForegroundColor Red
    exit 1
}

try {
    $report = Get-EnvironmentReport
} catch {
    Write-Host "[错误] 安装后复检失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
if (@($report.missing).Count -gt 0) {
    Write-Host "[错误] 安装完成后仍有依赖不可用：" -ForegroundColor Red
    foreach ($item in @($report.missing)) {
        Write-Host "  - $($item.pip_spec)"
    }
    exit 1
}

Write-Host "依赖安装并复检通过。" -ForegroundColor Green
exit 0
