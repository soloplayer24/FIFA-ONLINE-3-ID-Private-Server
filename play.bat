@echo off
rem One click: start the FO3ID private server (own window; close it to stop the server)
rem and then the game. Setup first (README): Python + cryptography, hosts entry
rem "127.0.0.1 APSEODESK172", certs\server.pem, gamedata\fifadb.sqlite and
rem launcher\fifalauncher.exe built from launcher\fifalauncher.cs.
cd /d "%~dp0"
start "FO3ID server" python start_all.py
timeout /t 4 /nobreak >nul
rem RunAsInvoker: start the game without the UAC prompt (it runs fine unelevated)
set __COMPAT_LAYER=RunAsInvoker
start "" "%~dp0launcher\fifalauncher.exe"
