@echo off
rem Rebuild "datasource manager.lnk" for THIS machine (run once after copying the
rem folder to a new PC, or after changing Python install).
rem
rem Why this exists: a .lnk stores absolute paths (where pythonw.exe is, where the
rem icon is), so it cannot travel inside the folder the way this .bat does.
rem start_datasource_manager.bat stays fully portable - this is only for the
rem faster, console-free desktop shortcut.
rem
rem Output: 数据源管理.lnk on the Desktop (override with -Path, see the .ps1).
setlocal
cd /d "%~dp0"

rem 65001 so the .ps1's Chinese output renders on non-UTF-8 consoles too
chcp 65001 >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\new_datasource_shortcut.ps1" %*
if errorlevel 1 (
  echo.
  echo [FAILED] Could not build the shortcut - see the message above.
  pause
  exit /b 1
)
echo.
pause
endlocal
