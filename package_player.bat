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
    echo Please run this script from the project folder.
    pause
    exit /b 1
)

where conda >nul 2>nul
if errorlevel 1 (
    echo ERROR: Conda was not found.
    echo Please install Anaconda or Miniconda, or add conda to PATH.
    pause
    exit /b 1
)

set "PYTHON=conda run -n rubi python"

echo Using Conda environment:
echo   rubi
%PYTHON% --version
if errorlevel 1 (
    echo ERROR: Conda environment "rubi" could not be started.
    echo Please create it first or check that the environment name is correct.
    pause
    exit /b 1
)
echo.

echo Checking PyInstaller...
%PYTHON% -m PyInstaller --version
if errorlevel 1 (
    echo ERROR: PyInstaller was not found in Conda environment "rubi".
    echo Please install PyInstaller in that environment, then run this script again.
    pause
    exit /b 1
)
echo.

echo Cleaning previous build output...
if exist "build" rmdir /s /q "build"
if exist "dist\furi-lrc-player" rmdir /s /q "dist\furi-lrc-player"
if exist "furi-lrc-player.spec" del /q "furi-lrc-player.spec"
echo.

echo Building the application...
%PYTHON% -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --windowed ^
    --onedir ^
    --name "furi-lrc-player" ^
    --add-data "furi-lrc_rubi.py;." ^
    --add-data "fonts;fonts" ^
    --add-data "settings.json;." ^
    --collect-all PyQt6 ^
    --collect-submodules mutagen ^
    --collect-submodules winsdk ^
    --hidden-import PyQt6.QtMultimedia ^
    --hidden-import PyQt6.QtMultimediaWidgets ^
    "furi-lrc-player.py"

if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller failed.
    echo Check the messages above for details.
    pause
    exit /b 1
)

echo Copying runtime resources beside the executable...
if exist "dist\furi-lrc-player\fonts" rmdir /s /q "dist\furi-lrc-player\fonts"
xcopy /e /i /y "fonts" "dist\furi-lrc-player\fonts" >nul
if errorlevel 1 (
    echo ERROR: Could not copy the fonts folder.
    pause
    exit /b 1
)

copy /y "furi-lrc_rubi.py" "dist\furi-lrc-player\" >nul
if errorlevel 1 (
    echo ERROR: Could not copy furi-lrc_rubi.py.
    pause
    exit /b 1
)

if exist "settings.json" copy /y "settings.json" "dist\furi-lrc-player\" >nul
if exist "_last_playlist.flpl" copy /y "_last_playlist.flpl" "dist\furi-lrc-player\" >nul

echo.
echo ============================================================
echo  Build finished.
echo ============================================================
echo.
echo Output folder:
echo   %cd%\dist\furi-lrc-player
echo.
echo Start the packaged app with:
echo   %cd%\dist\furi-lrc-player\furi-lrc-player.exe
echo.
pause
exit /b 0
