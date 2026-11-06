@echo off
setlocal

where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python 3 was not found. Install Python 3, then try again.
        set "RESULT=1"
        goto :finish
    )
    set "PYTHON=python"
)

echo Checking and installing required Python packages if needed...
%PYTHON% -m pip install --disable-pip-version-check -r "%~dp0..\bridge\requirements.txt"
if errorlevel 1 (
    echo Could not install the required Python packages.
    set "RESULT=1"
    goto :finish
)

%PYTHON% "%~dp0helpers\refresh_address_data.py"
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="0" echo Address data refreshed. Reload the setup page to use the new list.
goto :finish

:finish
echo.
pause
exit /b %RESULT%
