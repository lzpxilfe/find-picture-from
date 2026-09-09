@echo off
chcp 65001 > nul
setlocal EnableDelayedExpansion
title 사진찾기 - 바탕화면에 만들기
cd /d "%~dp0\.."

echo.
echo   ===================================================
echo     사진찾기.exe 를 만들어 바탕화면에 놓습니다.
echo     처음에는 3~10분쯤 걸립니다. 창을 닫지 마세요.
echo   ===================================================
echo.

rem ---- 1. 파이썬 확인 -------------------------------------------------
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    echo   [멈춤] 파이썬이 없습니다.
    echo.
    echo   https://www.python.org/downloads/ 에서 내려받아 설치해 주세요.
    echo   설치 첫 화면의 "Add Python to PATH" 를 꼭 체크해야 합니다.
    echo   설치한 뒤 이 파일을 다시 두 번 누르세요.
    echo.
    pause
    exit /b 1
)
echo   [1/4] 파이썬 확인
%PY% --version

rem ---- 2. 필요한 것 설치 ----------------------------------------------
echo.
echo   [2/4] 필요한 것 설치 중...
%PY% -m pip install --upgrade pip --quiet
%PY% -m pip install -e . --quiet
if errorlevel 1 goto :install_failed
%PY% -m pip install pyinstaller --quiet
if errorlevel 1 goto :install_failed

rem ---- 3. exe 만들기 --------------------------------------------------
echo.
echo   [3/4] exe 만드는 중... (가장 오래 걸립니다)
%PY% -m PyInstaller --noconfirm --clean 사진찾기.spec
if errorlevel 1 goto :build_failed
if not exist "dist\사진찾기.exe" goto :build_failed

rem ---- 4. 바탕화면으로 옮기기 -----------------------------------------
echo.
echo   [4/4] 바탕화면에 놓는 중...
set "DESKTOP="
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')" 2^>nul`) do set "DESKTOP=%%D"
if not defined DESKTOP set "DESKTOP=%USERPROFILE%\Desktop"
if not exist "!DESKTOP!" set "DESKTOP=%USERPROFILE%\Desktop"

copy /y "dist\사진찾기.exe" "!DESKTOP!\사진찾기.exe" >nul
if errorlevel 1 (
    echo.
    echo   바탕화면에 복사하지 못했습니다.
    echo   사진찾기 창이 이미 떠 있으면 닫고 다시 해 주세요.
    echo   그래도 안 되면 dist 폴더의 "사진찾기.exe" 를 직접 끌어다 놓으세요.
    explorer "%cd%\dist"
    pause
    exit /b 1
)

echo.
echo   ===================================================
echo     다 됐습니다.
echo     바탕화면의 "사진찾기" 를 두 번 누르면 실행됩니다.
echo   ===================================================
echo.
echo   * 처음 실행할 때 윈도우가 "PC를 보호했습니다" 라고 막을 수 있습니다.
echo     서명하지 않은 프로그램이라 그런 것이니,
echo     "추가 정보" 를 누른 뒤 "실행" 을 누르면 됩니다. 한 번만 하면 됩니다.
echo.
echo   * 이 폴더는 지워도 됩니다. exe 하나만 있으면 돌아갑니다.
echo.
explorer "!DESKTOP!"
pause
exit /b 0

:install_failed
echo.
echo   [멈춤] 필요한 것을 설치하지 못했습니다.
echo   인터넷 연결을 확인하거나, 이 파일을 오른쪽 클릭해
echo   "관리자 권한으로 실행" 으로 다시 해 보세요.
echo.
pause
exit /b 1

:build_failed
echo.
echo   [멈춤] exe 를 만들지 못했습니다. 위에 나온 메시지를 알려 주세요.
echo.
pause
exit /b 1
