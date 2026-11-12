@echo off
rem workos.cmd - call work-os (engine\cli.py) without typing its full path. Windows.
rem
rem   Add this directory (<work-os>\scripts) to PATH. No link is needed.
rem   Mac / Linux use scripts/workos in the same directory.
rem
rem This file is ASCII only with CRLF line endings: cmd.exe reads a batch file in the
rem console code page (cp932 on Japanese Windows), not UTF-8.
rem
rem The work tree is found from where this file sits, as in scripts/workos.
rem PYTHONUTF8=1: under cp932, printing U+2014 fails and git output with UTF-8 file
rem names is misread. It is an environment variable so that the child processes
rem cli.py starts inherit it (-X utf8 would apply to one process only).
rem
rem   WORKOS_HOME    the work tree (default: one level above this file)
rem   WORKOS_PYTHON  the python to run (default: py -3)
setlocal
if not defined WORKOS_HOME set "WORKOS_HOME=%~dp0.."
rem No ( ) block here: a path such as "Program Files (x86)" would close it early.
if not exist "%WORKOS_HOME%\engine\cli.py" echo workos: "%WORKOS_HOME%\engine\cli.py" not found. Point WORKOS_HOME at the work tree. 1>&2
if not exist "%WORKOS_HOME%\engine\cli.py" exit /b 1
if not defined WORKOS_PYTHON set "WORKOS_PYTHON=py -3"
set "PYTHONUTF8=1"
%WORKOS_PYTHON% "%WORKOS_HOME%\engine\cli.py" %*
exit /b %ERRORLEVEL%
