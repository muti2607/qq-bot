"""自测用的 mock OneBot（端口 3997）"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 3997


class M(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        action = urlparse(self.path).path.strip("/")
        if action == "send_private_msg":
            out = {"status": "ok", "retcode": 0, "data": {"message_id": 11}}
        elif action == "send_group_msg":
            out = {"status": "ok", "retcode": 0, "data": {"message_id": 12}}
        elif action == "get_login_info":
            out = {"status": "ok", "retcode": 0, "data": {"user_id": 10001, "nickname": "小助手"}}
        elif action == "get_stranger_info":
            out = {"status": "ok", "retcode": 0, "data": {"user_id": body.get("user_id"), "nickname": "好友A"}}
        elif action == "get_friend_list":
            out = {"status": "ok", "retcode": 0, "data": [{"user_id": 10001, "nickname": "好友A"}]}
        elif action == "get_group_list":
            out = {"status": "ok", "retcode": 0, "data": [{"group_id": 123456, "group_name": "测试群", "member_count": 42}]}
        else:
            out = {"status": "failed", "retcode": 404, "message": "unknown action"}
        print("MOCK", action, json.dumps(body, ensure_ascii=False)[:120], file=sys.stderr, flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(out).encode())


HTTPServer(("127.0.0.1", PORT), M).serve_forever()
