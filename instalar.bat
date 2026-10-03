@echo off
cd /d "%~dp0"

echo ==========================================
echo   Instalando dependencias do Telegram
echo ==========================================
echo.

python --version
if errorlevel 1 (
    echo.
    echo Python nao foi encontrado.
    echo Verifique se o Python esta adicionado ao PATH.
    echo.
    pause
    exit /b 1
)

echo.
echo Atualizando o pip...
python -m pip install --upgrade pip

echo.
echo Instalando dependencias...
python -m pip install -r requirements.txt

if errorlevel 1 (
    echo.
    echo ==========================================
    echo   Falha na instalacao.
    echo ==========================================
    echo.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo   Instalacao concluida com sucesso.
echo ==========================================
echo.
pause