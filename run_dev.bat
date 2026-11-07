@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 不打包、直接运行源码（需要先运行过一次 build.bat 或自己建好 .venv）
if not exist ".venv\Scripts\python.exe" (
  echo 还没有 .venv，请先运行 build.bat
  pause
  exit /b 1
)
".venv\Scripts\python.exe" run.py
