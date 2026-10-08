@echo off
rem Universal Content Intake for Windows (bin/uci on macOS). Run without arguments for help. See README.md.
setlocal
set "PYTHONUTF8=1"
pushd "%~dp0.." || exit /b 2
if defined UCI_PYTHON goto custom
rem "python" may be the Microsoft Store placeholder, which cannot run anything; fall back to the py launcher.
python -c "import sys" >nul 2>nul
if errorlevel 1 goto launcher
python -m src.uci_cli %*
goto done
:launcher
py -3 -m src.uci_cli %*
goto done
:custom
"%UCI_PYTHON%" -m src.uci_cli %*
:done
set "CODE=%ERRORLEVEL%"
popd
exit /b %CODE%
