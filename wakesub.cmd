@echo off
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
python -B -m wakesub %*
