# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import db

us = db.list_upload_users()
for u in us:
    print(u["username"], "→ uid_number =", u.get("uid_number"))

# 测试数字ID登录
if us:
    target = us[0]
    num = target.get("uid_number")
    got = db.get_upload_user(num)
    print("login by uid_number", num, "→", got["username"] if got else "FAIL")
