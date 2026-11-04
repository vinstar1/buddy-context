@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
  python dashboard.py %*
  goto :end
)

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 dashboard.py %*
  goto :end
)

where python3 >nul 2>nul
if %errorlevel%==0 (
  python3 dashboard.py %*
  goto :end
)

echo 未找到 Python，请先安装 Python 3.8 或更高版本。
pause

:end
