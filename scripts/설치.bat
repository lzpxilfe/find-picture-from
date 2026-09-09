@echo off
chcp 65001 > nul
title findpic 설치
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

echo.
echo   findpic 을 설치합니다.
echo.
python -m pip install --upgrade pip
python -m pip install -e .
if errorlevel 1 goto :failed

echo.
echo   설치가 끝났습니다.
echo   이제 scripts 폴더의 "사진찾기.bat" 을 두 번 누르면 실행됩니다.
echo   명령 프롬프트에서는 findpic 이라고 쳐도 됩니다.
echo.
pause
exit /b 0

:failed
echo.
echo   설치에 실패했습니다. 위에 나온 메시지를 확인해 주세요.
echo.
pause
exit /b 1
