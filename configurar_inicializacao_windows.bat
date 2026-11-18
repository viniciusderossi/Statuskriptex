@echo off
setlocal EnableDelayedExpansion

:: Verifica se esta executando com privilegios de Administrador
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Solicitando permissoes de Administrador do Windows...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process cmd.exe -ArgumentList '/c \"\"%~f0\"\"' -Verb RunAs"
    exit /b
)

echo ======================================================
echo   Configurando Motor Kryptex no Agendador de Tarefas
echo ======================================================
echo.

set "EXE_PATH=%~dp0Motor_Kryptex_GUI_PT_Panorama_Caixas.exe"

if not exist "%EXE_PATH%" (
    echo [ERRO] Motor_Kryptex_GUI_PT_Panorama_Caixas.exe nao encontrado nesta pasta!
    echo %EXE_PATH%
    echo.
    pause
    exit /b 1
)

echo Registrando tarefa de inicializacao com privilegios de Administrador...
schtasks /create /tn "MotorKryptexAutoStart" /tr "\"%EXE_PATH%\"" /sc onlogon /rl highest /f

if %errorlevel% equ 0 (
    echo.
    echo ======================================================
    echo  [SUCESSO] Tarefa registrada com sucesso no Windows!
    echo ======================================================
    echo O Motor Kryptex iniciara automaticamente sempre que o
    echo Windows fizer logon, com poderes de Administrador.
) else (
    echo.
    echo ======================================================
    echo  [ERRO] Falha ao registrar tarefa no Agendador do Windows.
    echo ======================================================
)

echo.
pause