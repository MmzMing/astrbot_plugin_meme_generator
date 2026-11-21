"""外部表情库管理。

meme_generator 只会加载 ``$MEME_HOME/libraries/`` 下的原生动态库，是否加载由
``$MEME_HOME/config.toml`` 的 ``[meme] load_external_memes`` 决定。动态库由独立
worker 子进程在启动时读入，所以安装或卸载之后只需重启 worker（插件会自动做），
不必重启 AstrBot——本模块只负责把文件放到位、写配置、维护安装记录。
"""

from __future__ import annotations

import asyncio
import json
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote, urljoin, urlsplit

import aiohttp
from astrbot.api import logger

LIBRARIES_DIR_NAME = "libraries"
RESOURCES_DIR_NAME = "resources"
MANIFEST_FILE_NAME = "libraries.json"
CONFIG_FILE_NAME = "config.toml"
CONFIG_SECTION = "meme"
CONFIG_KEY = "load_external_memes"
BUILTIN_KEY = "load_builtin_memes"

# 列表里默认展示的库。内置库编译在 meme_generator 里，常驻且不可卸载；
# meme-emoji 是唯一默认推荐的外部库，`/表情包库 安装` 不带参数时装它。
LIBRARY_CATALOG = (
    {
        "name": "内置",
        "repo": "",
        "memes": 300,
        "builtin": True,
        "note": "编译在 meme_generator 里，卸载不掉",
    },
    {
        "name": "meme-emoji",
        "repo": "anyliew/meme-emoji",
        "memes": 434,
        "note": "二游与梗图，MIT",
    },
)
DEFAULT_LIBRARY = "meme-emoji"

# 常用库的简写，省得每次敲 owner/repo；其他仓库直接写 owner/repo 即可。
KNOWN_LIBRARIES = {
    "meme-emoji": "anyliew/meme-emoji",
}

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_HTTP_TIMEOUT = aiohttp.ClientTimeout(
    total=None, connect=20, sock_connect=20, sock_read=120
)
# API 和探路请求不需要等两分钟，尽快失败并换源。
_API_TIMEOUT = aiohttp.ClientTimeout(
    total=30, connect=10, sock_connect=10, sock_read=30
)
_PROBE_TIMEOUT = aiohttp.ClientTimeout(
    total=30, connect=10, sock_connect=10, sock_read=30
)
# 素材文件通常很小，单文件不必等两分钟。
_RESOURCE_TIMEOUT = aiohttp.ClientTimeout(
    total=90, connect=15, sock_connect=15, sock_read=90
)
_REDIRECT_STATUS = (301, 302, 303, 307, 308)
_MAX_REDIRECTS = 6
_ATTEMPTS_PER_SOURCE = 2
_DIRECT_LABEL = "直连"
_RESOURCE_CONCURRENCY = 6
_RESOURCE_RETRIES = 2

# Release 资产实际从 release-assets.githubusercontent.com 下载，这个域名在
# 国内经常连不上。直连失败后依次尝试这些公共加速前缀；都能被 github_proxy
# 覆盖，所以失效了也不影响用户自己指定。
DEFAULT_MIRRORS = (
    "https://ghfast.top/",
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
)


class _DownloadRetry(Exception):
    """单次下载失败，可以换源重试。"""


