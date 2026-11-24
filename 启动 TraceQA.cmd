@echo off
setlocal
pushd "%~dp0"
set "PYTHONUTF8=1"

if exist "TraceQA.exe" (
    "TraceQA.exe" %*
    goto finished
)

if not exist "frontend\dist\index.html" (
    echo TraceQA needs a built frontend. Run npm ci and npm run build in the frontend folder, or use the portable release.
    pause
    popd
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "packaging\launcher.py" %*
    goto finished
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "packaging\launcher.py" %*
    goto finished
)
python "packaging\launcher.py" %*

:finished
set "traceqa_exit=%errorlevel%"
if not "%traceqa_exit%"=="0" pause
popd
exit /b %traceqa_exit%
