@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto checkenv
where py >nul 2>&1
if errorlevel 1 goto fallback
py -3.13 -c "import sys" >nul 2>&1
if not errorlevel 1 goto python313
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 goto python312
py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 goto python311
:fallback
python -c "import sys; assert sys.version_info >= (3,11)" >nul 2>&1
if errorlevel 1 goto nopython
python -m venv .venv
if errorlevel 1 goto failed
goto checkenv
:python313
py -3.13 -m venv .venv
if errorlevel 1 goto failed
goto checkenv
:python312
py -3.12 -m venv .venv
if errorlevel 1 goto failed
goto checkenv
:python311
py -3.11 -m venv .venv
if errorlevel 1 goto failed
:checkenv
if not exist ".venv\Scripts\python.exe" goto failed
".venv\Scripts\python.exe" -c "import fastapi,uvicorn,sqlalchemy,alembic,pydantic_settings,python_multipart" >nul 2>&1
if not errorlevel 1 goto run
".venv\Scripts\python.exe" -m pip --disable-pip-version-check install -e .
if errorlevel 1 goto failed
:run
".venv\Scripts\python.exe" -m be2_backend.cli serve --open
if errorlevel 1 goto failed
exit /b 0
:nopython
echo Python 3.11 or newer was not found. Install Python, then run this file again.
goto waiterror
:failed
echo The application could not start. Read the error above.
:waiterror
pause
exit /b 1
