@echo off
setlocal EnableExtensions DisableDelayedExpansion

cd /d "%~dp0"

set "COUNT=0"
set "FFMPEG=%~dp0ffmpeg.exe"
set "VIDEO=%~dp01.mp4"
set "RTMP=请替换为新的虎牙推流地址"

if not exist "%FFMPEG%" (
    echo 找不到 FFmpeg：%FFMPEG%
    pause
    exit /b 1
)

if not exist "%VIDEO%" (
    echo 找不到视频文件：%VIDEO%
    pause
    exit /b 1
)

:RESTART
title Huya 挂播 - 重启次数 [%COUNT%]

echo.
echo 当前时间：%time%
echo 当前日期：%date%
echo ============================================================
echo 虎牙挂播正在启动，重启次数：%COUNT%
echo ============================================================

"%FFMPEG%" ^
    -hide_banner -nostdin -loglevel warning ^
    -re -stream_loop -1 -i "%VIDEO%" ^
    -map 0:v:0 -map 0:a:0? ^
    -c:v libx264 -preset veryfast -tune zerolatency ^
    -pix_fmt yuv420p ^
    -b:v 1500k -maxrate 1500k -bufsize 3000k ^
    -g 48 -keyint_min 48 -sc_threshold 0 ^
    -c:a aac -b:a 128k -ar 44100 ^
    -flvflags no_duration_filesize ^
    -f flv "%RTMP%"

set "EXITCODE=%ERRORLEVEL%"
set /a COUNT+=1

echo.
echo 推流已停止，FFmpeg 错误码：%EXITCODE%
echo 10 秒后自动重试，按 Ctrl+C 可退出。
timeout /t 10 /nobreak >nul
goto RESTART