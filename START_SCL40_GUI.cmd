@echo off
setlocal
cd /d "%~dp0"
if not exist "%SCL40_PY%" set "SCL40_PY="
if not defined SCL40_PY where py >nul 2>nul && set "SCL40_PY=py -3"
if not defined SCL40_PY where python >nul 2>nul && set "SCL40_PY=python"
if not defined SCL40_PY (
  echo Python 3 was not found.
  echo Install Python 3 or run scl40_gui.py with a Python interpreter.
  pause
  exit /b 1
)
%SCL40_PY% scl40_gui.py 192.168.200.99 --enable-control --bind 0.0.0.0 --access-pin-file scl40_access_pin.txt
if errorlevel 1 pause
