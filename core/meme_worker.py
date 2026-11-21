"""独立的表情包 worker 进程。

通过本机 TCP 上的长度前缀 JSON 帧与主进程通信。把所有原生库加载、资源检查
和图片生成都放在这里，安装/卸载外部库后只需重启本进程。

stdout/stderr 只用于日志（Rust tracing 默认写 stdout），不会污染 RPC。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_MAX_FRAME = 256 * 1024 * 1024
_LOCK_RETRY_SECONDS = 20.0
_LOCK_HANDLE = None


def _acquire_worker_lock(meme_home: Path):
    """同一数据目录只允许一个 worker，避免并发下载内置资源。"""
    lock_path = meme_home / ".meme_worker.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+b")
    except OSError as exc:
        sys.stderr.write(f"cannot open worker lock {lock_path}: {exc}\n")
        return None

    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"1")
            handle.flush()
    except OSError:
        pass

    deadline = time.time() + _LOCK_RETRY_SECONDS
    while True:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.time() >= deadline:
                handle.close()
                return None
            # 旧 worker 通常会在父进程连接断开后自行退出，稍等再抢。
            time.sleep(0.5)


def _read_exact(stream, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return b""
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(stream) -> dict | None:
    header = _read_exact(stream, 4)
    if len(header) != 4:
        return None
    (size,) = struct.unpack(">I", header)
    if size <= 0 or size > _MAX_FRAME:
        return None
    body = _read_exact(stream, size)
    if len(body) != size:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_frame(stream, lock: threading.Lock, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    with lock:
        stream.write(struct.pack(">I", len(body)))
        stream.write(body)
        stream.flush()


def _meme_metadata(meme) -> dict:
    info = meme.info
    params = info.params
    return {
        "key": meme.key,
        "keywords": [str(item) for item in info.keywords],
        "tags": sorted(str(item) for item in info.tags),
        "min_images": int(params.min_images),
        "max_images": int(params.max_images),
        "min_texts": int(params.min_texts),
        "max_texts": int(params.max_texts),
        "default_texts": [str(item) for item in params.default_texts],
    }


def _error_payload(result) -> dict:
    name = type(result).__name__
    payload = {"type": name, "message": str(result)}
    for attr in ("error", "path"):
        value = getattr(result, attr, None)
        if value is not None:
            payload[attr] = str(value)
    for attr in ("min", "max", "actual"):
        value = getattr(result, attr, None)
        if value is not None:
            payload[attr] = int(value)
    feedback = getattr(result, "feedback", None)
    if feedback is not None:
        payload["feedback"] = str(feedback)
    return payload


def _generate(memes: dict, request: dict) -> dict:
    from meme_generator import Image as MemeImage

    key = str(request.get("key") or "")
    meme = memes.get(key)
    if meme is None:
        return {
            "ok": False,
            "error": {"type": "MemeNotFound", "message": f"模板不存在: {key}"},
        }

    images = [
        MemeImage(str(item.get("name") or ""), base64.b64decode(item.get("data") or ""))
        for item in request.get("images", [])
    ]
    texts = [str(item) for item in request.get("texts", [])]
    options = request.get("options") or {}
    result = meme.generate(images, texts, options)
    if isinstance(result, bytes):
        return {"ok": True, "data": base64.b64encode(result).decode("ascii")}
    return {"ok": False, "error": _error_payload(result)}


def main() -> int:
    global _LOCK_HANDLE

    meme_home_text = os.environ.get("MEME_HOME", "").strip()
    if not meme_home_text:
        sys.stderr.write("MEME_HOME is not set\n")
        return 2
    try:
        port = int(os.environ.get("MEME_RPC_PORT", "0"))
    except ValueError:
        port = 0
    token = os.environ.get("MEME_RPC_TOKEN", "")
    if port <= 0 or not token:
        sys.stderr.write("MEME_RPC_PORT / MEME_RPC_TOKEN is not set\n")
        return 2

    meme_home = Path(meme_home_text).expanduser()
    _LOCK_HANDLE = _acquire_worker_lock(meme_home)
    if _LOCK_HANDLE is None:
        sys.stderr.write(
            "another meme worker is already running for this MEME_HOME; "
            "waiting for the old process to exit timed out\n"
        )
        return 3

    try:
        import meme_generator
        from meme_generator.resources import check_resources
    except Exception:
        sys.stderr.write("failed to import meme_generator:\n")
        sys.stderr.write(traceback.format_exc())
        return 4

    try:
        memes = {meme.key: meme for meme in meme_generator.get_memes()}
        metadata = sorted(
            (_meme_metadata(meme) for meme in memes.values()),
            key=lambda item: item["key"],
        )
        version = str(meme_generator.get_version())
    except Exception:
        sys.stderr.write("failed to load meme registry:\n")
        sys.stderr.write(traceback.format_exc())
        return 5

    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=60)
        sock.settimeout(None)
        rpc_in = sock.makefile("rb")
        rpc_out = sock.makefile("wb")
    except OSError as exc:
        sys.stderr.write(f"failed to connect to parent RPC port {port}: {exc}\n")
        return 6

    write_lock = threading.Lock()
    try:
        _write_frame(rpc_out, write_lock, {"token": token})
        _write_frame(
            rpc_out,
            write_lock,
            {"event": "ready", "version": version, "memes": len(memes)},
        )
    except OSError as exc:
        sys.stderr.write(f"failed to send worker handshake: {exc}\n")
        return 6

    workers = max(2, min(4, os.cpu_count() or 2))
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="meme-worker")

    def respond(request_id, payload: dict) -> None:
        message = dict(payload)
        message["id"] = request_id
        try:
            _write_frame(rpc_out, write_lock, message)
        except (BrokenPipeError, OSError):
            pass

    def handle(request: dict) -> None:
        request_id = request.get("id")
        command = request.get("cmd")
        try:
            if command == "ping":
                respond(request_id, {"ok": True, "version": version})
            elif command == "list_memes":
                respond(request_id, {"ok": True, "memes": metadata})
            elif command == "generate":
                respond(request_id, _generate(memes, request))
            elif command == "check_resources":
                check_resources()
                respond(request_id, {"ok": True})
            else:
                respond(
                    request_id,
                    {
                        "ok": False,
                        "error": {
                            "type": "UnknownCommand",
                            "message": f"未知 worker 命令: {command}",
                        },
                    },
                )
        except Exception as exc:
            respond(
                request_id,
                {
                    "ok": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc) or repr(exc),
                    },
                },
            )

    while True:
        request = _read_frame(rpc_in)
        if request is None:
            break
        if request.get("cmd") == "shutdown":
            respond(request.get("id"), {"ok": True})
            break
        executor.submit(handle, request)

    executor.shutdown(wait=False, cancel_futures=True)
    try:
        rpc_in.close()
        rpc_out.close()
        sock.close()
    except OSError:
        pass
    try:
        _LOCK_HANDLE.close()
    except OSError:
        pass
    _LOCK_HANDLE = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
