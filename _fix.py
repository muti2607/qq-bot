# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import db

# 修复 onebot_http（被某次测试污染成 127.0.0.1:3993）
db.set_settings({"onebot_http": "http://napcat:3001"})

# 补 smtp_user（smtp_pass 已有授权码）
d = db.get_settings()
if not d.get("smtp_user"):
    db.set_settings({"smtp_user": "1641291737@qq.com"})

d = db.get_settings()
print("onebot_http =", d.get("onebot_http"))
print("smtp_user =", d.get("smtp_user"))
print("smtp_pass set =", bool(d.get("smtp_pass")))
print("upload_users =", len(db.list_upload_users()))
print("pending imgs =", len(db.list_images(status="pending")))
print("FIX DONE")
