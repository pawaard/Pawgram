@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "Pawgram.exe" (
  echo Pawgram.exe bulunamadi.
  echo Lutfen teslim klasorunun tamamini ayni konumda tutun.
  pause
  exit /b 1
)
"%~dp0Pawgram.exe"
