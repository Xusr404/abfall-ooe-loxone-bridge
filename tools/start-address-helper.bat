@echo off
setlocal

for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
set "VENV_PYTHON=%PROJECT_DIR%\.venv\Scripts\python.exe"
set "ADDRESS_DATA=%PROJECT_DIR%\tools\helpers\data\address-data.json"

if not exist "%VENV_PYTHON%" (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv "%PROJECT_DIR%\.venv"
    ) else (
        where python >nul 2>nul
        if errorlevel 1 (
            echo Python 3 was not found. Install Python 3, then try again.
            pause
            exit /b 1
        )
        python -m venv "%PROJECT_DIR%\.venv"
    )
    if errorlevel 1 (
        echo Could not create the local Python environment.
        pause
        exit /b 1
    )
)

"%VENV_PYTHON%" -m pip install --disable-pip-version-check -r "%PROJECT_DIR%\bridge\requirements.txt"
if errorlevel 1 (
    echo Could not install the required Python packages.
    pause
    exit /b 1
)

if not exist "%ADDRESS_DATA%" (
    echo Loading the address list for first use. This can take a few minutes...
    "%VENV_PYTHON%" "%PROJECT_DIR%\tools\helpers\refresh_address_data.py"
    if errorlevel 1 echo The address list could not be loaded now. You can retry from the setup page.
)

start "Abfall OOE" /min "%VENV_PYTHON%" "%PROJECT_DIR%\bridge\loxone_bridge.py" --host 127.0.0.1 --port 8765
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8765
endlocal
