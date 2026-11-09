# -*- coding: utf-8 -*-
import sys, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8")
import db, main

imgs = db.list_images()
print("图片数:", len(imgs))

for u in db.list_upload_users():
    name = u["username"]
    tok = main._make_upload_token(name)
    codes = {}
    for i in imgs:
        url = f"http://127.0.0.1:8001/remote/thumb/{i['id']}?token={tok}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                codes[r.status] = codes.get(r.status, 0) + 1
        except urllib.error.HTTPError as e:
            codes[e.code] = codes.get(e.code, 0) + 1
        except Exception as e:
            codes[type(e).__name__] = codes.get(type(e).__name__, 0) + 1
    is_adm = main._is_admin_user(name)
    print(f"{name}: is_admin={is_adm} | {codes}")
