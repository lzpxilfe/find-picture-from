@echo off
chcp 65001 > nul
title 사진찾기.exe 만들기
cd /d "%~dp0\.."

echo.
echo   사진찾기.exe 를 만듭니다. 처음에는 몇 분 걸립니다.
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo   파이썬이 설치되어 있지 않습니다.
    echo   https://www.python.org/downloads/ 에서 설치한 뒤,
    echo   설치 화면에서 "Add Python to PATH" 를 꼭 체크해 주세요.
    pause
    exit /b 1
)

python -m pip install --upgrade pip
python -m pip install -e .
python -m pip install pyinstaller
if errorlevel 1 goto :failed

python -m PyInstaller --noconfirm 사진찾기.spec
if errorlevel 1 goto :failed

echo.
echo   다 됐습니다.
echo   dist 폴더 안에 있는 "사진찾기.exe" 를 바탕화면으로 끌어다 놓고 쓰세요.
echo   그 exe 하나만 있으면 됩니다. 파이썬이 없는 컴퓨터에서도 돌아갑니다.
echo.
explorer "%cd%\dist"
pause
exit /b 0

:failed
echo.
echo   만들지 못했습니다. 위에 나온 메시지를 확인해 주세요.
echo.
pause
exit /b 1
