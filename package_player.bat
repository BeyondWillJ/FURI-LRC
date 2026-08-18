@echo off
setlocal EnableExtensions

title Package Furi LRC Player

cd /d "%~dp0"

echo ============================================================
echo  Furi LRC Player Packager
echo ============================================================
echo.

if not exist "furi-lrc-player.py" (
    echo ERROR: furi-lrc-player.py was not found in this folder.
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
    echo Using venv: .venv\Scripts\python.exe
    goto :python_ok
)

where conda >nul 2>nul
if errorlevel 1 (
    echo ERROR: Neither venv nor Conda was found.
    pause
    exit /b 1
)

set "PYTHON=conda run -n rubi python"

echo Using Conda environment: rubi
%PYTHON% --version
if errorlevel 1 (
    echo ERROR: Conda environment "rubi" could not be started.
    pause
    exit /b 1
)

:python_ok
echo.

echo Checking PyInstaller...
%PYTHON% -m PyInstaller --version
if errorlevel 1 (
    echo ERROR: PyInstaller was not found in Conda environment "rubi".
    pause
    exit /b 1
)
echo.

echo Cleaning previous build output...
if exist "build" rmdir /s /q "build"
if exist "dist\furi-lrc-player" rmdir /s /q "dist\furi-lrc-player"
echo.

echo Building the application...
%PYTHON% -m PyInstaller --noconfirm --clean "furi-lrc-player.spec"

if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller failed.
    pause
    exit /b 1
)

echo.
echo Creating default folders in output...
mkdir "dist\furi-lrc-player\songs"  2>nul
mkdir "dist\furi-lrc-player\flrc"   2>nul
mkdir "dist\furi-lrc-player\flpls"  2>nul

echo Verifying bundled desktop-lyrics resources...
mkdir "dist\furi-lrc-player\player_data" 2>nul
copy /y "player_data\icon-player.ico" "dist\furi-lrc-player\player_data\" >nul

if not exist "dist\furi-lrc-player\_internal\furi-lrc_rubi.py" (
    echo ERROR: The desktop lyrics module was not bundled.
    echo        Check furi-lrc-player.spec and rebuild.
    pause
    exit /b 1
)

if not exist "dist\furi-lrc-player\_internal\fonts\NotoSerifJP-SemiBold.ttf" (
    echo ERROR: Desktop lyrics fonts were not bundled.
    echo        Check furi-lrc-player.spec and rebuild.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Build finished.
echo ============================================================
echo.
echo Output folder:
echo   %cd%\dist\furi-lrc-player
echo.
pause
exit /b 0
