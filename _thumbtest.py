# -*- coding: utf-8 -*-
import sys, json, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8")
import db, main

# 造一个管理员会话 token（模拟前端登录后的 cookie）
users = db.list_upload_users()
adm = next((u for u in users if u.get("is_admin")), None)
print("admin user:", adm["username"] if adm else "NONE")
tok = main._make_upload_token(adm["username"]) if hasattr(main, "_make_upload_token") else None
if not tok:
    # 尝试其它命名
    for cand in ("_upload_token", "_new_upload_token", "_issue_upload_token"):
        if hasattr(main, cand):
            tok = getattr(main, cand)(adm["username"]); break
print("token:", (tok[:20] + "...") if tok else "无法生成")

imgs = db.list_images()
print("图片数:", len(imgs))

codes = {}
if tok:
    for i in imgs:
        url = f"http://127.0.0.1:8001/remote/thumb/{i['id']}?token={tok}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                codes[r.status] = codes.get(r.status, 0) + 1
        except urllib.error.HTTPError as e:
            codes[e.code] = codes.get(e.code, 0) + 1
        except Exception as e:
            codes[type(e).__name__] = codes.get(type(e).__name__, 0) + 1
    print("状态码分布:", codes)
else:
    print("跳过（无 token 生成函数）")
