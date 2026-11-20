@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 or newer is required. Install it from python.org and rerun this launcher.
  pause
  exit /b 1
)
python -c "import fastapi,uvicorn,webview" >nul 2>nul
if errorlevel 1 (
  echo Installing ClipForge desktop dependencies...
  python -m pip install -r requirements.txt
  if errorlevel 1 (echo Dependency installation failed.&pause&exit /b 1)
)
python launch.py
