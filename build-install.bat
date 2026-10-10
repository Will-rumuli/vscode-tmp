@echo off
setlocal enabledelayedexpansion
rem ============================================================
rem QPI SDK VS Code 扩展 一键构建安装脚本 (Windows 版)
rem
rem 用法:
rem   build-install.bat            @ 交互式输入版本号 (如 v0.1.1)
rem   build-install.bat v0.1.1     @ 直接带版本号
rem
rem 流程: 改 package.json 版本 -> tsc 编译 -> vsce 打包
rem        -> 卸载旧版扩展 -> 安装新 vsix
rem ============================================================

cd /d "%~dp0"

set "EXT_ID=Quectel.quectelpi"
set "EXT_DIR=%USERPROFILE%\.vscode\extensions"

rem ---------- 1. 获取版本号 ----------
set "VERSION=%~1"
if "%VERSION%"=="" set /p "VERSION=请输入版本号 (如 v0.1.1): "
set "VER=%VERSION%"
if "%VER:~0,1%"=="v" set "VER=%VER:~1%"

rem 版本号格式校验 (x.y.z), 带不带 v 都行
echo %VER%| findstr /r "^[0-9][0-9]*[.][0-9][0-9]*[.][0-9][0-9]*$" >nul
if errorlevel 1 (echo 错误: 版本号格式不对 -^> %VERSION% （示例: v0.1.1 或 0.1.1） & exit /b 1)
echo ==^> 目标版本: %VER%

rem ---------- 2. 检查工具 ----------
where code >nul 2>nul || (echo 错误: 未找到 code CLI, 请确认 VS Code 已安装且 code 在 PATH 中 & exit /b 1)
where npm  >nul 2>nul || (echo 错误: 未找到 npm & exit /b 1)
if not exist node_modules (echo 错误: 缺少 node_modules, 请先执行 npm install & exit /b 1)

rem ---------- 3. 更新 package.json 版本 ----------
echo ==^> 更新 package.json 版本为 %VER%
node -e "const fs=require('fs');const j=JSON.parse(fs.readFileSync('package.json','utf8'));j.version=process.argv[1];fs.writeFileSync('package.json',JSON.stringify(j,null,2)+'\n');console.log('    package.json version -> '+j.version);" %VER%
if errorlevel 1 exit /b 1

rem ---------- 4. 编译 ----------
echo ==^> 编译 (npm run compile)
call npm run compile
if errorlevel 1 (echo 编译失败 & exit /b 1)

rem ---------- 5. 打包 ----------
echo ==^> 打包 VSIX
call npm run package
if errorlevel 1 (echo 打包失败 & exit /b 1)
set "VSIX=quectelpi-%VER%.vsix"
if not exist "%VSIX%" (echo 错误: 打包后未找到 %VSIX% & exit /b 1)

rem ---------- 6. 卸载旧版本 ----------
echo ==^> 卸载旧版本扩展
call code --uninstall-extension "%EXT_ID%" >nul 2>nul
if errorlevel 1 (echo     （扩展未安装或已卸载, 继续）) else (echo     已卸载 %EXT_ID% （注册信息）)

rem 删除磁盘上的旧版本目录 (跳过当前目标版本)
echo ==^> 删除旧版本目录
for /d %%d in ("%EXT_DIR%\%EXT_ID%-*") do (
    set "DNAME=%%~nxd"
    if not "!DNAME!"=="%EXT_ID%-%VER%" (
        rmdir /s /q "%%d" >nul 2>nul
        if errorlevel 1 (echo     删除失败: !DNAME! （文件被占用, VS Code 开着时旧后端进程会锁住 python/ 目录）) else (echo     已删除: !DNAME!)
    )
)

set "REMAIN="
for /d %%d in ("%EXT_DIR%\%EXT_ID%-*") do (
    set "DNAME=%%~nxd"
    if not "!DNAME!"=="%EXT_ID%-%VER%" set "REMAIN=!REMAIN! !DNAME!"
)
if not "%REMAIN%"=="" (
    echo     警告: 以下旧版本目录残留: %REMAIN%
    echo     请重启 VS Code 后重跑本脚本, 或手动删除:
    echo       rmdir /s /q "%EXT_DIR%\%EXT_ID%-*"
)

rem ---------- 7. 安装新版本 ----------
echo ==^> 安装 %VSIX%
call code --install-extension "%VSIX%" --force
if errorlevel 1 (echo 安装失败 & exit /b 1)

echo.
echo 完成 - 已安装 %EXT_ID% @ %VER%
echo 请重启 VS Code (或执行命令: QPI SDK: 重启后端服务)
endlocal
