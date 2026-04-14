@echo off
chcp 65001 >nul

:: 切换到 start.bat 所在的目录，确保 Flask 能找到正确的 templates
cd /d "%~dp0"

echo.
echo ========================================
echo   WeChat Insight - 微信群聊洞察工具
echo ========================================
echo   项目目录: %cd%
echo.

pip install flask openai -q 2>nul

echo   正在启动，浏览器将自动打开...
echo   地址: http://localhost:5678
echo   按 Ctrl+C 退出
echo.

python app.py
pause
