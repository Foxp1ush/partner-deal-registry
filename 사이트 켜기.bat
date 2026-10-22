@echo off
chcp 65001 > nul
cd /d "%~dp0"

rem 이미 켜져 있으면 브라우저만 연다
netstat -ano | findstr "LISTENING" | findstr ":8501 " > nul
if %errorlevel%==0 (
  start "" http://localhost:8501
  exit /b
)

echo 사이트를 켜는 중입니다... 잠시 후 브라우저가 자동으로 열립니다.
echo 이 검은 창을 닫으면 사이트도 꺼집니다.
echo.

rem 3초 뒤 브라우저 열기 (서버가 뜰 시간)
start "" cmd /c "timeout /t 3 > nul & start http://localhost:8501"

.venv\Scripts\python.exe -m streamlit run app.py --server.headless true --server.address localhost
pause
