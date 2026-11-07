@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
echo ===============================================
echo  NASCAR 06 - ASSET CONVERSION ONLY
echo  Reading existing CARDATA folder
echo ===============================================
echo.
if not exist "CARDATA\" (
  echo ERROR: CARDATA folder not found. Run Extract Assets.bat first.
  goto :failed
)
if not exist "scripts\tmd-tmf_to_obj.py" goto :missing
if not exist "scripts\mmap_to_png.py" goto :missing
set "PYTHON=python"
python --version >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python not found.
  goto :failed
)
%PYTHON% -c "import numpy; import PIL" >nul 2>nul
if errorlevel 1 (
  echo ERROR: Texture converter requires numpy and Pillow.
  echo Install with: %PYTHON% -m pip install numpy Pillow
  goto :failed
)
set "FAILED=0"
set "MODELS=0"
for %%F in ("CARDATA\*.tmf" "CARDATA\*.tmd") do (
  if exist "%%~fF" (
    set /a MODELS+=1 >nul
    rem Try combined bodies where supported, otherwise export normal geometry.
    %PYTHON% "scripts\tmd-tmf_to_obj.py" "%%~fF" --all-body-variants --flip-v --overwrite --output-dir "converted_models" >nul 2>nul
    if errorlevel 1 (
      %PYTHON% "scripts\tmd-tmf_to_obj.py" "%%~fF" --flip-v --overwrite --output-dir "converted_models"
      if errorlevel 1 set "FAILED=1"
    )
  )
)
if "!MODELS!"=="0" echo WARNING: No TMF or TMD models found in CARDATA.
set "TEXTURES=0"
for %%F in ("CARDATA\*.mmap" "CARDATA\*.3ds") do (
  if exist "%%~fF" set /a TEXTURES+=1 >nul
)
if not "!TEXTURES!"=="0" (
  if exist "CARDATA\*.mmap" (
    %PYTHON% "scripts\mmap_to_png.py" "CARDATA\*.mmap" --skip-unsupported --overwrite -o "decoded_textures"
    if errorlevel 1 set "FAILED=1"
  )
  if exist "CARDATA\*.3ds" (
    %PYTHON% "scripts\mmap_to_png.py" "CARDATA\*.3ds" --skip-unsupported --overwrite -o "decoded_textures"
    if errorlevel 1 set "FAILED=1"
  )
) else (
  echo WARNING: No MMAP textures found in CARDATA.
)
if "!FAILED!"=="1" goto :failed
echo.
echo Conversion complete. See converted_models and decoded_textures.
pause
exit /b 0
:missing
echo ERROR: Missing a Python converter script.
:failed
echo Conversion incomplete. Review the messages above.
pause
exit /b 1
