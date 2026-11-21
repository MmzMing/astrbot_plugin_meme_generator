"""主进程侧的 worker 客户端。

负责启动/重启子进程、通过本机 TCP 收发 RPC 帧，并把 worker 的错误转换成
主进程可用的异常。安装或卸载外部库后只需要 ``restart()``，无需重启 AstrBot。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import re
import secrets
import struct
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Any, Callable

from astrbot.api import logger

from .meme_types import MemeImage, WorkerMeme

_MAX_FRAME = 256 * 1024 * 1024
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_WARNING_MARKERS = (
    "failed to load library",
    "is compiled with rustc",
    "may not work correctly",
    "traceback (most recent call last)",
)


class MemeServiceError(RuntimeError):
    """worker 启动、通信或退出失败。"""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        stderr_tail: list[str] | None = None,
    ):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail or []


class MemeGenerationError(RuntimeError):
    """worker 返回的模板生成错误。"""

    def __init__(self, message: str, error: dict | None = None):
        super().__init__(message)
        self.error = error or {}


def _format_generation_error(error: dict) -> str:
    kind = str(error.get("type") or "")
    message = str(error.get("message") or "")
    if kind == "ImageAssetMissing":
        return f"缺少图片资源：{error.get('path') or message}"
    if kind == "ImageDecodeError":
        return f"图片解码失败：{error.get('error') or message}"
    if kind == "ImageEncodeError":
        return f"图片编码失败：{error.get('error') or message}"
    if kind == "DeserializeError":
        return f"参数解析失败：{error.get('error') or message}"
    if kind == "ImageNumberMismatch":
        minimum = error.get("min", "?")
        maximum = error.get("max", "?")
        actual = error.get("actual", "?")
        return f"图片数量不符，应为 {minimum} ~ {maximum}，实际传入 {actual}"
    if kind == "TextNumberMismatch":
        minimum = error.get("min", "?")
        maximum = error.get("max", "?")
        actual = error.get("actual", "?")
        return f"文字数量不符，应为 {minimum} ~ {maximum}，实际传入 {actual}"
    if kind == "TextOverLength":
        text = str(error.get("message") or "")
        return f"文字过长：{text if len(text) <= 10 else text[:10] + '...'}"
    if kind == "MemeFeedback":
        return str(error.get("feedback") or message)
    if kind == "MemeNotFound":
        return message or "模板不存在"
    return message or f"表情包生成失败（{kind or '未知错误'}）"


class MemeService:
    """管理独立 meme worker 的异步客户端。"""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        on_starting: Callable[[], Any] | None = None,
        on_started: Callable[[], Any] | None = None,
        max_concurrent: int = 4,
    ):
        self.data_dir = Path(data_dir)
        self._on_starting = on_starting
        self._on_started = on_started
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self._lifecycle_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._server: asyncio.AbstractServer | None = None
        self._rpc_reader: asyncio.StreamReader | None = None
        self._rpc_writer: asyncio.StreamWriter | None = None
        self._rpc_task: asyncio.Task | None = None
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._connected_event: asyncio.Event | None = None
        self._token = ""
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._alive = False
        self._metadata: list[WorkerMeme] | None = None
        self._version = ""
        self._output_tail: deque[str] = deque(maxlen=40)
        self._load_warnings: list[str] = []

    # --- 生命周期 ---

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def version(self) -> str:
        return self._version

    @property
    def load_warnings(self) -> list[str]:
        return list(self._load_warnings)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            await self._start_locked()

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_locked()

    async def restart(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_locked()
            await self._start_locked()

    async def _start_locked(self) -> None:
        if self.is_running and self._alive:
            return

        # 连接已断但进程还活着（worker 卡死）时先清理，避免两个 worker 抢锁。
        if self._process is not None:
            await self._stop_locked()

        self._metadata = None
        self._load_warnings = []
        self._output_tail.clear()
        self._token = secrets.token_hex(16)
        self._connected_event = asyncio.Event()
        await self._call_hook(self._on_starting)

        try:
            self._server = await asyncio.start_server(
                self._handle_connection, "127.0.0.1", 0, limit=_MAX_FRAME
            )
        except OSError as exc:
            raise MemeServiceError(f"无法监听 worker RPC 端口: {exc}") from exc
        try:
            port = int(self._server.sockets[0].getsockname()[1])
        except (AttributeError, IndexError, OSError) as exc:
            await self._close_server(wait=False)
            raise MemeServiceError(f"无法创建 worker RPC 端口: {exc}") from exc

        try:
            process = await self._spawn(port)
        except Exception:
            await self._close_server(wait=False)
            raise
        try:
            self._process = process
            self._alive = True
            self._stdout_task = asyncio.create_task(
                self._read_output(process.stdout, "stdout"),
                name="meme-worker-stdout",
            )
            self._stderr_task = asyncio.create_task(
                self._read_output(process.stderr, "stderr"),
                name="meme-worker-stderr",
            )
        except Exception:
            await self._close_server(wait=False)
            raise

        connected = self._connected_event
        event_task = asyncio.create_task(connected.wait())
        exit_task = asyncio.create_task(process.wait())
        try:
            done, _pending = await asyncio.wait(
                {event_task, exit_task},
                timeout=90,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if event_task not in done:
                exit_code = process.returncode
                reason = (
                    "worker 在连接前退出"
                    if exit_task in done
                    else "等待 worker 连接超时"
                )
                if exit_task in done:
                    readers = [
                        task
                        for task in (self._stdout_task, self._stderr_task)
                        if task is not None
                    ]
                    if readers:
                        try:
                            await asyncio.wait_for(
                                asyncio.gather(*readers), timeout=2
                            )
                        except Exception:
                            pass
                tail = list(self._output_tail)
                await self._stop_locked()
                detail = " | ".join(tail[-3:]) if tail else "worker 没有输出"
                raise MemeServiceError(
                    f"表情包 worker 启动失败：{reason}（退出码 {exit_code}）；{detail}",
                    exit_code=exit_code,
                    stderr_tail=tail,
                )
        finally:
            for task in (event_task, exit_task):
                if not task.done():
                    task.cancel()
            # 连接已经建立；Server.wait_closed() 会等所有活跃连接结束，
            # 这里只停止接受新连接，不能等待。
            await self._close_server(wait=False)

        try:
            await self._request("ping", timeout=60, ensure_started=False)
        except Exception as exc:
            tail = list(self._output_tail)
            exit_code = process.returncode
            await self._stop_locked()
            detail = " | ".join(tail[-5:]) if tail else "worker 没有输出"
            raise MemeServiceError(
                f"表情包 worker 启动失败：{exc}；{detail}",
                exit_code=exit_code,
                stderr_tail=tail,
            ) from exc

        await self._call_hook(self._on_started)

    async def _stop_locked(self) -> None:
        process = self._process

        if process is not None and process.returncode is None:
            if self._alive and self._rpc_writer is not None:
                try:
                    await self._request(
                        "shutdown",
                        timeout=3,
                        ensure_started=False,
                        expected_alive=True,
                    )
                except Exception:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        pass

        self._process = None
        self._alive = False

        if self._rpc_writer is not None:
            try:
                self._rpc_writer.close()
            except Exception:
                pass
        self._rpc_writer = None
        self._rpc_reader = None

        for task in (self._rpc_task, self._stdout_task, self._stderr_task):
            if task is not None:
                task.cancel()
        for task in (self._rpc_task, self._stdout_task, self._stderr_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._rpc_task = None
        self._stdout_task = None
        self._stderr_task = None

        await self._close_server(wait=False)
        self._fail_pending(MemeServiceError("表情包 worker 已停止"))

    async def _close_server(self, *, wait: bool = True) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            if wait:
                try:
                    await server.wait_closed()
                except Exception:
                    pass

    @staticmethod
    async def _call_hook(hook: Callable[[], Any] | None) -> None:
        if hook is None:
            return
        try:
            result = hook()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("表情包 worker 生命周期回调失败: %s", exc)

    async def _spawn(self, port: int) -> asyncio.subprocess.Process:
        worker_path = Path(__file__).with_name("meme_worker.py")
        if not worker_path.is_file():
            raise MemeServiceError(f"找不到 worker 脚本: {worker_path}")
        if not sys.executable:
            raise MemeServiceError(
                "当前运行环境没有可用的 Python 解释器，无法启动表情包 worker"
            )
        if getattr(sys, "frozen", False):
            raise MemeServiceError(
                "当前 AstrBot 以冻结方式运行，无法启动独立 worker；"
                "请改用源码或容器部署，或暂时禁用外部库"
            )

        env = os.environ.copy()
        env["MEME_HOME"] = str(self.data_dir)
        env["MEME_RPC_PORT"] = str(port)
        env["MEME_RPC_TOKEN"] = self._token
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("RUST_LOG", "info")

        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            return await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                str(worker_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(worker_path.parent.parent),
                env=env,
                limit=_MAX_FRAME,
                **kwargs,
            )
        except (OSError, NotImplementedError) as exc:
            raise MemeServiceError(f"启动表情包 worker 失败: {exc}") from exc

    # --- RPC ---

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            message = await asyncio.wait_for(self._read_frame(reader), timeout=30)
        except Exception:
            writer.close()
            return
        if not isinstance(message, dict) or message.get("token") != self._token:
            writer.close()
            return
        event = self._connected_event
        if event is None or event.is_set():
            writer.close()
            return
        self._rpc_reader = reader
        self._rpc_writer = writer
        self._rpc_task = asyncio.create_task(
            self._read_rpc(reader), name="meme-worker-rpc"
        )
        event.set()

    @staticmethod
    async def _read_frame(reader: asyncio.StreamReader) -> dict:
        header = await reader.readexactly(4)
        (size,) = struct.unpack(">I", header)
        if size <= 0 or size > _MAX_FRAME:
            raise MemeServiceError(f"worker 帧长度异常: {size}")
        body = await reader.readexactly(size)
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise MemeServiceError("worker 返回了非对象帧")
        return data

    async def _request(
        self,
        command: str,
        *,
        timeout: float,
        ensure_started: bool = True,
        expected_alive: bool = False,
        **payload: Any,
    ) -> dict:
        if ensure_started:
            await self.start()

        writer = self._rpc_writer
        if writer is None:
            raise MemeServiceError("表情包 worker 未连接")
        if expected_alive and not self._alive:
            raise MemeServiceError("表情包 worker 已退出")

        loop = asyncio.get_running_loop()
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = future

        message = {"id": request_id, "cmd": command, **payload}
        try:
            body = json.dumps(message, ensure_ascii=False).encode("utf-8")
            async with self._write_lock:
                writer.write(struct.pack(">I", len(body)))
                writer.write(body)
                await writer.drain()
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise
        finally:
            self._pending.pop(request_id, None)

    async def _read_rpc(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                message = await self._read_frame(reader)
                if "event" in message:
                    self._handle_event(message)
                    continue
                request_id = message.get("id")
                future = self._pending.pop(request_id, None)
                if future is not None and not future.done():
                    future.set_result(message)
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except Exception as exc:
            logger.debug("读取表情包 worker RPC 失败: %s", exc)
        finally:
            self._alive = False
            self._fail_pending(
                MemeServiceError(
                    "表情包 worker 连接已断开",
                    exit_code=(
                        self._process.returncode if self._process is not None else None
                    ),
                    stderr_tail=list(self._output_tail),
                )
            )

    async def _read_output(self, stream: asyncio.StreamReader | None, source: str) -> None:
        if stream is None:
            return
        buffer = ""
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", "replace")
                while True:
                    positions = [
                        index
                        for index in (buffer.find("\n"), buffer.find("\r"))
                        if index >= 0
                    ]
                    if not positions:
                        break
                    index = min(positions)
                    line = buffer[:index]
                    buffer = buffer[index + 1 :]
                    self._handle_output_line(line, source)
                if len(buffer) > 8192:
                    self._handle_output_line(buffer, source)
                    buffer = ""
            if buffer:
                self._handle_output_line(buffer, source)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("读取表情包 worker %s 失败: %s", source, exc)

    def _handle_output_line(self, raw: str, source: str) -> None:
        line = _ANSI_RE.sub("", raw).strip()
        if not line:
            return
        self._output_tail.append(line)
        lower = line.lower()
        if any(marker in lower for marker in _WARNING_MARKERS):
            self._load_warnings.append(line)
            logger.warning("[meme-worker:%s] %s", source, line)
        elif "loaded library" in lower:
            logger.info("[meme-worker:%s] %s", source, line)
        else:
            logger.debug("[meme-worker:%s] %s", source, line)

    def _handle_event(self, message: dict) -> None:
        event = message.get("event")
        if event == "ready":
            self._version = str(message.get("version") or "")
            logger.info(
                "表情包 worker 就绪：meme_generator %s，%d 个模板",
                self._version or "?",
                int(message.get("memes") or 0),
            )

    def _fail_pending(self, exc: Exception) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(exc)

    # --- 业务接口 ---

    async def list_memes(self) -> list[WorkerMeme]:
        response = await self._request("list_memes", timeout=60)
        if not response.get("ok"):
            error = response.get("error") or {}
            raise MemeServiceError(
                str(error.get("message") or "读取模板列表失败")
            )
        self._metadata = [
            WorkerMeme.from_dict(item)
            for item in response.get("memes", [])
            if isinstance(item, dict)
        ]
        return list(self._metadata)

    async def check_resources(self) -> None:
        response = await self._request("check_resources", timeout=1800)
        if not response.get("ok"):
            error = response.get("error") or {}
            raise MemeServiceError(
                str(error.get("message") or "内置资源检查失败")
            )

    async def generate(
        self,
        key: str,
        images: list[MemeImage],
        texts: list[str],
        options: dict[str, Any],
        timeout: float,
    ) -> bytes:
        payload_images = [
            {
                "name": image.name,
                "data": base64.b64encode(image.data).decode("ascii"),
            }
            for image in images
        ]
        async with self._semaphore:
            response = await self._request(
                "generate",
                timeout=timeout + 10,
                key=key,
                images=payload_images,
                texts=list(texts),
                options=dict(options),
            )
        if response.get("ok"):
            return base64.b64decode(response.get("data") or "")
        error = response.get("error") or {}
        raise MemeGenerationError(_format_generation_error(error), error)
