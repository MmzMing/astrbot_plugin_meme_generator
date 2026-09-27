"""消息媒体提取与下载工具。

AstrBot 各平台把图片、视频挂在消息链的不同位置：可能是组件对象，也可能是
OneBot 原始 segment；引用消息与合并转发里往往只有一个 ID，必须回查才能拿到
内容。这里把这些差异收敛成「媒体引用字符串」，再统一下载。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import tempfile
from pathlib import Path

import aiohttp
import astrbot.core.message.components as Comp
from astrbot.api import logger
from astrbot.core.platform import AstrMessageEvent

REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}
AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
_REPLY_PAYLOAD_ATTRS = ("chain", "message", "origin", "content")
_IMAGE_REF_ATTRS = ("url", "path", "file")


class MediaError(Exception):
    """媒体获取失败，异常文本为可直接展示给用户的提示。"""


# --- 消息链解析 ---


def _refs_from_segment_dict(segment: dict) -> list[str]:
    kind = segment.get("type")
    if kind == "image":
        data = segment.get("data") or {}
        for candidate in (
            data.get("url"),
            data.get("file"),
            segment.get("url"),
            segment.get("file"),
        ):
            if isinstance(candidate, str) and candidate:
                return [candidate]
        return []
    if kind in ("node", "forward"):
        content = (segment.get("data") or {}).get("content") or segment.get("content")
        return refs_from_chain(content) if isinstance(content, list) else []
    return []


def refs_from_chain(chain) -> list[str]:
    """递归收集消息链里的图片引用，兼容组件对象、原始 segment 与转发节点。"""
    refs: list[str] = []
    for item in chain or []:
        if isinstance(item, Comp.Image):
            for attr in _IMAGE_REF_ATTRS:
                value = getattr(item, attr, None)
                if isinstance(value, str) and value:
                    refs.append(value)
                    break
        elif isinstance(item, Comp.Reply):
            refs.extend(refs_from_chain(_reply_payload(item)))
        elif isinstance(item, Comp.Nodes):
            for node in getattr(item, "nodes", None) or []:
                content = getattr(node, "content", None)
                if isinstance(content, list):
                    refs.extend(refs_from_chain(content))
        elif isinstance(item, dict):
            refs.extend(_refs_from_segment_dict(item))
    return refs


def _reply_payload(reply) -> list:
    for attr in _REPLY_PAYLOAD_ATTRS:
        payload = getattr(reply, attr, None)
        if isinstance(payload, list):
            return payload
    return []


def _at_avatar(event: AstrMessageEvent) -> str | None:
    for segment in event.get_messages():
        if isinstance(segment, Comp.At):
            user_id = str(getattr(segment, "qq", "") or "")
            if user_id.isdigit():
                return AVATAR_URL.format(user_id=user_id)
    return None


async def _call_action(event: AstrMessageEvent, action: str, **params) -> dict:
    call_action = getattr(getattr(getattr(event, "bot", None), "api", None), "call_action", None)
    if not callable(call_action):
        return {}
    try:
        result = await call_action(action, **params)
    except Exception as exc:  # noqa: BLE001 - 平台不支持该 action 时静默降级
        logger.debug("OneBot %s 调用失败: %s", action, exc)
        return {}
    return result if isinstance(result, dict) else {}


async def _refs_via_api(event: AstrMessageEvent) -> list[str]:
    """回查被引用消息与合并转发的内容。"""
    message_ids: list[str] = []
    forward_ids: list[str] = []
    for segment in event.get_messages():
        if isinstance(segment, Comp.Reply):
            mid = getattr(segment, "id", None) or getattr(segment, "message_id", None)
            if mid is not None and str(mid).isdigit():
                message_ids.append(str(mid))
        elif isinstance(segment, dict):
            data = segment.get("data") or {}
            if segment.get("type") == "reply":
                mid = data.get("id") or data.get("message_id")
                if mid is not None and str(mid).isdigit():
                    message_ids.append(str(mid))
            elif segment.get("type") == "forward" and data.get("id"):
                forward_ids.append(str(data["id"]))

    refs: list[str] = []
    for message_id in dict.fromkeys(message_ids):
        result = await _call_action(event, "get_msg", message_id=int(message_id))
        payload = result.get("data") if isinstance(result.get("data"), dict) else result
        refs.extend(refs_from_chain(payload.get("message")))

    for forward_id in dict.fromkeys(forward_ids):
        result = await _call_action(event, "get_forward_msg", id=forward_id)
        if not result:
            result = await _call_action(event, "get_forward_msg", message_id=forward_id)
        payload = result.get("data") if isinstance(result.get("data"), dict) else result
        for node in payload.get("messages") or payload.get("nodes") or []:
            if isinstance(node, dict):
                refs.extend(refs_from_chain(node.get("content") or node.get("message")))
    return refs


async def first_image_ref(event: AstrMessageEvent) -> str | None:
    """按「引用消息 → 当前消息 → 回查 → @对象头像」的优先级取一张图。"""
    chain = event.get_messages()
    reply = next((seg for seg in chain if isinstance(seg, Comp.Reply)), None)
    if reply and (refs := refs_from_chain(_reply_payload(reply))):
        return refs[0]
    if refs := refs_from_chain(chain):
        return refs[0]
    if refs := await _refs_via_api(event):
        return refs[0]
    return _at_avatar(event)


async def all_image_refs(event: AstrMessageEvent) -> list[str]:
    """当前消息、引用消息、合并转发里的全部图片引用，去重并保持顺序。"""
    refs = refs_from_chain(event.get_messages())
    refs.extend(await _refs_via_api(event))
    if avatar := _at_avatar(event):
        refs.append(avatar)
    return list(dict.fromkeys(refs))


def _is_video_segment(item) -> bool:
    if isinstance(item, Comp.Video):
        return True
    return isinstance(item, dict) and item.get("type") == "video"


def _media_field(item, name: str):
    if isinstance(item, dict):
        data = item.get("data") or {}
        return data.get(name) or item.get(name)
    return getattr(item, name, None)


def _score_video(item) -> tuple[int, str | None]:
    """给视频源打分：可直接下载的 url 最优，本地路径次之，文件 ID 最差。"""
    url = _media_field(item, "url")
    if isinstance(url, str) and url.startswith("http"):
        return 100, url
    path = _media_field(item, "path")
    if isinstance(path, str) and os.path.isabs(path) and os.path.exists(path):
        return 90, path
    file_id = _media_field(item, "file")
    if isinstance(file_id, str) and file_id:
        return 50, file_id
    return 0, None


def video_source(event: AstrMessageEvent) -> str | None:
    """从当前消息或引用消息里挑一个最可靠的视频源。"""
    chain = list(event.get_messages())
    reply = next((seg for seg in chain if isinstance(seg, Comp.Reply)), None)
    if reply:
        chain = _reply_payload(reply) + chain

    candidates = [
        scored
        for scored in (_score_video(item) for item in chain if _is_video_segment(item))
        if scored[1]
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]


async def resolve_file_id(event: AstrMessageEvent, file_id: str) -> str | None:
    """通过 OneBot get_file 把文件 ID 换成可下载的 url 或本地路径。"""
    result = await _call_action(event, "get_file", file_id=file_id)
    if not result:
        result = await _call_action(event, "get_file", file=file_id)
    payload = result.get("data") if isinstance(result.get("data"), dict) else result
    for key in ("url", "file"):
        value = payload.get(key)
        if isinstance(value, str) and (value.startswith("http") or os.path.exists(value)):
            return value
    return None


# --- 下载 ---


def _decode_base64(payload: str) -> bytes | None:
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        logger.warning("无法解析的媒体引用: %.64s", payload)
        return None


def _read_local_ref(ref: str) -> bytes | None:
    if ref.startswith("base64://"):
        return _decode_base64(ref[len("base64://") :])
    path = Path(ref)
    if path.is_file():
        try:
            return path.read_bytes()
        except OSError as exc:
            logger.error("读取本地文件失败: %s -> %s", ref, exc)
            return None
    return _decode_base64(ref)


async def download(ref: str, timeout: int = 30) -> bytes | None:
    """下载媒体引用，支持 http(s)、本地路径与 base64。"""
    if not ref.startswith(("http://", "https://")):
        return await asyncio.to_thread(_read_local_ref, ref)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                ref, headers=REQUEST_HEADERS, timeout=timeout
            ) as response:
                if response.status != 200:
                    logger.warning("下载失败 HTTP %s: %s", response.status, ref)
                    return None
                return await response.read()
    except Exception as exc:  # noqa: BLE001
        logger.error("下载媒体失败: %s -> %s", ref, exc)
        return None


async def download_all(refs: list[str], timeout: int = 30) -> list[bytes]:
    """并发下载多个引用，按输入顺序返回成功的那些。"""
    results = await asyncio.gather(*(download(ref, timeout) for ref in refs))
    return [data for data in results if data]


def _remove_quietly(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.warning("删除临时文件失败: %s -> %s", path, exc)


def remove_temp(path: str | None) -> None:
    """删除 fetch_video_to_temp 产生的临时文件；非临时文件不会被删。"""
    _remove_quietly(path or "")


async def fetch_video_to_temp(source: str, max_size_mb: float) -> tuple[str, bool]:
    """把视频源落到本地，返回 (路径, 是否为需要清理的临时文件)。

    本地文件直接复用原路径；远端文件流式写入临时文件，边写边校验体积上限，
    避免把整个视频读进内存。
    """
    if not source.startswith(("http://", "https://")):
        if os.path.exists(source):
            return source, False
        raise MediaError(f"无法访问该视频：{source}")

    limit = int(max_size_mb * 1024 * 1024)
    handle = tempfile.NamedTemporaryFile(suffix=".video", delete=False)
    handle.close()
    temp_path = handle.name
    try:
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                source, headers=REQUEST_HEADERS, timeout=timeout
            ) as response:
                if response.status != 200:
                    raise MediaError(f"视频下载失败（HTTP {response.status}）")
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > limit:
                    raise MediaError(f"视频超过 {max_size_mb:g}MB 上限，无法处理")
                written = 0
                with open(temp_path, "wb") as fp:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        written += len(chunk)
                        if written > limit:
                            raise MediaError(f"视频超过 {max_size_mb:g}MB 上限，无法处理")
                        fp.write(chunk)
    except BaseException:
        _remove_quietly(temp_path)
        raise
    return temp_path, True
