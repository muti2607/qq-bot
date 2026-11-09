# -*- coding: utf-8 -*-
"""curl(schannel) 走 GitHub REST API 推送 HEAD 的改动文件。"""
import base64, json, subprocess, sys, io, tempfile, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

OWNER, REPO = "muti2607", "qq-bot"

def token():
    out = subprocess.run(["git","credential","fill"],
        input="protocol=https\nhost=github.com\nusername=muti2607\n\n",
        capture_output=True, text=True).stdout
    for l in out.splitlines():
        if l.startswith("password="): return l[9:].strip()
    raise SystemExit("no token")

TOK = token()

def curl(method, path, body=None):
    args = ["curl.exe","-s","-X",method,
            "-H","Authorization: Bearer "+TOK,
            "-H","Accept: application/vnd.github+json",
            "-H","Content-Type: application/json",
            "-H","User-Agent: qqbot-deploy","--max-time","40"]
    tmp = None
    if body is not None:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(body, tmp, ensure_ascii=False); tmp.close()
        args += ["--data-binary","@"+tmp.name]
    args.append("https://api.github.com"+path)
    r = subprocess.run(args, capture_output=True)
    if tmp:
        try: os.unlink(tmp.name)
        except Exception: pass
    t = r.stdout.decode("utf-8","replace")
    try: return json.loads(t)
    except Exception: return {"_raw": t[:200]}

def main():
    changed = subprocess.run(["git","diff-tree","--no-commit-id","--name-only","-r","HEAD"],
                             capture_output=True, text=True).stdout.split()
    if not changed:
        print("no changed files"); return 0
    print("changed:", changed)
    ref = curl("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/main")
    base_sha = ref["object"]["sha"]
    commit = curl("GET", f"/repos/{OWNER}/{REPO}/git/commits/{base_sha}")
    base_tree = commit["tree"]["sha"]
    print("remote main:", base_sha[:8])
    tree = []
    for p in changed:
        if not os.path.isfile(p): continue
        b64 = base64.b64encode(open(p,"rb").read()).decode()
        r = curl("POST", f"/repos/{OWNER}/{REPO}/git/blobs", {"content": b64, "encoding":"base64"})
        if "sha" not in r: print("blob fail", p, r); return 1
        tree.append({"path":p,"mode":"100644","type":"blob","sha":r["sha"]})
        print("  blob", p, r["sha"][:8])
    r = curl("POST", f"/repos/{OWNER}/{REPO}/git/trees", {"base_tree":base_tree,"tree":tree})
    new_tree = r["sha"]
    msg = subprocess.run(["git","log","-1","--format=%s"],capture_output=True,text=True).stdout.strip()
    r = curl("POST", f"/repos/{OWNER}/{REPO}/git/commits", {"message":msg,"tree":new_tree,"parents":[base_sha]})
    new_sha = r["sha"]
    r = curl("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/main", {"sha":new_sha})
    ok = r.get("object",{}).get("sha") == new_sha
    print("PUSHED via curl:", new_sha if ok else "FAILED "+str(r)[:150])
    return 0 if ok else 1

sys.exit(main())
