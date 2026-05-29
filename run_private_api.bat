@echo off
cd /d %~dp0
if "%ELITE_DAYNIGHT_DB%"=="" set ELITE_DAYNIGHT_DB=%cd%\elite_daynight.db
if not exist "%ELITE_DAYNIGHT_DB%" if exist "%cd%\elite_daynight_template.db" copy "%cd%\elite_daynight_template.db" "%ELITE_DAYNIGHT_DB%"
if "%ELITE_API_PORT%"=="" set ELITE_API_PORT=8000
uvicorn elite_daynight_api:app --host 127.0.0.1 --port %ELITE_API_PORT% --workers 1
