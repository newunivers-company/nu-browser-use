@echo off
REM Entry point for the scheduled collection cadence (schtasks calls this).
REM
REM schtasks can invoke python directly, but then a task that never fires and a
REM task that fires and collects nothing leave the same trace: none. That
REM ambiguity has cost this project real time already, so every run appends to a
REM dated log whatever happens, including the failure to start python at all.
REM
REM collect_cycle.py writes the structured per-step record; this file exists for
REM the failures that happen before it can.
REM
REM Usage: run_cycle.cmd --daily   |   run_cycle.cmd --weekly

setlocal
set "CADENCE=%~1"
if "%CADENCE%"=="" set "CADENCE=--daily"

set "HERE=%~dp0"
set "REPO=%HERE%..\.."
set "PY=%REPO%\.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

set "LOGDIR=%USERPROFILE%\collect_cycle\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "DAY=%%d"
set "LOG=%LOGDIR%\%DAY%%CADENCE%.log"

echo. >> "%LOG%"
echo ==== %DATE% %TIME% starting %CADENCE% ==== >> "%LOG%"

if not exist "%PY%" (
	echo FATAL: no interpreter at %PY% >> "%LOG%"
	exit /b 2
)

REM %* not %1: the log name keys off the cadence, but extra flags must reach the
REM cycle. Without this there was no way to exercise this wrapper under Task
REM Scheduler except by running a full live cadence, so the path that actually
REM fires at 05:00 had never been tested end to end.
"%PY%" "%HERE%collect_cycle.py" %* >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo ==== %DATE% %TIME% finished %CADENCE% rc=%RC% ==== >> "%LOG%"
exit /b %RC%
