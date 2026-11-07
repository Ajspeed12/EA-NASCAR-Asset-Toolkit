@echo off
setlocal
cd /d "%~dp0"
for %%F in ("tools\quickbms.exe" "scripts\nascar_terf.bms" "source\CARDATA.DAT" "source\NASCAR.DB") do (
    if not exist "%%~F" (
        echo ERROR: Missing %%~F
        goto :failed
    )
)
if not exist "CARDATA\" (
    mkdir "CARDATA"
    if errorlevel 1 (
        echo ERROR: Could not create CARDATA folder.
        goto :failed
    )
)
echo Extracting source\CARDATA.DAT into CARDATA\...
"tools\quickbms.exe" "scripts\nascar_terf.bms" "source\CARDATA.DAT" "CARDATA"
if errorlevel 1 goto :failed
echo Extraction complete.
pause
exit /b 0
:failed
echo Extraction failed. Review the messages above.
pause
exit /b 1
