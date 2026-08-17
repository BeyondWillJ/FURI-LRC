@echo off
setlocal EnableExtensions

title Package Furi LRC GUI

cd /d "%~dp0"

echo ============================================================
echo  Furi LRC GUI Packager
echo ============================================================
echo.

if not exist "furi-lrc-gui.py" (
    echo ERROR: furi-lrc-gui.py was not found in this folder.
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
    echo ERROR: PyInstaller was not found. Install it with: pip install pyinstaller
    pause
    exit /b 1
)
echo.

echo Checking icon file...
if not exist "icon-i.ico" (
    echo ERROR: icon-i.ico was not found in this folder.
    pause
    exit /b 1
)
echo.

echo Cleaning previous build output...
if exist "build" rmdir /s /q "build"
if exist "dist\furi-lrc-gui" rmdir /s /q "dist\furi-lrc-gui"
echo.

echo Building the application...
%PYTHON% -m PyInstaller --noconfirm --clean "furi-lrc-gui.spec"

if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller failed.
    pause
    exit /b 1
)

echo.
echo Creating default folders in output...
mkdir "dist\furi-lrc-gui\songs"  2>nul
mkdir "dist\furi-lrc-gui\flrc"   2>nul
mkdir "dist\furi-lrc-gui\flpls"  2>nul

echo Verifying bundled font resources...
if not exist "dist\furi-lrc-gui\_internal\fonts\NotoSansJP-Regular.ttf" (
    echo ERROR: UI fonts were not bundled.
    echo        Check furi-lrc-gui.spec and rebuild.
    pause
    exit /b 1
)

if not exist "dist\furi-lrc-gui\_internal\icon-i.ico" (
    echo ERROR: icon-i.ico was not bundled.
    echo        Check furi-lrc-gui.spec and rebuild.
    pause
    exit /b 1
)

echo Copying sample files...
%PYTHON% -c "import shutil,pathlib; p=pathlib.Path('private/songs/扉をあけて.mp3'); shutil.copy2(p, pathlib.Path('dist/furi-lrc-gui/songs')/p.name) if p.exists() else None"
if exist "private\flrc\tobira-wo-akete.flrc"  copy /y "private\flrc\tobira-wo-akete.flrc"  "dist\furi-lrc-gui\flrc\" >nul

echo.
echo ============================================================
echo  Build finished.
echo ============================================================
echo.
echo Output folder:
echo   %cd%\dist\furi-lrc-gui
echo.
pause
exit /b 0
