@echo off
rem ==============================================================
rem  网易云评论检索器 —— Windows 一键启动（双击本文件即可）
rem
rem  · 不需要安装任何第三方 Python 库
rem  · 服务只监听本机 127.0.0.1，不对局域网/外网开放
rem  · 关闭这个窗口即可停止服务
rem ==============================================================
chcp 936 >nul
title 网易云评论检索器
cd /d "%~dp0"

if "%~1"=="--check" (set "CHECK=--check") else (set "CHECK=")

if not exist "scripts\launch.py" goto :broken

set "PYCMD="
call :probe py -3
if not defined PYCMD call :probe py
if not defined PYCMD call :probe python
if not defined PYCMD call :probe python3

rem 以下为常见安装位置（还没加进 PATH 时也能找到）
if not defined PYCMD call :probe_abs "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYCMD call :probe_abs "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYCMD call :probe_abs "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYCMD call :probe_abs "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PYCMD call :probe_abs "%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
if not defined PYCMD call :probe_abs "%ProgramFiles%\Python313\python.exe"
if not defined PYCMD call :probe_abs "%ProgramFiles%\Python312\python.exe"
if not defined PYCMD call :probe_abs "%ProgramFiles%\Python311\python.exe"
if not defined PYCMD call :probe_abs "%ProgramFiles%\Python310\python.exe"
if not defined PYCMD call :probe_abs "%ProgramFiles%\Python39\python.exe"
if not defined PYCMD call :probe_abs "C:\Python313\python.exe"
if not defined PYCMD call :probe_abs "C:\Python312\python.exe"
if not defined PYCMD call :probe_abs "C:\Python311\python.exe"
if not defined PYCMD call :probe_abs "C:\Python310\python.exe"
if not defined PYCMD call :probe_abs "C:\Python39\python.exe"
if not defined PYCMD goto :nopython

%PYCMD% "scripts\launch.py" %CHECK%
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%

rem ------------------------------------------------------------ 子过程
:probe
rem %1 = 命令名，%2 = 附加参数（如 -3）
if defined PYCMD exit /b 0
where %1 >nul 2>nul
if errorlevel 1 exit /b 0
%1 %2 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if errorlevel 1 exit /b 0
set "PYCMD=%1 %2"
exit /b 0

:probe_abs
rem %1 = python.exe 的绝对路径
if defined PYCMD exit /b 0
if not exist %1 exit /b 0
%1 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if errorlevel 1 exit /b 0
set "PYCMD=%1"
exit /b 0

:nopython
echo.
echo  ==============================================================
echo   未找到 Python（需要 3.8 或更高版本）
echo  ==============================================================
echo.
echo  请任选一种方式安装，装好后重新双击本文件：
echo.
echo    方式一：打开 https://www.python.org/downloads/
echo            下载 Windows 安装包。安装第一步务必勾选
echo            "Add python.exe to PATH"，然后一路 Next 到底。
echo.
echo    方式二：打开「Microsoft Store」，搜索 "Python 3.12" 安装。
echo.
echo  安装过程中遇到问题，可参考同目录下的「使用说明.md」。
echo.
pause
exit /b 1

:broken
echo.
echo  ==============================================================
echo   文件不完整：缺少 scripts\launch.py
echo  ==============================================================
echo.
echo  请确认整个文件夹是完整解压出来的，不要只复制其中几个文件。
echo.
pause
exit /b 1
