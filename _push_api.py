# -*- coding: utf-8 -*-
"""通过 GitHub REST API 推送本地提交（git 协议被网络干扰时的备用通道）。"""
import base64
import json
import subprocess
import sys
import urllib.request

OWNER = "muti2607"
REPO = "qq-bot"
COMMIT = "HEAD"


def token():
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\nusername=muti2607\n\n",
        capture_output=True, text=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line[9:].strip()
    raise SystemExit("no token")


def api(method, path, body=None, tok=None):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Basic " + base64.b64encode(f"{OWNER}:{tok}".encode()).decode(),
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "qqbot-deploy",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main():
    tok = token()
    print("token len", len(tok))
    # 远端当前 main
    ref = api("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/main", tok=tok)
    base_sha = ref["object"]["sha"]
    print("remote base", base_sha[:8])
    base_tree = api("GET", f"/repos/{OWNER}/{REPO}/git/commits/{base_sha}", tok=tok)["tree"]["sha"]

    # 本地提交改动的文件
    files = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", COMMIT],
        capture_output=True, text=True).stdout.split()
    entries = []
    for f in files:
        content = subprocess.run(["git", "show", f"{COMMIT}:{f}"], capture_output=True).stdout
        blob = api("POST", f"/repos/{OWNER}/{REPO}/git/blobs",
                   {"content": base64.b64encode(content).decode(), "encoding": "base64"}, tok=tok)
        entries.append({"path": f, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        print("blob", f, blob["sha"][:8])

    tree = api("POST", f"/repos/{OWNER}/{REPO}/git/trees",
               {"base_tree": base_tree, "tree": entries}, tok=tok)
    msg = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        capture_output=True, encoding="utf-8", errors="replace").stdout
    msg = (msg or "").strip() or "update"
    commit = api("POST", f"/repos/{OWNER}/{REPO}/git/commits",
                 {"message": msg, "tree": tree["sha"], "parents": [base_sha]}, tok=tok)
    print("commit", commit["sha"][:8])
    api("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/main",
        {"sha": commit["sha"], "force": False}, tok=tok)
    print("PUSHED via API:", commit["sha"])


if __name__ == "__main__":
    main()
