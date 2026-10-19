# -*- coding: utf-8 -*-
<#
.SYNOPSIS
    在本机生成「数据源管理.lnk」（默认放桌面）

.DESCRIPTION
    为什么需要这个脚本：
        .lnk 里存的是绝对路径（pythonw.exe 在哪、图标在哪），没法像 .bat 那样用
        %~dp0 相对定位，所以**不能随文件夹一起分发**。把整个 db-auto-router 文件夹
        拷到新机器后，跑一次「生成快捷方式.bat」就能按那台机器的实际路径重建。

        不重建也完全能用——start_datasource_manager.bat 在任何装了 Python 的
        机器上都能直接双击运行，只是慢一点、且会闪一下终端窗口。

.PARAMETER Path
    指定 .lnk 输出位置，默认为桌面。

.EXAMPLE
    powershell -File tools\new_datasource_shortcut.ps1
    powershell -File tools\new_datasource_shortcut.ps1 -Path D:\my.lnk
#>

[CmdletBinding()]
param(
    [string]$Path = ""
)

$ErrorActionPreference = "Stop"

# ---- 定位项目根：本脚本在 <root>\tools\ 下 ----
$proj = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $proj "tools\manage_datasources_gui.py"))) {
    Write-Host "[错误] 找不到 tools\manage_datasources_gui.py，项目根解析失败：$proj" -ForegroundColor Red
    exit 1
}

# ---- 找 pythonw.exe：PATH 优先，再枚举各版本安装目录（新的优先） ----
$candidates = New-Object System.Collections.Generic.List[string]

$onPath = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($onPath) { $candidates.Add($onPath.Source) }

# 枚举 %LocalAppData%\Programs\Python\Python3xx，取版本最新的
$pyRoot = Join-Path $env:LOCALAPPDATA "Programs\Python"
if (Test-Path $pyRoot) {
    Get-ChildItem $pyRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^Python3(\d+)$' } |
        Sort-Object { [int]$Matches[1] } -Descending |
        ForEach-Object { $candidates.Add((Join-Path $_.FullName "pythonw.exe")) }
}

# 兜底：py 启动器（能列出所有已安装版本）
try {
    $pyList = & py -0p 2>$null
    foreach ($line in $pyList) {
        if ($line -match '-\S+\s+(.+pythonw\.exe)') { $candidates.Add($Matches[1].Trim()) }
    }
} catch { }

$pyw = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $pyw) {
    Write-Host "[错误] 找不到 pythonw.exe。" -ForegroundColor Red
    Write-Host "        请先安装 Python（安装时勾选 'Add to PATH'），或手动把 pythonw.exe 的完整路径写进 start_datasource_manager.bat。"
    exit 1
}
Write-Host "pythonw : $pyw"

# ---- 图标：用项目自带的，没有再退回 pythonw 自己的 ----
$ico = Join-Path $proj "tools\datasource_manager.ico"
if (-not (Test-Path $ico)) { $ico = $pyw }
Write-Host "icon    : $ico"

# ---- 目标 .lnk 位置 ----
if (-not $Path) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    if (-not $desktop) { $desktop = $env:USERPROFILE }
    $Path = Join-Path $desktop "数据源管理.lnk"
}

# ---- 生成 ----
$sh = New-Object -ComObject WScript.Shell
$lnk = $sh.CreateShortcut($Path)
$lnk.TargetPath        = $pyw
$lnk.Arguments        = "tools\manage_datasources_gui.py"
$lnk.WorkingDirectory = $proj
$lnk.IconLocation     = "$ico,0"
$lnk.Description      = "db-auto-router 数据源管理（直连 pythonw，无终端黑窗）"
$lnk.WindowStyle      = 1
$lnk.Save()

Write-Host ""
Write-Host "已生成快捷方式：" -ForegroundColor Green
Write-Host "  $Path"
Write-Host ""
Write-Host "双击即可启动；可右键『固定到任务栏』或用 Win+数字键唤起。"
Write-Host "换机器/换 Python 后重新跑一次本脚本即可（.lnk 存绝对路径，不能随文件夹分发）。"
