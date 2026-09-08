@echo off
setlocal
py -3 "%~dp0pip_build.py" %*
exit /b %errorlevel%
