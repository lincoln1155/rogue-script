@echo off
echo ============================================
echo   Building ESP Overlay
echo ============================================
echo.
echo Make sure you have installed dependencies first:
echo   pip install -r requirements.txt
echo   pip install pyinstaller
echo.

pyinstaller --onefile --noconsole --name esp_overlay main.py

if not exist "dist\esp_overlay.exe" (
    echo.
    echo ERROR: Build failed! esp_overlay.exe was not created.
    pause
    exit /b 1
)

echo.
echo Build successful! Copying to install location...

:: Create the target folder if it doesn't exist
if not exist "%LOCALAPPDATA%\RogueLiteESP" (
    mkdir "%LOCALAPPDATA%\RogueLiteESP"
    echo Created folder: %LOCALAPPDATA%\RogueLiteESP
)

:: Copy the exe to the target location
copy /Y "dist\esp_overlay.exe" "%LOCALAPPDATA%\RogueLiteESP\esp_overlay.exe"

echo.
echo ============================================
echo   Done! esp_overlay.exe installed to:
echo   %LOCALAPPDATA%\RogueLiteESP\esp_overlay.exe
echo ============================================
echo.
echo The Lua script will auto-launch it, or you can
echo run it manually from that location.
echo.
pause
