@echo off
REM ============================================================
REM  注意：本文件必须保存为 GBK(cp936) + CRLF 行尾。
REM  cmd 解析 UTF-8 中文批处理时，goto/call 会按错误的字节偏移
REM  跳转，导致从某行中间继续执行（经典乱码 + "'xxx' 不是内部
REM  或外部命令"）。改这个文件请用 GBK 重新保存，别存成 UTF-8。
REM ============================================================
chcp 936 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title buddy-context 用量看板

REM ============================================================
REM  看板启动器
REM
REM  为什么不能直接写 python dashboard.py：
REM  很多人机器上 PATH 里的 python 是 Microsoft Store 的占位程序
REM  （%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe），双击时
REM  它不启动 Python，而是弹应用商店。
REM
REM  所以这里做两道过滤：
REM    1. 按位置排除 —— 父目录是 WindowsApps 的直接跳过；
REM    2. 按行为验证 —— 真的执行一次，跑不通的也跳过。
REM  顺序上优先用 WorkBuddy 自带的 Python（一定存在且是完整版）。
REM ============================================================

set "PY="
set "PYSRC="

REM ---- 0) 环境变量显式指定，优先级最高 ----
if not "%WORKBUDDY_PYTHON%"=="" call :try "%WORKBUDDY_PYTHON%" "环境变量 WORKBUDDY_PYTHON"
if defined PY goto :run

REM ---- 1) WorkBuddy 自带 Python：~/.workbuddy/binaries/python ----
set "WBV=%USERPROFILE%\.workbuddy\binaries\python\versions"
set "CURVER="
if exist "%WBV%\current" set /p CURVER=<"%WBV%\current"
if defined CURVER call :try "%WBV%\%CURVER%\python.exe" "WorkBuddy 自带 Python %CURVER%"
if defined PY goto :run

for /f "delims=" %%D in ('dir /b /ad /o-n "%WBV%" 2^>nul') do (
  call :try "%WBV%\%%D\python.exe" "WorkBuddy 自带 Python %%D"
  if defined PY goto :run
)

REM ---- 2) WorkBuddy 安装目录里的内置 Python ----
for %%R in (
  "%ProgramFiles%\WorkBuddy"
  "%LOCALAPPDATA%\Programs\WorkBuddy"
  "%LOCALAPPDATA%\WorkBuddy"
) do (
  call :try "%%~R\resources\vendor\python\python.exe" "WorkBuddy 安装目录内置 Python"
  if defined PY goto :run
)

REM ---- 3) py 启动器：不受商店占位程序影响，先解析出真实 exe ----
for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do call :try "%%P" "py -3 启动器"
if defined PY goto :run

REM ---- 4) PATH 里的 python / python3（占位程序会在 :try 里被剔除）----
for /f "delims=" %%P in ('where python python3 2^>nul') do (
  call :try "%%P" "PATH 中的 python"
  if defined PY goto :run
)

REM ---- 5) 常见安装位置兜底 ----
call :try "D:\Anaconda\python.exe" "Anaconda"
if defined PY goto :run
call :try "%USERPROFILE%\anaconda3\python.exe" "Anaconda（用户目录）"
if defined PY goto :run
call :try "%ProgramData%\Anaconda3\python.exe" "Anaconda（ProgramData）"
if defined PY goto :run
call :try "%ProgramData%\miniconda3\python.exe" "Miniconda"
if defined PY goto :run

for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  call :try "%%~D\python.exe" "Python %%D"
  if defined PY goto :run
)
for /d %%D in ("C:\Python3*") do (
  call :try "%%~D\python.exe" "Python %%D"
  if defined PY goto :run
)

goto :nopython


REM ============================================================
REM  :try  候选 Python 体检 —— 通过就把 PY / PYSRC 定下来
REM        %1 = python.exe 路径    %2 = 来源说明
REM ============================================================
:try
set "CAND="
if not "%~1"=="" if exist "%~1" set "CAND=%~1"
if not defined CAND exit /b 0

REM 商店占位程序（0 字节的转发壳）就在这两个目录里，先按位置剔除
if /i "%~dp1"=="%LOCALAPPDATA%\Microsoft\WindowsApps\" exit /b 0
if /i "%~dp1"=="%ProgramFiles%\WindowsApps\" exit /b 0

REM 能真的跑起来才算数：占位程序会返回 9009，损坏的安装会报 DLL 错误
"%CAND%" -c "import sys" >nul 2>nul
if errorlevel 1 exit /b 0

set "PY=%CAND%"
set "PYSRC=%~2"
exit /b 0


REM ============================================================
REM  启动
REM ============================================================
:run
echo.
echo   用量看板启动中
echo   Python ：%PY%
echo   来源   ：%PYSRC%
echo.
"%PY%" dashboard.py %*
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  echo.
  echo   看板退出，返回码 %RC%。上面的报错信息可以截图反馈。
  echo.
  pause
)
endlocal & exit /b %RC%


:nopython
echo.
echo   没找到可用的 Python 3。
echo.
echo   已依次试过：WorkBuddy 自带 Python、WorkBuddy 安装目录、
echo               py 启动器、PATH 中的 python、常见安装位置。
echo.
echo   如果你知道 Python 装在哪，指定一次就行：
echo       set WORKBUDDY_PYTHON=C:\path\to\python.exe
echo       start.bat
echo.
pause
endlocal & exit /b 1
