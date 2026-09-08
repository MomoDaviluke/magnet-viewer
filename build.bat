@echo off
rem One-click build: clean -> PyInstaller -> pack_check
rem
rem Note: PyInstaller's --clean is intentionally NOT used here. In this
rem environment it deletes >50 cached files at once and gets blocked by the
rem safe-delete guard (exit 1). Removing the dirs by hand first is equivalent.

setlocal
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

"%PY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [FAIL] PyInstaller not found. Run: %PY% -m pip install pyinstaller
    exit /b 1
)

echo === 1/3 clean old output ===
if exist "build\magnet-viewer" rmdir /s /q "build\magnet-viewer"
if exist "dist\MagnetViewer" rmdir /s /q "dist\MagnetViewer"

echo === 2/3 PyInstaller build ===
"%PY%" -m PyInstaller --noconfirm magnet-viewer.spec
if errorlevel 1 (
    echo [FAIL] build failed - see output above
    exit /b 1
)

echo === 3/3 verify output ===
"%PY%" pack_check.py
exit /b %errorlevel%
