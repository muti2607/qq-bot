# -*- coding: utf-8 -*-
"""服务器端 E2E：管理员登录 → 拉全量图片（走 HTTP 层验证前端同款调用）。"""
import sys, json, urllib.request, http.cookiejar
sys.stdout.reconfigure(encoding="utf-8")
import db

db.init_db()
users = {u["username"]: u for u in db.list_upload_users()}
muti = users.get("muti")
print("muti:", "found, is_admin =", muti.get("is_admin") if muti else "NOT FOUND")

import main as m
test_pw = "Tmppw2026!"
db.update_upload_user(muti["id"], password=test_pw)

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
base = "http://127.0.0.1:8001"

req = urllib.request.Request(base + "/remote/login",
    data=json.dumps({"username": "muti", "password": test_pw}).encode(),
    headers={"Content-Type": "application/json"})
r = opener.open(req, timeout=10)
d = json.loads(r.read())
print("login:", r.status, "| is_admin:", d.get("is_admin"))
tok = d.get("token", "")

req = urllib.request.Request(base + "/remote/admin-images",
    data=json.dumps({"admin_password": ""}).encode(),
    headers={"Content-Type": "application/json", "X-Upload-Session": tok})
try:
    r = opener.open(req, timeout=10)
    d = json.loads(r.read())
    imgs = d.get("images", [])
    print("admin-images:", r.status, "| count:", len(imgs))
    for i in imgs[:6]:
        print("  ", i["id"], (i["original"] or "")[:24], i["status"], "@" + str(i["uploader"]))
    if len(imgs) > 6:
        print("   ... total", len(imgs))
except urllib.error.HTTPError as e:
    print("admin-images ERR:", e.code, e.read().decode()[:150])

print()
print("NOTE: muti password temporarily reset to:", test_pw)
