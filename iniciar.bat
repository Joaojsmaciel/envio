@echo off
cd /d "%~dp0"

echo ==========================================
echo   Iniciando Telegram Fotos
echo ==========================================
echo.

python app.py

if errorlevel 1 (
    echo.
    echo ==========================================
    echo   O programa terminou com erro.
    echo ==========================================
    echo.
    pause
)