# -*- coding: utf-8 -*-
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
import db

print("=== 服务状态 ===")
import urllib.request
for u in ["http://127.0.0.1:8080/login", "http://127.0.0.1:8001/"]:
    try:
        with urllib.request.urlopen(u, timeout=6) as r:
            print(u, "->", r.status)
    except Exception as e:
        print(u, "-> ERR", type(e).__name__)

print("=== 缩略图目录 ===")
THUMB = db.DATA_DIR / "thumbs"
print("thumb dir exists:", THUMB.exists(), "| 文件数:", len(list(THUMB.glob("*"))) if THUMB.exists() else 0)

print("=== 逐张测试缩略图生成 ===")
import main
imgs = db.list_images()
print("总图片:", len(imgs))
ok, fail = 0, []
for i in imgs[:60]:
    src = db.UPLOAD_DIR / i["filename"]
    if not src.exists():
        fail.append((i["id"], i["filename"], "文件不存在"))
        continue
    try:
        t = main._ensure_thumb(i)
        if t and t.exists():
            ok += 1
        else:
            fail.append((i["id"], i["filename"], "生成返回空"))
    except Exception as e:
        fail.append((i["id"], i["filename"], type(e).__name__ + ": " + str(e)[:60]))

print("成功:", ok, "| 失败:", len(fail))
for f in fail[:15]:
    print("  FAIL", f)
