@echo off
cd /d "%~dp0"
py -3 "%~dp0挂播工具.py"
if errorlevel 1 pause
