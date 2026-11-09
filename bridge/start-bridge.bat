@echo off
chcp 65001 >nul
cd /d %~dp0
echo Installing MetaTrader5 package if needed...
py -3.12 -m pip install -q -r requirements.txt
py -3.12 bridge.py
pause
