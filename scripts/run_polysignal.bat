@echo off
rem POLYSIGNAL — permanent runner for Windows Task Scheduler / NSSM.
rem Auto-restarts the engine on crash; logs append to logs\engine.log.
cd /d %~dp0\..
if not exist logs mkdir logs
:loop
python scripts\run_engine.py --with-delivery >> logs\engine.log 2>&1
echo [%date% %time%] engine exited (code %errorlevel%) — restart in 5s >> logs\engine.log
timeout /t 5 /nobreak > nul
goto loop
