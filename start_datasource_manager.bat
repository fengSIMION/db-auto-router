@echo off
rem Launcher: datasource manager GUI (v4.7, follows .env MCP_CONFIG_MODE)
rem
rem PORTABILITY: every path here resolves relative to this file (%~dp0), and
rem pythonw is located via PATH - so the whole folder can be copied as-is to any
rem machine with Python installed and this .bat just works.
rem
rem v4.7 changes:
rem   1) The `where pythonw` pre-check became a pure-cmd PATH scan (for + if
rem      exist), which drops one process spawn per launch. If the scan finds
rem      nothing we still fall back to `where` so a PATH entry containing
rem      & ^ ( ) can never produce a false "not found".
rem   2) For a faster, console-free launch, run 生成快捷方式.bat once. It puts
rem      数据源管理.lnk on the Desktop pointing straight at pythonw.exe
rem      (skips the cmd.exe hop and the console flash; also pinnable).
rem      NOTE: a .lnk stores ABSOLUTE paths, so it cannot ship inside the folder.
rem      Re-run the generator after copying to a new PC. This .bat stays the
rem      zero-setup, fully portable fallback.
rem
rem Migration notes (for a new PC):
rem   - Copy this whole folder as-is; every path is resolved relative to this
rem     folder (nothing hardcoded to the old machine)
rem   - If datasources.db does not exist, first launch auto-creates an empty one
rem   - Requires Python installed with "Add to PATH" checked
rem   - Optional: run 生成快捷方式.bat for the faster desktop shortcut
cd /d "%~dp0"

setlocal enabledelayedexpansion
set "PYW="

rem Fast path: scan PATH with cmd builtins only - no where.exe process spawn
for %%P in ("%PATH:;=" "%") do (
  if exist "%%~P\pythonw.exe" (
    if not defined PYW set "PYW=%%~P\pythonw.exe"
  )
)

rem Definitive fallback: let where.exe have the final say
if not defined PYW (
  for /f "delims=" %%I in ('where pythonw 2^>nul') do (
    if not defined PYW set "PYW=%%I"
  )
)

if not defined PYW (
  echo [ERROR] pythonw not found. Please install Python and check "Add to PATH".
  echo         Or edit this .bat and put the full path to pythonw.exe on the start line.
  pause
  exit /b 1
)

start "" "%PYW%" tools\manage_datasources_gui.py
endlocal
