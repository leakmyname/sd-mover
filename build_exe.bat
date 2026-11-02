@echo off
REM SD卡自动转存工具 - 打包单文件exe
REM 在项目根目录运行: build_exe.bat

echo ========================================
echo   SD卡自动转存工具 - 打打包单文件exe
echo ========================================
echo.

echo [1/3] 清理旧的构建产物...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist sd_mover.spec del sd_mover.spec
echo 清理完成.
echo.

echo [2/3] 使用 PyInstaller 打包...
pyinstaller --onefile --name sd_mover ^
    --windowed ^
    --noconfirm ^
    --clean ^
    --add-data "sd_mover;sd_mover" ^
    run.py

if errorlevel 1 (
    echo.
    echo !! 打包失败 !!
    pause
    exit /b 1
)
echo.
echo [3/3] 打包完成!
echo 输出文件: dist\sd_mover.exe
echo.
pause
