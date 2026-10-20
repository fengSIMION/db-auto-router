@echo off
rem Launcher: datasource manager GUI (v4.8: one entry point, refreshes the desktop shortcut)
rem
rem PORTABILITY: every path here resolves relative to this file (%~dp0), and
rem pythonw is located via PATH - so the whole folder can be copied as-is to any
rem machine with Python installed and this .bat just works.
rem
rem What happens on each launch:
rem   1. locate pythonw.exe (PATH scan, then `where` fallback)
rem   2. start the GUI (tools\manage_datasources_gui.py)
rem   3. refresh the desktop shortcut (.lnk) - best effort, never blocks the GUI.
rem      A .lnk stores ABSOLUTE paths so it cannot ship inside the folder;
rem      rebuilding it on every launch is what makes the folder "copy to a new
rem      PC and it just works" - nothing to remember, no second script to run.
rem
rem Failure policy: if the shortcut refresh fails (no PowerShell, execution
rem policy, ...) we print one hint line and continue. The GUI is the product;
rem the shortcut is a convenience.
rem
rem Custom shortcut location (default: Desktop), run the generator directly:
rem   powershell -NoProfile -ExecutionPolicy Bypass -File tools\new_datasource_shortcut.ps1 -Path D:\my.lnk
rem
rem NOTE: this file is intentionally all-ASCII. Chinese text in a .bat gets
rem mangled (or worse) under non-UTF-8 console codepages; the Chinese UX lives
rem in the GUI itself and in the shortcut name, not in this launcher.
rem
rem Migration notes (for a new PC):
rem   - Copy this whole folder as-is; every path is resolved relative to this
rem     folder (nothing hardcoded to the old machine)
rem   - If datasources.db does not exist, first launch auto-creates an empty one
rem   - Requires Python installed with "Add to PATH" checked
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
    if not defined PYW set "PYW=%%~I"
  )
)

if not defined PYW (
  echo [ERROR] pythonw not found. Please install Python and check "Add to PATH".
  echo         Or edit this .bat and put the full path to pythonw.exe on the start line.
  pause
  exit /b 1
)

rem 1) GUI first - it is the product, start it before anything slow
start "" "%PYW%" tools\manage_datasources_gui.py

rem 2) then refresh the desktop shortcut: hidden window, output swallowed,
rem    failure only hints - never blocks the GUI that is already running.
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0tools\new_datasource_shortcut.ps1" >nul 2>&1
if errorlevel 1 (
  echo [HINT] Desktop shortcut refresh failed - the GUI runs fine without it.
  echo        To retry manually: powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\new_datasource_shortcut.ps1"
)
endlocal