def _normalize_proxy(value: str) -> str:
    """把 ``127.0.0.1:7890`` 这类写法补成 aiohttp 认的 URL。"""
    text = (value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    return text


def _system_proxy() -> str:
    """读取操作系统代理。

    内置资源由 Rust reqwest 下载，默认启用 system-proxy；外部库由 aiohttp
    下载，默认不会读系统代理。这里用 urllib 的跨平台实现补齐 Windows 注册表、
    macOS 系统设置和 Linux 环境变量。
    """
    try:
        proxies = urllib.request.getproxies()
    except Exception as exc:
        logger.debug("读取系统代理失败: %s", exc)
        return ""
    for key in ("https", "http", "all"):
        value = proxies.get(key)
        if value:
            logger.debug("检测到系统代理 %s=%s", key, value)
            return _normalize_proxy(value)
    return ""


def _looks_like_proxy(value: str) -> bool:
    """判断 ``github_proxy`` 里填的是代理地址还是 GitHub 加速前缀。"""
    text = (value or "").strip()
    if not text:
        return False
    lower = text.lower()
    if lower.startswith(("socks4://", "socks5://", "socks://")):
        return True
    if "://" not in text:
        # host:port 或 127.0.0.1:7890 这类写法按代理处理
        if ":" not in text:
            return False
        text = f"http://{text}"
    try:
        parsed = urlsplit(text)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    if not host:
        return False
    if parsed.path not in ("", "/"):
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if host.startswith(("10.", "192.168.")):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
        except (IndexError, ValueError):
            second = 0
        if 16 <= second <= 31:
            return True
    # 有显式端口且没有路径，通常是代理；无端口的域名按加速前缀处理。
    return port is not None


def _probe_library(path: Path) -> tuple[bool, str]:
    """在子进程里尝试加载动态库，避免坏库把主进程带崩。"""
    if not sys.executable or getattr(sys, "frozen", False):
        return True, "当前运行环境无法预检，跳过"
    code = (
        "import ctypes, os, sys\n"
        "path = sys.argv[1]\n"
        "if sys.platform == 'win32':\n"
        "    handle = os.add_dll_directory(os.path.dirname(path))\n"
        "    lib = ctypes.WinDLL(path)\n"
        "else:\n"
        "    lib = ctypes.CDLL(path)\n"
        "getattr(lib, 'MEME_PACK_DECLARATION')\n"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code, str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if completed.returncode == 0:
        return True, ""
    lines = [
        line.strip()
        for line in (completed.stderr or completed.stdout).splitlines()
        if line.strip()
    ]
    detail = " | ".join(lines[-3:]) if lines else f"退出码 {completed.returncode}"
    return False, detail

# 同名不同内核的仓库：下划线版是 Python 版 meme-generator 的扩展，编译不出
# 动态库，放进来只会加载失败。这里提前拦一下，省得用户下了几百 MB 才发现。
PYTHON_ONLY_REPOS = {
    ("anyliew", "meme_emoji"): "anyliew/meme-emoji",
    ("anyliew", "crazy_emoji"): "anyliew/crazy-emoji",
}


class ExternalLibraryError(RuntimeError):
    """外部表情库操作失败。"""


@dataclass(slots=True)
class InstalledLibrary:
    """一个已落盘的外部动态库。"""

    dll_name: str
    path: Path
    size: int
    repo: str = ""
    tag: str = ""
    installed_at: float = 0.0
    image_dirs: int = 0
    quarantined: bool = False


@dataclass(slots=True)
class InstallResult:
    """安装结果，用于拼回执。"""

    repo: str
    dll_name: str
    dll_bytes: int
    image_files: int
    image_bytes: int
    tag: str = ""
    note: str = ""
    asset_name: str = ""
    tip: str = ""


class ExternalLibraryManager:
    """外部表情库的安装、卸载与状态查询。"""

    def __init__(
        self,
        data_dir: Path | str,
        github_proxy: str = "",
        http_proxy: str = "",
        astrbot_github_proxy: str = "",
    ):
        self.data_dir = Path(data_dir)
        # 插件自己的前缀，优先级最高（显式配置压过继承来的）
        self.github_proxy = (github_proxy or "").strip()
        # AstrBot 设置 → 网络 → GitHub 加速地址，没填插件前缀时直接沿用
        self.astrbot_github_proxy = (astrbot_github_proxy or "").strip()
        # AstrBot 全局 http_proxy（传输层代理）优先；没有时补系统代理。
        # 内置库走 Rust reqwest 会自动读系统代理，外部库用 aiohttp 必须显式传。
        self.system_proxy = "" if http_proxy else _system_proxy()
        self.http_proxy = _normalize_proxy(http_proxy or self.system_proxy)
        self.proxy_source = (
            "AstrBot http_proxy" if http_proxy else ("系统代理" if self.system_proxy else "")
        )
        self.libraries_dir = self.data_dir / LIBRARIES_DIR_NAME
        self.resources_dir = self.data_dir / RESOURCES_DIR_NAME
        self.images_dir = self.resources_dir / "images"
        self.config_path = self.data_dir / CONFIG_FILE_NAME
        self.manifest_path = self.libraries_dir / MANIFEST_FILE_NAME

    # --- 目录与清单 ---

    def refresh_proxy(self, http_proxy: str) -> None:
        """重新读取 AstrBot/系统代理，用于配置改动后的热重载。"""
        self.system_proxy = "" if http_proxy else _system_proxy()
        self.http_proxy = _normalize_proxy(http_proxy or self.system_proxy)
        self.proxy_source = (
            "AstrBot http_proxy"
            if http_proxy
            else ("系统代理" if self.system_proxy else "")
        )

    def has_libraries(self) -> bool:
        return bool(self._dll_paths())

    def _dll_paths(self, include_disabled: bool = False) -> list[Path]:
        suffixes = {".dll", ".so", ".dylib"}
        search_dirs = [self.libraries_dir]
        if include_disabled:
            search_dirs.append(self.libraries_dir / "disabled")
        found: list[Path] = []
        for directory in search_dirs:
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            found.extend(
                path
                for path in entries
                if path.is_file() and path.suffix.lower() in suffixes
            )
        return sorted(found)

    def _load_manifest(self) -> dict:
        if not self.manifest_path.is_file():
            return {"libraries": {}}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取外部库清单失败，将重新生成: %s", exc)
            return {"libraries": {}}
        if not isinstance(data.get("libraries"), dict):
            return {"libraries": {}}
        return data

    def _save_manifest(self, data: dict) -> None:
        libraries = data.get("libraries")
        if isinstance(libraries, dict):
            # 库文件被手动删掉后，清单项会一直留着占位，顺手剔掉；
            # 被隔离到 disabled/ 的保留，方便移回来时还原素材记录。
            data["libraries"] = {
                name: record
                for name, record in libraries.items()
                if (self.libraries_dir / name).exists()
                or (self.libraries_dir / "disabled" / name).exists()
            }
        self.libraries_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.manifest_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("保存外部库清单失败: %s", exc)

    @property
    def loading_flag_path(self) -> Path:
        return self.libraries_dir / ".loading"

    def mark_loading(self) -> None:
        """加载外部库之前落一个标记，记录这次要加载哪些文件。

        动态库是原生代码，rustc 版本不匹配时进程会在加载阶段直接崩溃，
        Python 层拦不住。标记留着说明上次加载没走完，下次启动据此**只隔离
        记录里的那几个文件**——否则用户手动换掉坏库后，新装的库会被一起误伤。
        """
        names = [path.name for path in self._dll_paths()]
        try:
            self.libraries_dir.mkdir(parents=True, exist_ok=True)
            self.loading_flag_path.write_text(
                json.dumps({"names": names, "ts": time.time()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.debug("写入外部库加载标记失败: %s", exc)

    def clear_loading(self) -> None:
        try:
            self.loading_flag_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("清除外部库加载标记失败: %s", exc)

    def loading_record(self) -> list[str] | None:
        """读上次的加载标记：没有标记返回 None，有则返回记录的文件名列表。

        旧版本标记只是一串时间戳，解析不出文件名时返回空列表，由调用方
        退化成"隔离全部"。
        """
        try:
            raw = self.loading_flag_path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        if isinstance(data, dict):
            names = data.get("names")
            if isinstance(names, list):
                return [str(name) for name in names]
        return []

    def cleanup_temp_files(self) -> int:
        """清理上次中断留下的临时文件：``.part`` / ``.pending`` / ``.*.zip``。"""
        try:
            entries = list(self.libraries_dir.iterdir())
        except OSError:
            return 0
        removed = 0
        for path in entries:
            if not path.is_file():
                continue
            name = path.name
            is_temp = (
                name.endswith(".part")
                or name.endswith(".pending")
                or (name.startswith(".") and name.endswith(".zip"))
            )
            if not is_temp:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("清理临时文件失败 %s: %s", path, exc)
        if removed:
            logger.info("已清理 %d 个外部库临时文件", removed)
        return removed

    def quarantine(self, names: list[str] | None = None) -> list[str]:
        """把动态库挪进 ``libraries/disabled/``。

        ``names`` 为空时隔离全部（旧标记没有记录文件名的情况）。挪而不是删：
        文件留着方便换对版本后移回去，清单里的素材记录也不动。
        """
        if names:
            targets = [self.libraries_dir / name for name in names]
        else:
            targets = self._dll_paths()
        target_dir = self.libraries_dir / "disabled"
        moved: list[str] = []
        for path in targets:
            if not path.is_file():
                continue
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                dest = target_dir / path.name
                dest.unlink(missing_ok=True)
                path.replace(dest)
                moved.append(path.name)
            except OSError as exc:
                logger.error("隔离动态库失败 %s: %s", path, exc)
        return moved

    def scan(self, include_disabled: bool = False) -> list[InstalledLibrary]:
        """列出动态库，附带清单里的来源信息。

        ``include_disabled`` 为真时把 ``libraries/disabled/`` 里隔离的库也算上，
        状态面板和卸载需要看到它们，否则会出现"库明明还在却显示未安装"。
        """
        manifest = self._load_manifest().get("libraries", {})
        result: list[InstalledLibrary] = []
        for path in self._dll_paths(include_disabled):
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            record = manifest.get(path.name, {})
            result.append(
                InstalledLibrary(
                    dll_name=path.name,
                    path=path,
                    size=size,
                    repo=str(record.get("repo", "")),
                    tag=str(record.get("tag", "")),
                    installed_at=float(record.get("installed_at", 0) or 0),
                    image_dirs=len(record.get("image_dirs", []) or []),
                    quarantined=path.parent.name == "disabled",
                )
            )
        return result

    # --- config.toml ---

    def is_switch_enabled(self, key: str, default: bool = False) -> bool:
        """读取 config.toml 里 ``[meme]`` 段的布尔开关。"""
        if not self.config_path.is_file():
            return default
        try:
            text = self.config_path.read_text(encoding="utf-8")
        except OSError:
            return default
        section = ""
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if section != CONFIG_SECTION or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() != key:
                continue
            value = value.split("#", 1)[0].strip().lower()
            return value in ("true", "1", "yes")
        return default

    def is_external_enabled(self) -> bool:
        """没写过这一行时按"装了库就算启用"处理，与启动时的自动补齐一致。"""
        return self.is_switch_enabled(CONFIG_KEY, default=self.has_libraries())

    def is_builtin_enabled(self) -> bool:
        """没写过这一行时按 meme_generator 的默认值处理：内置库默认开。"""
        return self.is_switch_enabled(BUILTIN_KEY, default=True)

    def set_switch(self, key: str, enable: bool) -> bool:
        """把某个开关写成指定值，返回是否发生了改动。

        只就地修改 ``[meme]`` 段里的这一行，其余内容与注释保持原样；配置文件
        不存在时创建一个只含该段的最小文件。
        """
        target = "true" if enable else "false"
        text = ""
        if self.config_path.is_file():
            try:
                text = self.config_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ExternalLibraryError(f"读取 config.toml 失败: {exc}") from exc

        lines = text.splitlines()
        section = ""
        section_index: int | None = None
        key_index: int | None = None
        for index, raw_line in enumerate(lines):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                if section == CONFIG_SECTION:
                    section_index = index
                continue
            if section != CONFIG_SECTION or "=" not in line:
                continue
            if line.partition("=")[0].strip() == key:
                key_index = index
                break

        if key_index is not None:
            current = lines[key_index].partition("=")[2].split("#", 1)[0].strip()
            if current.lower() == target:
                return False
            lines[key_index] = f"{key} = {target}"
        elif section_index is not None:
            lines.insert(section_index + 1, f"{key} = {target}")
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend([f"[{CONFIG_SECTION}]", f"{key} = {target}"])

        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            raise ExternalLibraryError(f"写入 config.toml 失败: {exc}") from exc
        logger.info("已把 config.toml 的 %s 设为 %s", key, target)
        return True

    def ensure_enabled(self, enable: bool = True) -> bool:
        return self.set_switch(CONFIG_KEY, enable)

    def apply_switches(self, builtin: bool, external: bool) -> None:
        """按插件配置写两个加载开关。

        外部库只有在确实装了动态库时才置 true，避免列表里显示"已开启"却空跑。
        """
        self.set_switch(BUILTIN_KEY, builtin)
        self.set_switch(CONFIG_KEY, bool(external and self.has_libraries()))

    # --- 安装 ---

    async def install(
        self,
        spec: str,
        before_replace: Callable[[], Awaitable[None]] | None = None,
    ) -> InstallResult:
        """从 GitHub Release 安装一个外部表情库。

        ``spec`` 支持 ``owner/repo``、GitHub 链接或简写，可加 ``@1.93.1``
        指定 rustc 变体——cdylib 必须和主程序同一编译器版本才安全。

        ``before_replace`` 会在动态库正式落盘前调用；调用方用它停掉当前
        worker，释放 Windows 上已加载 DLL 的文件锁。
        """
        spec, variant = split_variant(spec)
        owner, repo = parse_library_spec(spec)
        full_name = f"{owner}/{repo}"
        note = ""
        rust_name = PYTHON_ONLY_REPOS.get((owner.lower(), repo.lower()))
        if rust_name:
            note = f"{full_name} 是 Python 版，已改用 Rust 版 {rust_name}"
            logger.warning("%s 是 Python 版扩展，改用 %s", full_name, rust_name)
            full_name = rust_name
        logger.info(
            "开始安装外部表情库 %s（%s/%s）",
            full_name,
            sys.platform,
            platform.machine(),
        )
        self.libraries_dir.mkdir(parents=True, exist_ok=True)

        async with aiohttp.ClientSession(
            timeout=_HTTP_TIMEOUT,
            headers={
                "User-Agent": "astrbot-plugin-meme-generator",
                "Accept": "application/vnd.github+json",
            },
        ) as session:
            logger.info("正在查询 %s 的 Release 资产…", full_name)
            candidates, tag, alternatives = await self._find_platform_assets(
                session, full_name, variant
            )
            logger.info(
                "已选择 Release %s，候选动态库：%s",
                tag or "?",
                "、".join(str(item.get("name") or "") for item in candidates),
            )
            chosen: dict | None = None
            chosen_source = ""
            chosen_bytes = 0
            failures: list[str] = []
            for asset in candidates:
                asset_name = str(asset.get("name") or "")
                pending = self.libraries_dir / f".{asset_name}.pending"
                logger.info("正在下载候选动态库 %s…", asset_name)
                try:
                    dll_bytes, source = await self._download(
                        session,
                        asset["browser_download_url"],
                        pending,
                        size_hint=int(asset.get("size") or 0),
                    )
                except ExternalLibraryError as exc:
                    failures.append(f"{asset_name}: {exc}")
                    pending.unlink(missing_ok=True)
                    continue
                logger.info(
                    "候选动态库 %s 下载完成（%.1f MB），正在预检…",
                    asset_name,
                    dll_bytes / 1048576,
                )
                ok, detail = await asyncio.to_thread(_probe_library, pending)
                if not ok:
                    failures.append(f"{asset_name}: {detail}")
                    pending.unlink(missing_ok=True)
                    logger.warning(
                        "候选动态库 %s 无法在当前环境加载，尝试下一个: %s",
                        asset_name,
                        detail,
                    )
                    continue
                logger.info("候选动态库 %s 预检通过", asset_name)
                chosen = asset
                chosen_source = source
                chosen_bytes = dll_bytes
                break

            if chosen is None:
                detail = "\n".join(f"- {item}" for item in failures[-5:])
                raise ExternalLibraryError(
                    f"{full_name} 没有可在当前环境加载的动态库。\n{detail}"
                )

            first_name = str(candidates[0].get("name") or "")
            chosen_name = str(chosen.get("name") or "")
            if chosen_name != first_name:
                note = (
                    f"{note}；默认候选 {first_name} 无法加载，已自动改用 {chosen_name}"
                    if note
                    else f"默认候选 {first_name} 无法加载，已自动改用 {chosen_name}"
                )

            dll_path = self.libraries_dir / chosen_name
            pending = dll_path.with_name(f".{dll_path.name}.pending")
            logger.info("动态库已就绪，开始同步 %s 的素材…", full_name)
            try:
                image_files, image_bytes, image_dirs = await self._install_resources(
                    session, full_name
                )
            except BaseException:
                # 素材没落地就别把动态库转正，否则会留下"能加载但没图"的半成品
                pending.unlink(missing_ok=True)
                raise

            if before_replace is not None:
                logger.info("停止旧 worker，准备替换动态库…")
                try:
                    await before_replace()
                except Exception as exc:
                    pending.unlink(missing_ok=True)
                    raise ExternalLibraryError(f"停止旧 worker 失败: {exc}") from exc
            try:
                pending.replace(dll_path)
                logger.info("动态库已落盘：%s", dll_path.name)
            except OSError as exc:
                pending.unlink(missing_ok=True)
                raise ExternalLibraryError(f"安装动态库失败: {exc}") from exc

        if chosen_source:
            source_note = f"经 {chosen_source} 下载"
            note = f"{note}；{source_note}" if note else source_note

        manifest = self._load_manifest()
        # 同一仓库换变体时，旧 DLL 会在 worker 下次加载时和新 DLL 重复注册；
        # 先清理掉同仓库的其它文件，并同步删除清单记录。
        repo_short = full_name.rsplit("/", 1)[-1].lower()
        removed_names: list[str] = []
        removed_dirs: set[str] = set()
        for item in self.scan():
            if item.path == dll_path:
                continue
            if (
                item.repo.lower() == full_name.lower()
                or (repo_short and repo_short in item.dll_name.lower())
            ):
                try:
                    item.path.unlink()
                    removed_names.append(item.dll_name)
                except OSError as exc:
                    logger.warning("清理旧变体 %s 失败: %s", item.dll_name, exc)
        for name in removed_names:
            old_record = manifest["libraries"].pop(name, {})
            removed_dirs.update(old_record.get("image_dirs", []) or [])

        previous = manifest["libraries"].get(dll_path.name, {})
        # 升级时新旧版本的素材目录可能不同，合并记录，卸载才能一起删干净
        merged_dirs = sorted(
            set(previous.get("image_dirs", []) or [])
            | set(image_dirs)
            | removed_dirs
        )
        manifest["libraries"][dll_path.name] = {
            "repo": full_name,
            "tag": tag,
            "installed_at": time.time(),
            "image_dirs": merged_dirs,
            "load_verified": True,
        }
        manifest["updated_at"] = time.time()
        self._save_manifest(manifest)
        self.ensure_enabled(True)
        # 新装的库重置"上次加载崩溃"状态，不然会被旧标记牵连
        self.clear_loading()

        return InstallResult(
            repo=full_name,
            dll_name=dll_path.name,
            dll_bytes=chosen_bytes,
            image_files=image_files,
            image_bytes=image_bytes,
            tag=tag,
            note=note,
            asset_name=chosen_name,
            tip=_variant_tip(full_name, variant, alternatives),
        )

    async def _find_platform_assets(
        self, session: aiohttp.ClientSession, full_name: str, variant: str = ""
    ) -> tuple[list[dict], str, list[str]]:
        """按“最新 release → 近几个 release”的顺序找可用的平台资产。"""
        releases: list[dict] = []
        latest = await self._fetch_json(
            session, f"https://api.github.com/repos/{full_name}/releases/latest"
        )
        if isinstance(latest, dict) and latest.get("tag_name"):
            releases.append(latest)

        def _first_matches() -> tuple[list[dict], dict] | None:
            for release in releases:
                assets = release.get("assets") or []
                candidates = pick_platform_assets(assets, variant)
                if candidates:
                    return candidates, release
            return None

        found = _first_matches()
        if found is None:
            logger.info(
                "最新 Release 没有匹配当前平台的资产，回退最近 10 个 Release…"
            )
            page = await self._fetch_json(
                session, f"https://api.github.com/repos/{full_name}/releases?per_page=10"
            )
            if isinstance(page, list):
                known_tags = {str(item.get("tag_name") or "") for item in releases}
                for release in page:
                    if not isinstance(release, dict):
                        continue
                    if release.get("draft") or release.get("prerelease"):
                        continue
                    tag = str(release.get("tag_name") or "")
                    if tag and tag in known_tags:
                        continue
                    releases.append(release)
                    known_tags.add(tag)
            found = _first_matches()

        if found is not None:
            candidates, release = found
            assets = release.get("assets") or []
            return (
                candidates,
                str(release.get("tag_name") or ""),
                _asset_variants(assets),
            )

        sample_release = releases[0] if releases else {}
        assets = sample_release.get("assets") or []
        names = ", ".join(str(item.get("name")) for item in assets[:6])
        scope = f"变体 {variant}" if variant else "当前平台"
        raise ExternalLibraryError(
            f"{full_name} 的 Release 里没有匹配{scope}"
            f"（{sys.platform}/{platform.machine()}）的动态库。候选：{names or '无'}"
        )

    async def _install_resources(
        self, session: aiohttp.ClientSession, full_name: str
    ) -> tuple[int, int, list[str]]:
        """安装素材：优先按文件树逐文件下载，拿不到清单再退回整包。

        内置库那套是「清单 + 逐文件 + 校验」：已有文件跳过、只补缺的。整包
        下载在 Docker 里要额外占一份几百 MB 的临时空间，重试还得从头来，所以
        这里先走逐文件；已有同尺寸文件直接跳过，失败保留已下部分，重试接着来。
        """
        logger.info("正在获取 %s 的素材清单…", full_name)
        branch = await self._default_branch(session, full_name)
        blobs = await self._fetch_resource_blobs(session, full_name, branch)
        if blobs:
            total_bytes = sum(int(item.get("size") or 0) for item in blobs)
            logger.info(
                "素材清单：%d 个文件，共 %.1f MB",
                len(blobs),
                total_bytes / 1048576,
            )
            source = await self._pick_resource_source(
                session, full_name, branch, blobs
            )
            if source is not None:
                return await self._download_blobs(
                    session, full_name, branch, blobs, source
                )
            logger.warning("逐文件下载的源都不可用，退回整包下载")
        else:
            logger.warning("没有拿到素材文件树，改为整包下载")
        return await self._install_resources_zip(session, full_name, branch)

    async def _default_branch(
        self, session: aiohttp.ClientSession, full_name: str
    ) -> str:
        info = await self._fetch_json(
            session, f"https://api.github.com/repos/{full_name}"
        )
        if isinstance(info, dict) and info.get("default_branch"):
            return str(info["default_branch"])
        return "main"

    async def _fetch_resource_blobs(
        self, session: aiohttp.ClientSession, full_name: str, branch: str
    ) -> list[dict]:
        """列出仓库里 resources/images、resources/fonts 下的所有文件。"""
        url = (
            f"https://api.github.com/repos/{full_name}/git/trees/{branch}"
            "?recursive=1"
        )
        try:
            data = await self._fetch_json(session, url)
        except ExternalLibraryError as exc:
            logger.warning("取文件树失败，退回整包下载: %s", exc)
            return []
        tree = data.get("tree") if isinstance(data, dict) else None
        if not isinstance(tree, list):
            return []

        blobs: list[dict] = []
        for item in tree:
            if not isinstance(item, dict) or item.get("type") != "blob":
                continue
            path = str(item.get("path") or "")
            position = path.find("resources/")
            if position < 0:
                continue
            relative = path[position + len("resources/"):]
            if not relative.startswith(("images/", "fonts/")):
                continue
            blobs.append(
                {"path": path, "rel": relative, "size": int(item.get("size") or 0)}
            )
        return blobs

    async def _pick_resource_source(
        self,
        session: aiohttp.ClientSession,
        full_name: str,
        branch: str,
        blobs: list[dict],
    ) -> tuple[str, str | None] | None:
        """用一个最小的文件探路，挑出能用的源，避免每个文件都试一轮。"""
        probe = min(blobs, key=lambda item: item["size"] or 1 << 30)
        raw = self._raw_url(full_name, branch, probe["path"])
        logger.info("正在探测素材下载源…")
        for label, prefix, proxy in self._candidates():
            try:
                async with session.get(
                    self._apply_prefix(raw, prefix),
                    proxy=proxy,
                    timeout=_PROBE_TIMEOUT,
                ) as response:
                    if response.status == 200:
                        logger.info("素材下载源：%s", label)
                        return prefix, proxy
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                logger.info("探测下载源 %s 失败，尝试下一个: %s", label, exc)
                continue
        return None

    async def _download_blobs(
        self,
        session: aiohttp.ClientSession,
        full_name: str,
        branch: str,
        blobs: list[dict],
        source: tuple[str, str | None],
    ) -> tuple[int, int, list[str]]:
        prefix, proxy = source
        semaphore = asyncio.Semaphore(_RESOURCE_CONCURRENCY)
        stats = {"downloaded": 0, "skipped": 0, "bytes": 0}
        failures: list[tuple[dict, str]] = []
        image_dirs: set[str] = set()
        logger.info(
            "开始下载素材：%d 个文件（已存在且大小一致的会跳过）", len(blobs)
        )

        async def fetch(blob: dict) -> None:
            relative = blob["rel"]
            dest = (self.resources_dir / relative).resolve()
            if not dest.is_relative_to(self.resources_dir.resolve()):
                logger.warning("跳过越界的素材路径: %s", blob["path"])
                return
            parts = relative.split("/")
            if len(parts) >= 3 and parts[0] == "images":
                image_dirs.add(f"images/{parts[1]}")

            expected = int(blob.get("size") or 0)
            if dest.is_file():
                try:
                    if not expected or dest.stat().st_size == expected:
                        stats["skipped"] += 1
                        return
                except OSError:
                    pass

            async with semaphore:
                try:
                    size = await self._download_blob(
                        session, full_name, branch, blob, dest, prefix, proxy
                    )
                except ExternalLibraryError as exc:
                    failures.append((blob, str(exc)))
                    if len(failures) <= 3:
                        logger.warning("素材下载失败 %s: %s", relative, exc)
                    return
            stats["downloaded"] += 1
            stats["bytes"] += size
            done = stats["downloaded"] + stats["skipped"]
            if done % 50 == 0:
                logger.info(
                    "素材进度 %d/%d（新下载 %d，跳过 %d，%.1f MB）",
                    done,
                    len(blobs),
                    stats["downloaded"],
                    stats["skipped"],
                    stats["bytes"] / 1048576,
                )

        await asyncio.gather(*(fetch(blob) for blob in blobs))

        # 单源探路只能说明那个小文件能下；网络抖动时个别文件会超时。
        # 对失败文件逐个换备用源重试，避免 3 个坏文件导致整库安装失败。
        if failures:
            for label, alt_prefix, alt_proxy in self._candidates():
                if not failures:
                    break
                if (alt_prefix, alt_proxy) == (prefix, proxy):
                    continue
                logger.info(
                    "用备用源 %s 重试 %d 个失败素材…", label, len(failures)
                )
                remaining: list[tuple[dict, str]] = []
                for blob, _error in failures:
                    relative = blob["rel"]
                    dest = (self.resources_dir / relative).resolve()
                    if not dest.is_relative_to(self.resources_dir.resolve()):
                        continue
                    try:
                        size = await self._download_blob(
                            session,
                            full_name,
                            branch,
                            blob,
                            dest,
                            alt_prefix,
                            alt_proxy,
                            retries=1,
                        )
                    except ExternalLibraryError as exc:
                        remaining.append((blob, str(exc)))
                        continue
                    stats["downloaded"] += 1
                    stats["bytes"] += size
                    logger.info("备用源 %s 已补下 %s", label, relative)
                failures = remaining

        if failures:
            preview = "；".join(
                f"{blob['rel']}: {error}" for blob, error in failures[:3]
            )
            raise ExternalLibraryError(
                f"{len(failures)} 个素材文件下载失败（{preview}）。"
                "已尝试直连和内置镜像；已下的文件会保留，重试时自动跳过。"
                "可在插件配置的 github_proxy 里填加速前缀或代理地址后重试。"
            )

        logger.info(
            "外部库 %s 素材就绪: 新下载 %d 个 / 跳过 %d 个 / %.1f MB",
            full_name,
            stats["downloaded"],
            stats["skipped"],
            stats["bytes"] / 1048576,
        )
        return stats["downloaded"], stats["bytes"], sorted(image_dirs)

    async def _download_blob(
        self,
        session: aiohttp.ClientSession,
        full_name: str,
        branch: str,
        blob: dict,
        dest: Path,
        prefix: str,
        proxy: str | None,
        retries: int = _RESOURCE_RETRIES,
    ) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        url = self._apply_prefix(
            self._raw_url(full_name, branch, blob["path"]), prefix
        )
        expected = int(blob.get("size") or 0)
        last_error = ""

        for attempt in range(max(1, retries)):
            try:
                size = 0
                async with session.get(
                    url, proxy=proxy, timeout=_RESOURCE_TIMEOUT
                ) as response:
                    response.raise_for_status()
                    with open(part, "wb") as handle:
                        async for chunk in response.content.iter_chunked(1 << 15):
                            handle.write(chunk)
                            size += len(chunk)
                if expected and size != expected:
                    raise _DownloadRetry(f"大小不符（{size}/{expected}）")
                part.replace(dest)
                return size
            except asyncio.CancelledError:
                part.unlink(missing_ok=True)
                raise
            except (
                aiohttp.ClientError,
                OSError,
                asyncio.TimeoutError,
                _DownloadRetry,
            ) as exc:
                part.unlink(missing_ok=True)
                last_error = str(exc) or exc.__class__.__name__
                if attempt + 1 < _RESOURCE_RETRIES:
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise ExternalLibraryError(last_error)

    @staticmethod
    def _raw_url(full_name: str, branch: str, path: str) -> str:
        return (
            "https://raw.githubusercontent.com/"
            f"{full_name}/{branch}/{quote(path)}"
        )

    async def _install_resources_zip(
        self, session: aiohttp.ClientSession, full_name: str, branch: str
    ) -> tuple[int, int, list[str]]:
        """整包下载兜底：一次请求拿全部素材再解压。"""
        archive_url = f"https://github.com/{full_name}/archive/refs/heads/{branch}.zip"
        self.resources_dir.mkdir(parents=True, exist_ok=True)
        zip_path = self.libraries_dir / f".{full_name.replace('/', '_')}.zip"
        logger.info("正在下载整包素材 %s…", full_name)
        try:
            await self._download(session, archive_url, zip_path)
            return await asyncio.to_thread(
                self._extract_resources, zip_path, full_name
            )
        finally:
            zip_path.unlink(missing_ok=True)

    def _extract_resources(
        self, zip_path: Path, full_name: str
    ) -> tuple[int, int, list[str]]:
        """把 zip 里 resources/images 与 resources/fonts 合并进数据目录。"""
        files = 0
        total = 0
        image_dirs: set[str] = set()
        created_dirs: set[str] = set()
        marker = "/resources/"
        try:
            with zipfile.ZipFile(zip_path) as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    normalized = member.filename.replace("\\", "/")
                    position = normalized.find(marker)
                    if position < 0:
                        continue
                    relative = normalized[position + len(marker):]
                    if not relative.startswith(("images/", "fonts/")):
                        continue
                    target = (self.resources_dir / relative).resolve()
                    if not target.is_relative_to(self.resources_dir.resolve()):
                        logger.warning("跳过越界的资源路径: %s", member.filename)
                        continue
                    parts = relative.split("/")
                    top_dir: Path | None = None
                    if len(parts) >= 3 and parts[0] == "images":
                        top_dir = self.resources_dir / parts[0] / parts[1]
                        if not top_dir.exists():
                            created_dirs.add(f"images/{parts[1]}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, open(target, "wb") as dest:
                        while True:
                            chunk = source.read(1 << 16)
                            if not chunk:
                                break
                            dest.write(chunk)
                            total += len(chunk)
                    files += 1
                    if top_dir is not None:
                        image_dirs.add(f"images/{parts[1]}")
        except (zipfile.BadZipFile, OSError) as exc:
            # 只回滚这次新建的目录：升级时原有素材不能动
            for relative in created_dirs:
                _remove_tree_safely(self.resources_dir / relative)
            raise ExternalLibraryError(f"解压素材失败: {exc}") from exc

        logger.info(
            "外部库 %s 素材落地: %d 个文件 / %.1f MB", full_name, files, total / 1048576
        )
        return files, total, sorted(image_dirs)

    async def _download(
        self,
        session: aiohttp.ClientSession,
        url: str,
        dest: Path,
        size_hint: int = 0,
    ) -> tuple[int, str]:
        """按候选源依次下载，返回 (字节数, 生效的来源描述)。

        每次尝试都会自己跟重定向：Release 资产会 302 到
        release-assets.githubusercontent.com，那个域名在部分网络下连不通，
        所以失败后要能换下一个源重来，而不是把异常直接抛给命令层。
        """
        last_error = "未知错误"
        size_text = (
            f"{size_hint / 1048576:.1f} MB" if size_hint else "大小未知"
        )
        for label, prefix, proxy in self._candidates():
            for attempt in range(_ATTEMPTS_PER_SOURCE):
                if attempt == 0:
                    logger.info(
                        "开始下载 %s（%s，源：%s）",
                        _display_name(dest),
                        size_text,
                        label,
                    )
                try:
                    total = await self._download_once(
                        session, url, dest, prefix, proxy, size_hint
                    )
                    logger.info(
                        "下载 %s 成功（%.1f MB，%s）",
                        _display_name(dest),
                        total / 1048576,
                        label,
                    )
                    return total, "" if label == _DIRECT_LABEL else label
                except _DownloadRetry as exc:
                    last_error = str(exc)
                    logger.warning(
                        "下载 %s 失败（%s，第 %d 次）: %s",
                        _display_name(dest),
                        label,
                        attempt + 1,
                        exc,
                    )
                    if attempt + 1 < _ATTEMPTS_PER_SOURCE:
                        await asyncio.sleep(1.5 * (attempt + 1))
        raise ExternalLibraryError(
            f"下载 {url} 失败: {last_error}\n"
            "插件会自动使用 AstrBot 的 http_proxy、系统代理和内置镜像；"
            "也可以在插件配置的 github_proxy 里填加速前缀或代理地址后重试。"
        )

    async def _download_once(
        self,
        session: aiohttp.ClientSession,
        url: str,
        dest: Path,
        prefix: str,
        proxy: str | None,
        size_hint: int,
    ) -> int:
        part = dest.with_name(dest.name + ".part")
        current = url
        total = 0
        reported = 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            for _ in range(_MAX_REDIRECTS):
                request_url = self._apply_prefix(current, prefix)
                async with session.get(
                    request_url, allow_redirects=False, proxy=proxy
                ) as response:
                    if response.status in _REDIRECT_STATUS:
                        location = response.headers.get("Location")
                        if not location:
                            raise _DownloadRetry(
                                f"HTTP {response.status} 缺少 Location"
                            )
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    with open(part, "wb") as handle:
                        async for chunk in response.content.iter_chunked(1 << 16):
                            handle.write(chunk)
                            total += len(chunk)
                            if total - reported >= 5 * 1048576:
                                reported = total
                                logger.info(
                                    "下载 %s: %.1f MB",
                                    _display_name(dest),
                                    total / 1048576,
                                )
                    break
            else:
                raise _DownloadRetry("重定向次数过多")
        except asyncio.CancelledError:
            part.unlink(missing_ok=True)
            raise
        except (aiohttp.ClientError, OSError, ValueError, asyncio.TimeoutError) as exc:
            part.unlink(missing_ok=True)
            raise _DownloadRetry(str(exc) or exc.__class__.__name__) from exc

        if size_hint and total != size_hint:
            part.unlink(missing_ok=True)
            raise _DownloadRetry(f"文件不完整（{total}/{size_hint} 字节）")
        part.replace(dest)
        return total

    async def _fetch_json(self, session: aiohttp.ClientSession, url: str):
        """按同一套候选源取 JSON，404 直接返回 None。"""
        last_error = ""
        for label, prefix, proxy in self._candidates(for_api=True):
            target = self._apply_prefix(url, prefix)
            try:
                async with session.get(
                    target, proxy=proxy, timeout=_API_TIMEOUT
                ) as response:
                    if response.status == 404:
                        return None
                    response.raise_for_status()
                    return await response.json(content_type=None)
            except (aiohttp.ClientError, OSError, ValueError) as exc:
                last_error = str(exc)
                logger.info("请求 %s 失败（%s），尝试下一个源: %s", target, label, exc)
        raise ExternalLibraryError(
            f"访问 GitHub API 失败（{last_error}）。"
            "插件会自动使用 AstrBot 的 http_proxy、系统代理和内置镜像；"
            "也可以在插件配置的 github_proxy 里填加速前缀或代理地址后重试。"
        )

    def proxy_label(self) -> str:
        """当前下载途径的可读描述，用于日志和 /表情包库 列表。"""
        parts: list[str] = []
        if self.github_proxy:
            parts.append(f"插件配置 {self.github_proxy}")
        if self.astrbot_github_proxy:
            parts.append(f"AstrBot GitHub 加速 {self.astrbot_github_proxy}")
        if self.http_proxy:
            parts.append(f"{self.proxy_source or 'http_proxy'} {self.http_proxy}")
        return " → ".join(parts) if parts else "未配置（直连）"

    def _candidates(self, for_api: bool = False) -> list[tuple[str, str, str | None]]:
        """候选来源。

        下载（图片/动态库）：插件前缀/加速地址 → 传输层代理 → 直连 → 内置镜像。
        前缀候选会同时尝试“走代理”和“直连”，因为用户可能只开了系统代理，
        而加速服务本身也需要代理才能访问。API 请求则代理/直连优先。
        """
        configured = [p for p in re.split(r"[,\s]+", self.github_proxy) if p]
        prefix_values: list[str] = []
        proxy_values: list[str] = []
        for entry in configured:
            if _looks_like_proxy(entry):
                proxy_values.append(_normalize_proxy(entry))
            else:
                prefix_values.append(entry)
        if self.astrbot_github_proxy:
            if _looks_like_proxy(self.astrbot_github_proxy):
                proxy_values.append(_normalize_proxy(self.astrbot_github_proxy))
            else:
                prefix_values.append(self.astrbot_github_proxy)
        if self.http_proxy:
            proxy_values.append(self.http_proxy)

        proxies: list[str] = []
        for proxy in proxy_values:
            if not proxy or proxy in proxies:
                continue
            if proxy.lower().startswith("socks"):
                logger.warning(
                    "aiohttp 不支持 SOCKS 代理，已跳过 %s；请改用 HTTP 代理", proxy
                )
                continue
            proxies.append(proxy)

        prefixes: list[tuple[str, str, str | None]] = []
        for prefix in dict.fromkeys(prefix_values):
            if not prefix:
                continue
            for proxy in proxies[:1]:
                prefixes.append((f"前缀 {prefix}（走代理）", prefix, proxy))
            prefixes.append((f"前缀 {prefix}", prefix, None))

        transport: list[tuple[str, str, str | None]] = [
            (f"代理 {proxy}", "", proxy) for proxy in proxies
        ]
        direct = [(_DIRECT_LABEL, "", None)]

        taken = {item.rstrip("/") for item in prefix_values}
        mirrors: list[tuple[str, str, str | None]] = []
        for prefix in DEFAULT_MIRRORS:
            if prefix.rstrip("/") in taken:
                continue
            for proxy in proxies[:1]:
                mirrors.append((f"镜像 {prefix}（走代理）", prefix, proxy))
            mirrors.append((f"镜像 {prefix}", prefix, None))

        if for_api:
            return transport + direct + prefixes + mirrors
        return prefixes + transport + direct + mirrors

    @staticmethod
    def _apply_prefix(url: str, prefix: str) -> str:
        if not prefix:
            return url
        base = prefix if prefix.endswith("/") else f"{prefix}/"
        if url.startswith(base):
            return url
        return f"{base}{url}"

    # --- 卸载 ---

    def restore(self, name: str) -> str:
        """把 ``libraries/disabled/`` 里隔离的库移回 ``libraries/``。"""
        quarantined = [
            item for item in self.scan(include_disabled=True) if item.quarantined
        ]
        target = _match_library(quarantined, name)
        if target is None:
            raise ExternalLibraryError(f"没有找到被隔离的库: {name}")
        dest = self.libraries_dir / target.dll_name
        try:
            target.path.replace(dest)
        except OSError as exc:
            raise ExternalLibraryError(f"移回动态库失败: {exc}") from exc
        self.clear_loading()
        logger.info("已把隔离的 %s 移回 libraries/", target.dll_name)
        return target.dll_name

    def uninstall(self, name: str) -> tuple[InstalledLibrary, int, int]:
        """删除指定库的动态库与它带进来的素材。"""
        libraries = self.scan(include_disabled=True)
        target = _match_library(libraries, name)
        if target is None:
            raise ExternalLibraryError(f"没有找到外部库: {name}")

        manifest = self._load_manifest()
        record = manifest["libraries"].pop(target.dll_name, {})
        removed_files = 0
        freed = 0
        for relative in record.get("image_dirs", []) or []:
            directory = (self.resources_dir / relative).resolve()
            if not directory.is_relative_to(self.images_dir.resolve()):
                logger.warning("跳过越界的素材目录: %s", relative)
                continue
            if not directory.is_dir():
                continue
            for path in directory.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    freed += path.stat().st_size
                    path.unlink()
                    removed_files += 1
                except OSError as exc:
                    logger.warning("删除素材失败 %s: %s", path, exc)
            _remove_empty_tree(directory)

        try:
            target.path.unlink()
        except OSError as exc:
            raise ExternalLibraryError(f"删除动态库失败: {exc}") from exc

        manifest["updated_at"] = time.time()
        self._save_manifest(manifest)
        self.clear_loading()

        if not self.has_libraries():
            self.ensure_enabled(False)
        return target, removed_files, freed

    # --- 展示 ---

    def library_summary(self) -> list[dict[str, object]]:
        """给状态面板用的库清单：名字、模板数、启用状态、补充说明。"""
        libraries = self.scan(include_disabled=True)
        builtin_on = self.is_builtin_enabled()
        external_on = self.is_external_enabled()
        shown: set[str] = set()
        rows: list[dict[str, object]] = []
        for entry in LIBRARY_CATALOG:
            if entry.get("builtin"):
                rows.append(
                    {
                        "name": entry["name"],
                        "count": entry["memes"],
                        "state": "启用" if builtin_on else "关闭",
                        "detail": "常驻，不可卸载",
                    }
                )
                continue
            item = _match_repo(libraries, entry["repo"])
            if item is None:
                rows.append(
                    {
                        "name": entry["name"],
                        "count": entry["memes"],
                        "state": "未安装",
                        "detail": entry["note"],
                    }
                )
                continue
            shown.add(item.dll_name)
            detail = _format_size(item.size)
            if item.image_dirs:
                detail += f" · 素材 {item.image_dirs} 组"
            if item.quarantined:
                rows.append(
                    {
                        "name": entry["name"],
                        "count": entry["memes"],
                        "state": "已隔离",
                        "detail": "在 libraries/disabled/，移回后重载",
                    }
                )
                continue
            rows.append(
                {
                    "name": entry["name"],
                    "count": entry["memes"],
                    "state": "启用" if external_on else "关闭",
                    "detail": detail,
                }
            )
        for item in libraries:
            if item.dll_name in shown:
                continue
            rows.append(
                {
                    "name": item.repo or item.dll_name,
                    "count": 0,
                    "state": "启用" if external_on else "关闭",
                    "detail": _format_size(item.size),
                }
            )
        return rows

    def format_status(self, builtin_ready: bool = True) -> str:
        """库清单：内置常驻，meme-emoji 常显，其他已装库附在后面。

        ``builtin_ready`` 取资源检查结果——内置模板的图片要靠 CDN 下下来，
        还没下完时不能写"已下载"。
        """
        libraries = self.scan(include_disabled=True)
        builtin_on = self.is_builtin_enabled()
        external_on = self.is_external_enabled()
        shown: set[str] = set()
        lines = ["#当前列表"]

        for entry in LIBRARY_CATALOG:
            if entry.get("builtin"):
                state = "已下载" if builtin_ready else "待下载"
                lines.append(
                    f"- 内置：{entry['memes']} 个模板 · {state} · "
                    f"{_on_off(builtin_on)}"
                )
                continue
            item = _match_repo(libraries, entry["repo"])
            if item is None:
                lines.append(
                    f"- {entry['name']}：{entry['memes']} 个模板 · 未安装"
                )
                continue
            shown.add(item.dll_name)
            version = _short_version(item.tag)
            prefix = f"（{version}）" if version else ""
            if item.quarantined:
                lines.append(
                    f"- {entry['name']}：{prefix}{entry['memes']} 个模板 · "
                    "已隔离（在 libraries/disabled/，移回后重载）"
                )
                continue
            lines.append(
                f"- {entry['name']}：{prefix}{entry['memes']} 个模板 · "
                f"已安装 · {_on_off(external_on)}"
            )

        for item in libraries:
            if item.dll_name in shown:
                continue
            label = item.repo or item.dll_name
            version = _short_version(item.tag)
            prefix = f"（{version}）" if version else ""
            lines.append(
                f"- {label}：{prefix}已安装 · {_on_off(external_on)}"
            )

        lines.append("")
        lines.append(f"下载代理：{self.proxy_label()}")
        if not builtin_on or (libraries and not external_on):
            lines.append("")
            lines.append(
                "开关在配置的 enable_builtin_library / "
                "enable_external_libraries，改完执行 /表情包库 重载"
            )
        lines.append("")
        lines.append(
            "用法：/表情包库 安装 [库] ｜ /表情包库 卸载 <库>"
            " ｜ /表情包库 恢复 <库> ｜ /表情包库 重载"
        )
        return "\n".join(lines)


def _asset_variants(assets: list[dict]) -> list[str]:
    """从资产名里收集 rustc 版本，按数值升序返回，例如 ['1.93.1', '1.100.0']。"""
    found = set()
    pattern = re.compile(r"-(\d+\.\d+\.\d+)\.(?:dll|so|dylib)$")
    for asset in assets:
        match = pattern.search(str(asset.get("name") or "").lower())
        if match:
            found.add(match.group(1))
    return sorted(found, key=lambda v: tuple(int(p) for p in v.split(".")))


def _variant_tip(repo: str, variant: str, alternatives: list[str]) -> str:
    """给用户一条"版本不对怎么办"的指令。"""
    if variant or not alternatives:
        return ""
    options = " / ".join(alternatives)
    return (
        "安装时已逐个下载并预检 DLL；若外部库仍加载失败，可手动指定 rustc 变体："
        f"/表情包库 安装 {repo}@<版本>，可选 {options}"
    )


def split_variant(spec: str) -> tuple[str, str]:
    """把 ``owner/repo@1.93.1`` 拆成 (地址, rustc 变体)。"""
    text = (spec or "").strip()
    if "@" not in text:
        return text, ""
    head, _, tail = text.rpartition("@")
    if not head or "/" in tail:
        return text, ""
    return head.strip(), tail.strip()


def parse_library_spec(spec: str) -> tuple[str, str]:
    """把 owner/repo、GitHub 链接或简写解析成 (owner, repo)。"""
    text = (spec or "").strip()
    if not text:
        raise ExternalLibraryError("请给出库地址，例如 anyliew/meme-emoji")
    text = text.split("?", 1)[0].rstrip("/")
    if text in KNOWN_LIBRARIES:
        text = KNOWN_LIBRARIES[text]
    elif "github.com/" in text:
        text = text.split("github.com/", 1)[1]
        text = text.removesuffix(".git")
        parts = text.split("/")
        if len(parts) < 2:
            raise ExternalLibraryError(f"无法解析地址: {spec}")
        text = "/".join(parts[:2])
    if not _REPO_PATTERN.match(text):
        raise ExternalLibraryError(f"无法解析地址: {spec}")
    owner, repo = text.split("/", 1)
    return owner, repo


def pick_platform_assets(assets: list[dict], variant: str = "") -> list[dict]:
    """在 Release 资产里挑出当前平台的动态库，按优先顺序返回。

    外部库用 cdylib 加载，必须和主程序同一个 rustc 版本才安全。这里不再
    盲选 stable：带具体 rustc 版本的资产排在前面（版本号高的优先），stable
    作为兜底。调用方会逐个下载并在子进程里预检，第一个能加载的胜出。
    """
    suffix, platform_tokens, arch_tokens = _platform_signature()
    needle = variant.strip().lower()
    matches: list[tuple[int, re.Match | None, dict]] = []
    pattern = re.compile(r"-(\d+\.\d+\.\d+)\.(?:dll|so|dylib)$")

    for asset in assets:
        name = str(asset.get("name") or "")
        lowered = name.lower()
        if not lowered.endswith(suffix):
            continue
        if not any(token in lowered for token in platform_tokens):
            continue
        has_arch = not arch_tokens or any(
            token in lowered for token in arch_tokens
        )
        if not has_arch:
            continue
        score = 2 if arch_tokens else 0
        version = pattern.search(lowered)
        if needle:
            if needle not in lowered:
                continue
            score += 5
        elif not version and "stable" in lowered:
            score += 1
        if not version and not needle and "stable" not in lowered:
            continue
        matches.append((score, version, asset))

    def sort_key(item: tuple[int, re.Match | None, dict]):
        score, version, _asset = item
        if version:
            parts = tuple(int(part) for part in version.group(1).split("."))
            return (0, tuple(-part for part in parts), -score)
        return (1, (), -score)

    matches.sort(key=sort_key)
    return [asset for _score, _version, asset in matches]


def _platform_signature() -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    machine = (platform.machine() or "").lower()
    if machine in ("amd64", "x86_64", "x64"):
        arch = ("x86_64", "amd64", "x64")
    elif machine in ("arm64", "aarch64"):
        arch = ("aarch64", "arm64")
    else:
        arch = ()

    if sys.platform == "win32":
        return ".dll", ("windows", "win64", "win-"), arch
    if sys.platform == "darwin":
        return ".dylib", ("macos", "darwin", "osx"), arch
    return ".so", ("linux",), arch


def _match_repo(
    libraries: list[InstalledLibrary], repo: str
) -> InstalledLibrary | None:
    """按 owner/repo 找已装库；手动放的 dll 按文件名里的库名兜底匹配。"""
    needle = repo.lower()
    short = needle.split("/")[-1]
    for item in libraries:
        if item.repo.lower() == needle:
            return item
    for item in libraries:
        if short and short in item.dll_name.lower():
            return item
    return None


def _match_library(libraries: list[InstalledLibrary], name: str) -> InstalledLibrary | None:
    needle = (name or "").strip().lower()
    if not needle:
        return None
    for item in libraries:
        keys = {
            item.dll_name.lower(),
            item.dll_name.rsplit(".", 1)[0].lower(),
            item.repo.lower(),
            item.repo.split("/")[-1].lower() if item.repo else "",
        }
        if needle in keys:
            return item
    for item in libraries:
        if needle in item.dll_name.lower() or (
            item.repo and needle in item.repo.lower()
        ):
            return item
    return None


def _on_off(enabled: bool) -> str:
    return "已启用" if enabled else "已关闭"


def _short_version(tag: str) -> str:
    """v0.0.6+build.71 → v0.0.6，列表里只留版本号。"""
    return (tag or "").split("+", 1)[0].strip()


def _display_name(path: Path) -> str:
    """临时文件名不该出现在日志里：.xxx.pending → xxx。"""
    name = path.name
    if name.startswith("."):
        name = name[1:]
    return name.removesuffix(".pending")


def _remove_tree_safely(root: Path) -> None:
    """删掉某个素材目录；只用于回滚本次安装新建的内容。"""
    try:
        shutil.rmtree(root)
    except OSError as exc:
        logger.warning("回滚素材目录失败 %s: %s", root, exc)


def _format_size(size: int) -> str:
    value = float(max(size, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _remove_empty_tree(root: Path) -> None:
    """自底向上删掉空目录，失败就保留。"""
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass
