@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "SCRIPT=%ROOT%\hls_copy_prep_final.py"
set "README=%ROOT%\README-使用说明.md"
set "BIN_DIR=%ROOT%\bin"
set "FFMPEG=%BIN_DIR%\ffmpeg.exe"
set "FFPROBE=%BIN_DIR%\ffprobe.exe"
set "PY_EXE="
set "PY_ARGS="
set "PY_INSTALLER="
set "RC=0"

title ErsatzTV HLS Copy 预处理工具

if not exist "%ROOT%\source" md "%ROOT%\source" >nul 2>nul
if not exist "%ROOT%\target" md "%ROOT%\target" >nul 2>nul
if not exist "%ROOT%\done" md "%ROOT%\done" >nul 2>nul
if not exist "%ROOT%\logs" md "%ROOT%\logs" >nul 2>nul
if not exist "%ROOT%\bin" md "%ROOT%\bin" >nul 2>nul

call :refresh_env

:menu
cls
echo ========================================================================
echo ErsatzTV HLS Copy 预处理工具（最终定版参数）
echo ========================================================================
echo.
echo 目标：离线预处理一次，ErsatzTV 播放时继续使用 copy 模式
echo 固定参数：preset=medium ^| crf=18 ^| audio=192k ^| 1秒GOP ^| 1秒强制关键帧
echo.
echo 程序目录：%ROOT%
echo.
echo [环境状态]
if defined PY_EXE (
  echo   Python 3   : [OK] %PY_EXE% %PY_ARGS%
) else (
  echo   Python 3   : [缺失] 未检测到 Python 3
)
if defined PY_INSTALLER (
  echo   本地安装包 : [OK] %PY_INSTALLER%
) else (
  echo   本地安装包 : [未发现] 根目录未找到 python-*.exe
)
if exist "%SCRIPT%" (
  echo   主程序     : [OK] %SCRIPT%
) else (
  echo   主程序     : [缺失] %SCRIPT%
)
if exist "%FFMPEG%" (
  echo   ffmpeg      : [OK] %FFMPEG%
) else (
  echo   ffmpeg      : [缺失] %FFMPEG%
)
if exist "%FFPROBE%" (
  echo   ffprobe     : [OK] %FFPROBE%
) else (
  echo   ffprobe     : [缺失] %FFPROBE%
)
echo.
echo [目录说明]
echo   source  = 放待处理视频
echo   target  = 放处理后的视频
echo   done    = 成功后移动原始文件
echo   logs    = 过程日志 / 汇总日志 / 预检查报告
echo.
echo [功能菜单]
echo   [1] 开始预检查（只扫描，不转码）
echo   [2] 开始正式预处理
echo   [3] 仅做环境检查
echo   [4] 打开 source 目录
echo   [5] 打开 logs 目录
echo   [6] 打开使用说明
echo   [7] 重新检测环境
echo   [8] 安装 Python 3（使用本目录安装包）
echo   [0] 退出
echo.
choice /c 123456780 /n /m "请输入选项 [1/2/3/4/5/6/7/8/0]: "
if errorlevel 9 goto :end
if errorlevel 8 goto :install_python_menu
if errorlevel 7 goto :refresh_only
if errorlevel 6 goto :open_readme
if errorlevel 5 goto :open_logs
if errorlevel 4 goto :open_source
if errorlevel 3 goto :env_check
if errorlevel 2 goto :run_guided
if errorlevel 1 goto :run_scan
goto :menu

:run_scan
call :require_runtime || goto :menu
cls
echo ========================================================================
echo 预检查模式（只扫描，不转码）
echo ========================================================================
echo.
echo 这个模式会：
echo   - 扫描 source 或你指定目录中的视频
echo   - 统计文件数量、总大小、总时长
echo   - 给出经验性处理时间预估
echo   - 生成风险报告，帮你优先看最可能有问题的文件
echo.
echo 预检查报告会写入 logs 目录。
echo.
pause
"%PY_EXE%" %PY_ARGS% "%SCRIPT%" --scan-only --root "%ROOT%"
set "RC=%ERRORLEVEL%"
call :after_run "预检查"
goto :menu

:run_guided
call :require_runtime || goto :menu
cls
echo ========================================================================
echo 正式预处理模式
echo ========================================================================
echo.
echo 这个模式会：
echo   - 先扫描并估算耗时
echo   - 再按固定参数逐个转码
echo   - 输出写入 target
echo   - 成功后把原文件移动到 done
echo   - 如检测到目标文件已完整存在，则只移动 source 到 done，避免重复转码
echo   - 全过程日志写入 logs
echo.
echo 温馨提示：建议先用“预检查模式”看一下文件概况，再开始正式处理。
echo.
pause
"%PY_EXE%" %PY_ARGS% "%SCRIPT%" --guided --root "%ROOT%"
set "RC=%ERRORLEVEL%"
call :after_run "正式预处理"
goto :menu

:env_check
call :require_runtime || goto :menu
cls
echo ========================================================================
echo 环境检查
echo ========================================================================
echo.
"%PY_EXE%" %PY_ARGS% "%SCRIPT%" --check-only --root "%ROOT%"
set "RC=%ERRORLEVEL%"
call :after_run "环境检查"
goto :menu

:open_source
start "" explorer "%ROOT%\source"
goto :menu

:open_logs
start "" explorer "%ROOT%\logs"
goto :menu

:open_readme
if exist "%README%" (
  start "" "%README%"
) else (
  echo.
  echo [错误] 未找到说明文档：%README%
  pause
)
goto :menu

:refresh_only
call :refresh_env
echo.
echo [提示] 环境信息已刷新。
timeout /t 1 >nul
goto :menu

:install_python_menu
cls
echo ========================================================================
echo Python 3 安装
echo ========================================================================
echo.
call :install_python_flow
echo.
pause
goto :menu

:refresh_env
set "PY_EXE="
set "PY_ARGS="
set "PY_INSTALLER="
for %%F in ("%ROOT%\python-*.exe") do (
  if exist "%%~fF" if not defined PY_INSTALLER set "PY_INSTALLER=%%~fF"
)

py -3 --version >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  set "PY_EXE=py"
  set "PY_ARGS=-3"
  goto :eof
)

python --version >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  set "PY_EXE=python"
  set "PY_ARGS="
  goto :eof
)

if exist "%LocalAppData%\Programs\Python\Python314\python.exe" (
  set "PY_EXE=%LocalAppData%\Programs\Python\Python314\python.exe"
  set "PY_ARGS="
  goto :eof
)
if exist "%ProgramFiles%\Python314\python.exe" (
  set "PY_EXE=%ProgramFiles%\Python314\python.exe"
  set "PY_ARGS="
  goto :eof
)
for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do (
  if exist "%%~fD\python.exe" if not defined PY_EXE (
    set "PY_EXE=%%~fD\python.exe"
    set "PY_ARGS="
  )
)
goto :eof

:install_python_flow
call :refresh_env
if defined PY_EXE (
  echo [提示] 当前已经检测到 Python 3：%PY_EXE% %PY_ARGS%
  exit /b 0
)
if not defined PY_INSTALLER (
  echo [错误] 根目录未发现 Python 安装包（python-*.exe）。
  echo.
  echo 你可以把安装包放到这里：
  echo   %ROOT%
  exit /b 1
)

echo 检测到可用安装包：
echo   %PY_INSTALLER%
echo.
echo 安装建议：
echo   - 使用默认安装即可
echo   - 建议勾选 Add python.exe to PATH
echo.
choice /c YN /n /m "是否现在启动安装器 [Y/N]: "
if errorlevel 2 exit /b 1

echo.
echo 正在启动安装器，请按界面完成安装...
start /wait "" "%PY_INSTALLER%"
echo.
echo 安装器已退出，正在重新检测 Python 环境...
call :refresh_env
if defined PY_EXE (
  echo [OK] 已检测到 Python 3：%PY_EXE% %PY_ARGS%
  echo 现在可以直接使用本工具了。
  exit /b 0
)
echo [提示] 安装器已运行，但当前窗口还未检测到 Python 3。
echo 这通常是因为 PATH 尚未刷新。
echo 你可以：
echo   1. 关闭本工具后重新打开
echo   2. 或者直接选择“7. 重新检测环境”再试一次
echo   3. 或确认 Python 已按默认方式成功安装
exit /b 1

:require_runtime
if not exist "%SCRIPT%" (
  echo.
  echo [错误] 缺少主程序：%SCRIPT%
  echo.
  pause
  exit /b 1
)
if not exist "%FFMPEG%" (
  echo.
  echo [错误] 缺少 ffmpeg.exe：%FFMPEG%
  echo.
  pause
  exit /b 1
)
if not exist "%FFPROBE%" (
  echo.
  echo [错误] 缺少 ffprobe.exe：%FFPROBE%
  echo.
  pause
  exit /b 1
)
if not defined PY_EXE (
  echo.
  echo [错误] 未检测到可用的 Python 3 运行环境。
  echo.
  if defined PY_INSTALLER (
    echo 已检测到本地安装包：
    echo   %PY_INSTALLER%
    echo.
    choice /c YN /n /m "是否现在启动安装器 [Y/N]: "
    if errorlevel 2 (
      echo.
      pause
      exit /b 1
    )
    call :install_python_flow
    call :refresh_env
    if defined PY_EXE exit /b 0
    echo.
    pause
    exit /b 1
  ) else (
    echo 你需要安装：
    echo   - Python 3（建议 3.10 或更高版本）
    echo.
    echo 安装时建议勾选：
    echo   - Add python.exe to PATH
    echo.
    pause
    exit /b 1
  )
)
exit /b 0

:after_run
echo.
if "%RC%"=="0" (
  echo [完成] %~1 已结束。
) else (
  echo [结束] %~1 退出码：%RC%
  echo         如有失败，请查看 logs 目录中的 .log / summary.json / scan-report。
)
echo.
choice /c YN /n /m "是否现在打开 logs 目录 [Y/N]: "
if errorlevel 2 exit /b 0
start "" explorer "%ROOT%\logs"
exit /b 0

:end
echo.
echo 已退出。
timeout /t 1 >nul
exit /b 0
