@echo off
chcp 65001 > nul
title 한글 보고서 사진 원본 찾기
cd /d "%~dp0\.."

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   파이썬이 설치되어 있지 않습니다.
    echo   https://www.python.org/downloads/ 에서 설치한 뒤,
    echo   설치 화면에서 "Add Python to PATH" 를 꼭 체크해 주세요.
    echo.
    pause
    exit /b 1
)

python -c "import findpic" >nul 2>nul
if errorlevel 1 (
    echo.
    echo   처음 실행이라 필요한 것을 설치합니다. 잠시 기다려 주세요.
    echo.
    python -m pip install -e . || goto :failed
)

start "" pythonw -m findpic.gui
exit /b 0

:failed
echo.
echo   설치에 실패했습니다. 인터넷 연결을 확인하거나 관리자 권한으로 다시 실행해 보세요.
echo.
pause
exit /b 1
