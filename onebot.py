"""OneBot 11 客户端：通过 NapCat 的 HTTP API 发群消息/私聊，并查询群列表。"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
import db

TIMEOUT = 20.0


class OneBotError(RuntimeError):
    pass


def _conf() -> tuple[str, str]:
    s = db.get_settings()
    return (s.get("onebot_http") or "http://127.0.0.1:3001").rstrip("/"), s.get("onebot_token") or ""


def _headers() -> dict[str, str]:
    _, token = _conf()
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _url(action: str) -> str:
    base, _ = _conf()
    return f"{base}/{action}"


def call(action: str, payload: dict[str, Any] | None = None) -> Any:
    # trust_env=False：不吃系统代理设置，内网 127.0.0.1 / napcat:3001 一定直连
    try:
        with httpx.Client(timeout=TIMEOUT, headers=_headers(), trust_env=False) as client:
            resp = client.post(_url(action), json=payload or {})
    except httpx.HTTPError as e:  # 网络层错误
        raise OneBotError(f"无法连接 OneBot 服务（{_conf()[0]}）：{e}") from e
    if resp.status_code >= 400:
        raise OneBotError(f"{action} 返回 HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise OneBotError(f"{action} 返回了非 JSON 内容: {resp.text[:300]}") from e
    if isinstance(data, dict) and data.get("status") == "failed":
        raise OneBotError(f"{action} 失败: retcode={data.get('retcode')} {data.get('message') or data.get('wording') or ''}".strip())
    return data.get("data") if isinstance(data, dict) else data


def image_segment(path: Path) -> dict[str, Any]:
    """本地图片转 base64 的 image 消息段，避免依赖 NapCat 能读到同一路径。"""
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode()
    return {"type": "image", "data": {"file": f"base64://{b64}"}}


def text_segment(text: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


def at_all_segment() -> dict[str, Any]:
    return {"type": "at", "data": {"qq": "all"}}


def send_message(target_type: str, target_id: int | str, segments: list[dict[str, Any]], at_all: bool = False) -> Any:
    if at_all:
        segments = [at_all_segment(), text_segment("\n")] + segments
    if target_type == "group":
        return call("send_group_msg", {"group_id": int(target_id), "message": segments})
    return call("send_private_msg", {"user_id": int(target_id), "message": segments})


def get_login_info() -> dict[str, Any]:
    return call("get_login_info") or {}


def get_group_list() -> list[dict[str, Any]]:
    data = call("get_group_list") or []
    return data if isinstance(data, list) else []


def get_friend_list() -> list[dict[str, Any]]:
    data = call("get_friend_list") or []
    return data if isinstance(data, list) else []


def get_stranger_info(user_id: int | str) -> dict[str, Any]:
    return call("get_stranger_info", {"user_id": int(user_id)}) or {}


def get_status() -> dict[str, Any]:
    try:
        info = get_login_info()
        return {"online": True, "self_id": info.get("user_id"), "nickname": info.get("nickname")}
    except OneBotError as e:
        return {"online": False, "error": str(e)}


def send_poke(group_id: int | None, user_id: int) -> Any:
    """戳一戳。群聊要带 group_id，私聊只带 user_id。

    NapCat 对群戳的实现是 send_poke；早期版本用 group_poke，这里自动降级。
    """
    payload: dict[str, Any] = {"user_id": int(user_id)}
    if group_id:
        payload["group_id"] = int(group_id)
        try:
            return call("send_poke", payload)
        except OneBotError:
            return call("group_poke", payload)
    try:
        return call("send_poke", payload)
    except OneBotError:
        return call("friend_poke", payload)


def face_segment(face_id: int | str) -> dict[str, Any]:
    """QQ 小黄脸表情。"""
    return {"type": "face", "data": {"id": str(face_id)}}


def read_container_file(path: str) -> bytes | None:
    """读取 NapCat 容器内的文件。

    机器人跑在另一个容器里，够不到宿主机路径；这里用共享卷里的软链不行，
    所以走 NapCat 的 HTTP get_image 拿到的路径时，只能靠 compose 共享目录。
    没有共享目录时返回 None，让调用方退回 URL 下载。
    """
    try:
        p = Path(path)
        if p.is_file():
            return p.read_bytes()
    except OSError:
        pass
    return None
