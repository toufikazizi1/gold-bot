@echo off
chcp 65001 >nul
cd /d %~dp0
echo Installing MetaTrader5 package if needed...
python -m pip install -q -r requirements.txt
python bridge.py
pause
