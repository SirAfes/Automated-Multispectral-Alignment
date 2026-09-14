@echo off
setlocal
cd /d "%~dp0\.."

echo ============================================================
echo  Automated Multispectral Alignment - Windows EXE Builder
echo ============================================================
echo.

where py >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python launcher ^(py^) was not found.
    echo Install Python 3.11-3.13 from python.org and enable the launcher.
    pause
    exit /b 1
)

if not exist ".buildvenv\Scripts\python.exe" (
    echo [1/5] Creating isolated build environment...
    py -3 -m venv .buildvenv
    if errorlevel 1 goto :fail
) else (
    echo [1/5] Build environment already exists.
)

echo [2/5] Updating pip...
".buildvenv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo [3/5] Installing dependencies...
".buildvenv\Scripts\python.exe" -m pip uninstall -y opencv-python opencv-contrib-python opencv-python-headless >nul 2>nul
".buildvenv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 goto :fail

echo [4/5] Verifying OpenCV AKAZE support...
".buildvenv\Scripts\python.exe" -c "import cv2,sys; print('OpenCV:',cv2.__version__); print('AKAZE:',hasattr(cv2,'AKAZE_create')); sys.exit(0 if hasattr(cv2,'AKAZE_create') else 1)"
if errorlevel 1 (
    echo ERROR: Installed OpenCV does not provide AKAZE_create.
    goto :fail
)

echo [5/5] Building standalone executable...
if exist build-output rmdir /s /q build-output
if exist build-work rmdir /s /q build-work
".buildvenv\Scripts\python.exe" -m PyInstaller --clean --noconfirm --distpath build-output --workpath build-work build\Automated_Multispectral_Alignment.spec
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo BUILD COMPLETE
echo EXE: %CD%\build-output\Automated_Multispectral_Alignment.exe
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo BUILD FAILED. Review the error shown above.
pause
exit /b 1
