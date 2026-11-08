@echo off
rem 双击即可运行：把 src 加进 PYTHONPATH 后启动界面，不要求先 pip install。
setlocal
cd /d "%~dp0.."
if "%PYTHONPATH%"=="" (
  set "PYTHONPATH=%CD%\src"
) else (
  set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
)
python -m ui.app %*
if errorlevel 1 pause
endlocal
