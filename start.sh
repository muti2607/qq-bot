#!/bin/sh
# 同时启动：主后台(8080) + 独立上传服务(8001)
python -m uvicorn main:remote_app --host 0.0.0.0 --port 8001 --log-level warning &
exec python -m uvicorn main:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips "*"
