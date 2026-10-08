@echo off
rem Universal Content Intake for Windows (bin\uci on macOS). Run without arguments for help. See README.md.
setlocal
set "PYTHONUTF8=1"
pushd "%~dp0.." || exit /b 2
if defined UCI_PYTHON goto custom
where py >/dev/null 2>nul
if errorlevel 1 goto plain
py -3 -m src.uci_cli %*
goto done
:custom
"%UCI_PYTHON%" -m src.uci_cli %*
goto done
:plain
python -m src.uci_cli %*
:done
set "CODE=%ERRORLEVEL%"
popd
exit /b %CODE%
