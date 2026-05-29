@echo off
cd /d %~dp0
if "%ELITE_DAYNIGHT_API_URL%"=="" set ELITE_DAYNIGHT_API_URL=http://127.0.0.1:8000
if "%ELITE_DAYNIGHT_DB%"=="" set ELITE_DAYNIGHT_DB=%cd%\elite_daynight.db
if "%ELITE_WEBSITE_HOST%"=="" set ELITE_WEBSITE_HOST=127.0.0.1
if "%ELITE_WEBSITE_PORT%"=="" set ELITE_WEBSITE_PORT=8080
uvicorn elite_daynight_website:app --host %ELITE_WEBSITE_HOST% --port %ELITE_WEBSITE_PORT% --workers 1
