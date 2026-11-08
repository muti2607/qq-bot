#!/usr/bin/env python3
"""把 docker 网桥地址转发到本机的隧道端口。

两个通道：
  172.18.0.1:18999 -> 127.0.0.1:18999  (our-free-model 转发，opencode/kilo 池)
  172.18.0.1:18998 -> 127.0.0.1:18998  (chan-gateway，CodeBuddy/Trae/Buddy 独立额度池)

docker 容器访问不到宿主机 loopback，只能走网桥网关 172.18.0.1，
所以这个进程只监听网桥地址（不暴露公网），把连接转给 loopback。
"""
from __future__ import annotations

import select
import socket
import sys
import threading

# (监听地址, 目标地址)
ROUTES = [
    (("172.18.0.1", 18999), ("127.0.0.1", 18999)),
    (("172.18.0.1", 18998), ("127.0.0.1", 18998)),
]


def pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 300)
            if not r:
                continue
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass


def serve(listen, target) -> None:
    def handle(client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(target, timeout=5)
        except Exception:
            client.close()
            return
        pipe(client, upstream)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(listen)
    except OSError as e:
        print(f"bind {listen} failed: {e}", flush=True)
        return
    srv.listen(64)
    print(f"ai-bridge {listen[0]}:{listen[1]} -> {target[0]}:{target[1]}", flush=True)
    while True:
        try:
            conn, _ = srv.accept()
        except Exception:
            continue
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def main() -> None:
    for listen, target in ROUTES:
        threading.Thread(target=serve, args=(listen, target), daemon=True).start()
    threading.Event().wait()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
