@echo off
cd /d %~dp0
if not exist .venv (
  echo [1/2] 创建虚拟环境...
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install --no-cache-dir -r requirements.txt
)
echo [2/2] 启动服务： http://127.0.0.1:8080
.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8080
pause
