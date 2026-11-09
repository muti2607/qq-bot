# -*- coding: utf-8 -*-
import sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8")
import db, main

print("=== 用户 ===")
for u in db.list_upload_users():
    print(" ", u["username"], "| uid:", u.get("uid_number"), "| is_admin:", u.get("is_admin"),
          "| admin_until:", (u.get("admin_until") or "-"), "| enabled:", u.get("enabled"))

imgs = db.list_images()
print("图片数:", len(imgs), "| 已通过:", sum(1 for i in imgs if i["status"]=="approved"))

# 分别用每个账号的 token 测
for u in db.list_upload_users()[:3]:
    name = u["username"]
    tok = main._make_upload_token(name)
    codes = {}
    for i in imgs[:12]:
        url = f"http://127.0.0.1:8001/remote/thumb/{i['id']}?token={tok}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                codes[r.status] = codes.get(r.status, 0) + 1
        except urllib.error.HTTPError as e:
            codes[e.code] = codes.get(e.code, 0) + 1
        except Exception as e:
            codes[type(e).__name__] = codes.get(type(e).__name__, 0) + 1
    print(f"{name} (admin={u.get('is_admin')}, until={u.get('admin_until') or '-'}): {codes}")

print("=== 缩略图接口的 admin 判定 ===")
import inspect
src = inspect.getsource(main.remote_thumb)
for line in src.splitlines():
    if "admin" in line.lower() or "is_admin" in line:
        print("  ", line.strip())
