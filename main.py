import asyncio
import json
import os
import re
import yaml
from pathlib import Path
import astrbot.core.message.components as Comp
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from astrbot.core.platform import AstrMessageEvent
from astrbot.core.star.filter.event_message_type import EventMessageType
from astrbot.api.star import StarTools

# 原生模板全部在独立 worker 子进程里加载，主进程只负责传 MEME_HOME。
# StarTools 提供 AstrBot 的跨平台插件数据目录；这里显式传插件名，因为
# AstrBot 还没注册本模块的 metadata。
PLUGIN_NAME = "astrbot_plugin_meme_generator"
MEME_HOME = Path(StarTools.get_data_dir(PLUGIN_NAME))
os.environ["MEME_HOME"] = str(MEME_HOME)
logger.info("表情包资源目录: %s", MEME_HOME)

from .core import gif_toolbox
from .core.external_library import (
    DEFAULT_LIBRARY,
    ExternalLibraryError,
    ExternalLibraryManager,
)
from .core.gif_toolbox import GifToolboxError
from .core.meme_manager import MemeManager
from .core.meme_manager import ResourceNotReadyError
from .utils import media_source
from .utils.media_source import MediaError
from .utils.permission_utils import PermissionUtils
from .utils.render_fallback import (
    format_help_menu_text,
    format_plugin_status_text,
    format_template_list_text,
    format_template_text_pages,
    render_with_fallback,
)


PLUGIN_DIR = Path(__file__).parent
STATIC_DIR = PLUGIN_DIR / "static"
STATIC_HTML_DIR = STATIC_DIR / "html"
STATIC_DATA_DIR = STATIC_DIR / "data"

# 模板数超过这个量级就不要再渲染卡片图：上千个模板拼成一张竖图，
# 字会小到看不清，渲染也慢。auto 模式下超过就走纯文本分页。
_LIST_IMAGE_LIMIT = 200
_LIST_TEXT_WORDS = {"文字", "文本", "text", "txt"}
_LIST_IMAGE_WORDS = {"图片", "卡片", "图", "image", "img", "card"}

# GIF 工具箱的意图表。AstrBot 的 CommandFilter 要求 event.is_at_or_wake_command
# （必须带唤醒前缀或 @机器人），而 RegexFilter 不受 wake_prefix 制约，所以这批指令
# 全部走正则：裸发「加速5」也能触发，带前缀同样命中。
#
# 每个意图只留一个规范词：口语变体（快进、慢放、拆帧、电子包浆…）不再收进来，
# 否则群里随口说一句「这视频快进了」就会被当成指令。列表顺序即优先级。
_TOOLBOX_INTENTS = [
    (r"视频\s*转\s*(?:成|为)?\s*(?:gif|动图)", "video"),
    (r"线稿", "lineart"),
    (r"做旧", "age"),
    (r"合成", "concat"),
    (r"分解", "decompose"),
    (r"倒放", "reverse"),
    (r"裁剪", "crop"),
    (r"变速", "speed"),
    (r"(?:\d+(?:\.\d+)?\s*倍)?\s*加速", "accelerate"),
    (r"(?:\d+(?:\.\d+)?\s*倍)?\s*减速", "decelerate"),
]

# 显式指令：开头就是规范词。是否「显式」由 _is_explicit_command 判断——群聊里
# 走了唤醒前缀或 @机器人，私聊里带了前缀也算。语义关闭时只认这种写法。
_STRICT_PREFIX = r"^\s*(?:[/#>›]\s*)?"
# 自然语言：前缀、@、填充词都可以省略，只要开头是规范词就触发。
_SEMANTIC_PREFIX = (
    r"^\s*(?:[/#>›]\s*)?(?:@\S+\s*)?"
    r"(?:请|帮我|帮忙|麻烦)?\s*(?:把|将)?\s*(?:这个|这张|那张)?\s*"
)

_TOOLBOX_INTENTS_BODY = "|".join(body for body, _ in _TOOLBOX_INTENTS)
_COMMAND_PATTERNS = [
    (re.compile(_STRICT_PREFIX + body, re.IGNORECASE), intent)
    for body, intent in _TOOLBOX_INTENTS
]
_SEMANTIC_PATTERNS = [
    (re.compile(_SEMANTIC_PREFIX + body, re.IGNORECASE), intent)
    for body, intent in _TOOLBOX_INTENTS
]
# 过滤器只负责把消息拦进分发器，具体走指令还是自然语言由 _match_toolbox_intent 判断。
_TOOLBOX_ANY = re.compile(
    _SEMANTIC_PREFIX + "(?:" + _TOOLBOX_INTENTS_BODY + ")",
    re.IGNORECASE,
)


def _is_explicit_command(event: AstrMessageEvent) -> bool:
    """这次触发是否走的是唤醒路径（唤醒前缀或 @机器人）。

    AstrBot 在唤醒阶段就把唤醒前缀从 message_str 里剥掉了，不能再靠它判断：
    群聊看 is_at_or_wake_command；私聊默认全部唤醒，这个标记恒为 True，只能回
    消息链里的原始文本确认有没有前缀。
    """
    if not event.is_private_chat():
        return bool(event.is_at_or_wake_command)

    self_id = str(event.get_self_id() or "").strip()
    for component in event.get_messages():
        if isinstance(component, Comp.At):
            if str(getattr(component, "qq", "") or "").strip() == self_id:
                return True
            continue
        if isinstance(component, Comp.Plain):
            return bool(re.match(r"^\s*[/#>›]", component.text or ""))
    return False


def _match_toolbox_intent(
    message: str, semantic_enabled: bool = True, explicit: bool = False
) -> tuple[str, bool] | None:
    """返回 (意图, 是否自然语言触发)；没命中返回 None。

    显式指令永远优先。语义关闭后只认显式指令，裸发的短词交还给表情生成流程，
    避免「合成」「分解」这类词在闲聊里被工具箱抢走。
    """
    text = message or ""
    if explicit:
        for pattern, intent in _COMMAND_PATTERNS:
            if pattern.search(text):
                return intent, False
    if not semantic_enabled:
        return None
    for pattern, intent in _SEMANTIC_PATTERNS:
        if pattern.search(text):
            return intent, True
    return None


def _first_number(text: str, default: float) -> float:
    match = re.search(r"\d+(?:\.\d+)?", text or "")
    return float(match.group(0)) if match else default


def _parse_frame_duration(text: str, default: float = 0.5) -> float:
    """解析表情合成的静态图帧时长，支持 `10fps` 与 `0.2` 两种写法。"""
    fps = re.search(r"(\d+(?:\.\d+)?)\s*(?:fps|帧)", text or "", re.IGNORECASE)
    if fps and float(fps.group(1)) > 0:
        return 1.0 / float(fps.group(1))
    seconds = re.search(r"\d+(?:\.\d+)?", text or "")
    if seconds and 0.01 <= float(seconds.group(0)) <= 60:
        return float(seconds.group(0))
    return default


def _load_static_template(template_name: str) -> str | None:
    template_path = STATIC_HTML_DIR / template_name
    if not template_path.exists() or not template_path.is_file():
        return None
    try:
        content = template_path.read_text(encoding="utf-8")
    except Exception:
        return None

    css_map = {
        "../css/meme_ui.css": STATIC_DIR / "css" / "meme_ui.css",
    }
    for relative_path, css_path in css_map.items():
        if relative_path not in content or not css_path.exists():
            continue
        try:
            css_content = css_path.read_text(encoding="utf-8")
        except Exception:
            continue
        css_link = f'<link rel="stylesheet" href="{relative_path}">'
        content = content.replace(css_link, f"<style>\n{css_content}\n</style>")
    return content


def _load_static_data(data_file_name: str) -> dict[str, object] | None:
    data_path = STATIC_DATA_DIR / data_file_name
    if not data_path.exists() or not data_path.is_file():
        return None
    try:
        return json.loads(data_path.read_text(encoding="utf-8"))
    except Exception:
        return None




def _format_bytes(size: int) -> str:
    """把字节数写成好读的单位。"""
    value = float(max(size, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _split_command_args(message_str: str, names: set[str]) -> list[str]:
    """把「/表情包库 安装 xxx」拆成参数列表。

    AstrBot 的命令过滤器只把消息原文交回来，命令词是否带唤醒前缀、是否被
    别名替换都不确定，这里统一剥掉开头的斜杠和命令词再切分。
    """
    text = (message_str or "").strip()
    while text.startswith("/"):
        text = text[1:].lstrip()
    tokens = text.split()
    if tokens and tokens[0] in names:
        tokens = tokens[1:]
    return tokens


_GITHUB_PROXY_KEYS = (
    "github_proxy",
    "githubProxy",
    "github_mirror",
    "githubMirror",
    "github_accelerate",
    "github_acceleration",
)


def _deep_find_proxy(node, depth: int = 0) -> str:
    """在配置树里递归找 GitHub 加速地址。

    WebUI「设置 → 网络 → GitHub 加速地址」的字段名随版本变过，位置也可能被
    收进分组里，所以按键名递归找，取第一个非空字符串。
    """
    if depth > 6:
        return ""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key in _GITHUB_PROXY_KEYS:
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, dict):
                    for sub in ("url", "value", "proxy", "address"):
                        candidate = value.get(sub)
                        if isinstance(candidate, str) and candidate.strip():
                            return candidate.strip()
        for value in node.values():
            found = _deep_find_proxy(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _deep_find_proxy(item, depth + 1)
            if found:
                return found
    return ""


def _astrbot_github_proxy() -> str:
    """读 AstrBot 设置里的 GitHub 加速地址，没有就返回空。"""
    try:
        from astrbot.core import astrbot_config

        value = _deep_find_proxy(astrbot_config)
    except Exception:
        return ""
    if value:
        logger.debug("AstrBot GitHub 加速地址：%s", value)
    return value


def _astrbot_http_proxy() -> str:
    """取 AstrBot 全局 http_proxy，取不到再退回环境变量。

    插件的下载不走 AstrBot 的网络栈，代理得显式读出来传下去，否则用户在
    AstrBot 里配好的代理对装库不生效。系统代理由 ExternalLibraryManager
    再用 urllib.request.getproxies() 兜底。
    """
    try:
        from astrbot.core import astrbot_config

        value = str(astrbot_config.get("http_proxy", "") or "").strip()
        logger.debug("AstrBot 全局 http_proxy：%r", value)
        if value:
            return value
    except Exception:
        pass
    for key in (
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        value = os.environ.get(key, "").strip()
        if value:
            logger.debug("使用环境变量代理 %s=%s", key, value)
            return value
    return ""


# 配置分组，和 _conf_schema.json 里的 object 分组保持一致。旧版配置是平铺的，
# _read 先读分组再回退平铺；_migrate_flat_config 负责把旧值搬进分组。
CONFIG_GROUPS = {
    "basic": (
        "enable_plugin",
        "trigger_prefix",
        "cooldown_seconds",
        "generation_timeout",
        "enable_avatar_cache",
        "cache_expire_hours",
        "list_style",
    ),
    "libraries": (
        "enable_builtin_library",
        "enable_external_libraries",
        "disabled_templates",
        "github_proxy",
    ),
    "poke": ("enable_poke_response", "poke_response_any_target"),
    "gif": (
        "gif_enable_toolbox",
        "gif_enable_semantic",
        "gif_output_format",
        "gif_default_scale",
        "gif_default_fps",
        "gif_max_duration",
        "gif_max_video_size_mb",
        "gif_max_colors",
    ),
}
_FIELD_GROUP = {
    name: group for group, names in CONFIG_GROUPS.items() for name in names
}
_CONFIG_VERSION = 2


class MemeConfig:
    """表情包生成器配置管理类"""

    def __init__(self, config: AstrBotConfig):
        self.config = config
        self._migrate_flat_config()
        self._load_config()

    def _migrate_flat_config(self):
        """把旧版平铺配置搬进分组，写过标记后不再重复。

        不迁移的话，AstrBot 会按新 schema 给分组补默认值，用户之前调过的
        冷却、前缀这些会被默认值顶掉。
        """
        if self.config.get("_schema_version") == _CONFIG_VERSION:
            return

        moved = False
        for key, group in _FIELD_GROUP.items():
            if key not in self.config:
                continue
            section = self.config.get(group)
            if not isinstance(section, dict):
                section = {}
                self.config[group] = section
            section[key] = self.config[key]
            moved = True
        self.config["_schema_version"] = _CONFIG_VERSION
        if moved:
            logger.info("已把旧版平铺配置迁移到 %d 个分组", len(CONFIG_GROUPS))
        try:
            self.config.save_config()
        except Exception as exc:
            logger.warning("保存迁移后的配置失败: %s", exc)

    def _read(self, key: str, default):
        """先读分组内的值，读不到再回退旧版平铺键。"""
        group = _FIELD_GROUP.get(key)
        if group:
            section = self.config.get(group)
            if isinstance(section, dict) and key in section:
                return section[key]
        return self.config.get(key, default)

    def _load_config(self):
        """加载配置"""
        self.enable_plugin: bool = self._read("enable_plugin", True)
        self.trigger_prefix: str = str(self._read("trigger_prefix", "") or "")
        self.generation_timeout: int = self._read("generation_timeout", 30)
        self.cooldown_seconds: int = self._read("cooldown_seconds", 3)
        self.enable_avatar_cache: bool = self._read("enable_avatar_cache", True)
        self.cache_expire_hours: int = self._read("cache_expire_hours", 24)
        self.disabled_templates: list[str] = self._read("disabled_templates", [])
        self.enable_poke_response: bool = self._read(
            "enable_poke_response", True
        )
        self.poke_response_any_target: bool = self._read(
            "poke_response_any_target", False
        )
        self.gif_enable_toolbox: bool = self._read("gif_enable_toolbox", True)
        self.gif_enable_semantic: bool = self._read("gif_enable_semantic", True)
        self.gif_output_format: str = gif_toolbox.normalize_format(
            str(self._read("gif_output_format", "GIF"))
        )
        self.gif_default_scale: float = float(self._read("gif_default_scale", 0.3))
        self.gif_default_fps: int = int(self._read("gif_default_fps", 10))
        self.gif_max_duration: float = float(self._read("gif_max_duration", 10.0))
        self.gif_max_video_size_mb: float = float(
            self._read("gif_max_video_size_mb", 50.0)
        )
        self.gif_max_colors: int = int(self._read("gif_max_colors", 256))
        self.github_proxy: str = str(self._read("github_proxy", "") or "")
        list_style = str(self._read("list_style", "auto") or "auto").lower()
        self.list_style: str = (
            list_style if list_style in ("auto", "text", "image") else "auto"
        )
        self.enable_builtin_library: bool = bool(
            self._read("enable_builtin_library", True)
        )
        self.enable_external_libraries: bool = bool(
            self._read("enable_external_libraries", True)
        )

    def gif_video_options(self) -> gif_toolbox.VideoOptions:
        """视频转动图所需的参数集合。"""
        return gif_toolbox.VideoOptions(
            output_format=self.gif_output_format,
            default_scale=self.gif_default_scale,
            default_fps=self.gif_default_fps,
            max_duration=self.gif_max_duration,
            max_colors=self.gif_max_colors,
        )

    def _save_specific_config(self, key: str, value):
        """保存单个配置项，按分组写回去。"""
        group = _FIELD_GROUP.get(key)
        if group:
            section = self.config.get(group)
            if not isinstance(section, dict):
                section = {}
                self.config[group] = section
            section[key] = value
        else:
            self.config[key] = value
        self.config.save_config()

    def is_template_disabled(self, template_name: str) -> bool:
        return template_name in self.disabled_templates

    def disable_template(self, template_name: str) -> bool:
        if template_name not in self.disabled_templates:
            self.disabled_templates.append(template_name)
            self._save_specific_config("disabled_templates", self.disabled_templates)
            return True
        return False

    def enable_template(self, template_name: str) -> bool:
        if template_name in self.disabled_templates:
            self.disabled_templates.remove(template_name)
            self._save_specific_config("disabled_templates", self.disabled_templates)
            return True
        return False

    def get_disabled_templates(self) -> list[str]:
        return self.disabled_templates.copy()

    def enable_plugin_func(self) -> bool:
        if not self.enable_plugin:
            self.enable_plugin = True
            self._save_specific_config("enable_plugin", True)
            return True
        return False

    def disable_plugin_func(self) -> bool:
        if self.enable_plugin:
            self.enable_plugin = False
            self._save_specific_config("enable_plugin", False)
            return True
        return False

    def is_plugin_enabled(self) -> bool:
        return self.enable_plugin


class TemplateHandlers:
    """模板相关命令处理器"""

    def __init__(self, meme_manager: MemeManager, config: MemeConfig):
        self.meme_manager = meme_manager
        self.config = config

    async def handle_template_info(
        self,
        event: AstrMessageEvent,
        keyword: str | int | None = None,
    ):
        if not keyword:
            yield event.plain_result("请指定要查看的模板关键词")
            return

        template_info = await self.meme_manager.get_template_info(str(keyword))
        if not template_info:
            yield event.plain_result("未找到相关模板")
            return

        yield event.plain_result(self._build_template_info_text(template_info))

    async def handle_disable_template(
        self,
        event: AstrMessageEvent,
        template_name: str | None = None,
    ):
        if not template_name:
            yield event.plain_result("请指定要禁用的模板名称")
            return
        if not await self.meme_manager.template_manager.keyword_exists(template_name):
            yield event.plain_result(f"模板 {template_name} 不存在")
            return
        if self.config.is_template_disabled(template_name):
            yield event.plain_result(f"模板 {template_name} 已被禁用")
            return
        if self.config.disable_template(template_name):
            yield event.plain_result(f"✅ 已禁用模板: {template_name}")
        else:
            yield event.plain_result(f"❌ 禁用模板失败: {template_name}")

    async def handle_enable_template(
        self,
        event: AstrMessageEvent,
        template_name: str | None = None,
    ):
        if not template_name:
            yield event.plain_result("请指定要启用的模板名称")
            return
        if not await self.meme_manager.template_manager.keyword_exists(template_name):
            yield event.plain_result(f"模板 {template_name} 不存在")
            return
        if not self.config.is_template_disabled(template_name):
            yield event.plain_result(f"模板 {template_name} 未被禁用")
            return
        if self.config.enable_template(template_name):
            yield event.plain_result(f"✅ 已启用模板: {template_name}")
        else:
            yield event.plain_result(f"❌ 启用模板失败: {template_name}")

    async def handle_list_disabled(self, event: AstrMessageEvent):
        disabled_templates = self.config.get_disabled_templates()
        if not disabled_templates:
            yield event.plain_result("📋 当前没有禁用的模板")
            return
        yield event.plain_result(
            self._format_template_list(
                disabled_templates,
                title="🔒 禁用模板列表",
                empty_message="当前没有禁用的模板",
            )
        )

    def _format_template_list(
        self,
        templates: list,
        title: str,
        empty_message: str,
        items_per_page: int = 20,
    ) -> str:
        if not templates:
            return f"{title}\n{empty_message}"

        total_items = len(templates)
        total_pages = (total_items + items_per_page - 1) // items_per_page
        result = f"{title}\n📊 总计: {total_items} 个模板\n"

        if total_pages > 1:
            result += f"📄 分页显示 (每页 {items_per_page} 个，共 {total_pages} 页)\n"

        result += "─" * 30 + "\n"
        page_templates = templates[:items_per_page]
        max_index_width = len(str(len(page_templates)))
        for i, template in enumerate(page_templates, 1):
            result += f"{i:>{max_index_width}}. {template}\n"

        if total_pages > 1:
            result += "─" * 30 + "\n"
            result += f"💡 提示: 当前显示第 1/{total_pages} 页"
            if total_items > items_per_page:
                result += f"，还有 {total_items - items_per_page} 个模板未显示"

        return result

    @staticmethod
    def _build_template_info_text(template_info: dict) -> str:
        meme_info = ""
        if template_info["name"]:
            meme_info += f"名称：{template_info['name']}\n"
        if template_info["keywords"]:
            meme_info += f"别名：{template_info['keywords']}\n"

        max_images = template_info["max_images"]
        min_images = template_info["min_images"]
        if max_images > 0:
            meme_info += (
                f"所需图片：{min_images}张\n"
                if min_images == max_images
                else f"所需图片：{min_images}~{max_images}张\n"
            )

        max_texts = template_info["max_texts"]
        min_texts = template_info["min_texts"]
        if max_texts > 0:
            meme_info += (
                f"所需文本：{min_texts}段\n"
                if min_texts == max_texts
                else f"所需文本：{min_texts}~{max_texts}段\n"
            )

        if template_info["default_texts"]:
            meme_info += f"默认文本：{template_info['default_texts']}\n"
        if template_info["tags"]:
            meme_info += f"标签：{template_info['tags']}\n"
        return meme_info


class GenerationHandler:
    """表情包生成命令处理器"""

    def __init__(self, meme_manager: MemeManager):
        self.meme_manager = meme_manager

    async def handle_generate_meme(self, event: AstrMessageEvent):
        try:
            image = await self.meme_manager.generate_meme(event)
            if image:
                user_id = event.get_sender_id()
                message_str = event.get_message_str()
                logger.info(
                    f"表情包生成成功 - 用户: {user_id}, 消息: "
                    f"{message_str[:50]}{'...' if len(message_str) > 50 else ''}"
                )
                yield event.chain_result([Comp.Image.fromBytes(image)])
        except ResourceNotReadyError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            user_id = event.get_sender_id()
            message_str = event.get_message_str()
            logger.error(
                f"表情包生成异常 - 用户: {user_id}, 消息: "
                f"{message_str[:50]}{'...' if len(message_str) > 50 else ''}, 错误: {e}"
            )

    async def handle_random_meme(self, event: AstrMessageEvent):
        """Send a random meme together with the command that recreates it."""
        try:
            generated = await self.meme_manager.generate_random_meme(event)
            if generated:
                image, keyword = generated
                prefix = self.meme_manager.config.trigger_prefix
                command = f"{prefix}{keyword}"
                yield event.chain_result([
                    Comp.Plain(f"这是指令：{command}\n"),
                    Comp.Image.fromBytes(image),
                ])
        except ResourceNotReadyError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.error("随机表情生成失败: %s", exc, exc_info=True)


def _is_poke_to_bot(event: AstrMessageEvent, allow_any_target: bool = False) -> bool:
    """Return whether this event contains a poke aimed at the bot.

    LLBot's aiocqhttp poke notices may omit ``self_id`` even though the event is
    delivered to this bot. In that case, accept the Poke component rather than
    silently discarding a valid poke response.
    """
    components = [
        component for component in event.get_messages() if isinstance(component, Comp.Poke)
    ]
    if not components:
        return False
    if allow_any_target:
        logger.info("收到戳一戳事件，已开启任意目标响应")
        return True

    self_id = str(event.get_self_id() or "").strip()
    if not self_id:
        logger.info("收到未携带机器人 ID 的戳一戳事件，按 LLBot 兼容模式响应")
        return True

    for component in components:
        target_getter = getattr(component, "target_id", None)
        target = target_getter() if callable(target_getter) else getattr(component, "qq", None)
        if target is None:
            logger.info("收到未携带目标 ID 的戳一戳事件，按兼容模式响应")
            return True
        if str(target).strip() == self_id:
            return True
        logger.info("忽略戳向其他用户的事件：目标=%s，机器人=%s", target, self_id)
    return False


class AdminHandlers:
    """管理员命令处理器"""

    def __init__(self, config: MemeConfig):
        self.config = config

    async def handle_enable_plugin(self, event: AstrMessageEvent):
        if self.config.enable_plugin_func():
            yield event.plain_result("✅ 表情包生成功能已启用")
        else:
            yield event.plain_result("ℹ️ 表情包生成功能已经是启用状态")

    async def handle_disable_plugin(self, event: AstrMessageEvent):
        if self.config.disable_plugin_func():
            yield event.plain_result("🔒 表情包生成功能已禁用")
        else:
            yield event.plain_result("ℹ️ 表情包生成功能已经是禁用状态")




def load_metadata_from_yaml():
    """从metadata.yaml加载插件元数据"""
    try:
        metadata_path = Path(__file__).parent / "metadata.yaml"
        if metadata_path.exists():
            with open(metadata_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


_metadata = load_metadata_from_yaml()


@register(
    _metadata.get("name"),
    _metadata.get("author"),
    _metadata.get("desc"),
    _metadata.get("version"),
    _metadata.get("repo"),
)
class MemeGeneratorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        # 初始化配置管理器
        self.meme_config = MemeConfig(config)
        logger.info("表情插件初始化")

        # MEME_HOME 和插件缓存共用 AstrBot 分配的跨平台插件数据目录。
        data_dir = str(MEME_HOME)

        # 外部表情库的开关写在 MEME_HOME/config.toml；worker 启动时读取。
        self.external_library = ExternalLibraryManager(
            MEME_HOME,
            self.meme_config.github_proxy,
            _astrbot_http_proxy(),
            _astrbot_github_proxy(),
        )
        logger.info(
            "外部库下载代理：%s", self.external_library.proxy_label()
        )
        self.external_library.cleanup_temp_files()
        self._external_load_guard = False
        external_enabled = self.meme_config.enable_external_libraries
        try:
            if not external_enabled:
                # 外部库都不加载了，残留标记没有意义，留着会误伤下次开启
                self.external_library.clear_loading()
            else:
                record = self.external_library.loading_record()
                if record is not None:
                    # 只隔离上次真的在加载、现在还在的那几个文件；
                    # 用户手动换过库时，新库不会被牵连
                    moved = self.external_library.quarantine(record)
                    self.external_library.clear_loading()
                    if moved:
                        external_enabled = False
                        logger.error(
                            "上次 worker 在加载外部库时崩溃（多为 rustc 版本不匹配）。"
                            "已把 %s 移到 libraries/disabled/，本次以内置库启动；"
                            "换对版本后移回并执行 /表情包库 重载。",
                            "、".join(moved),
                        )
                    else:
                        logger.info(
                            "发现上次的加载崩溃标记，但对应动态库已不在，按正常流程加载"
                        )
            # 两个加载开关必须在 worker 启动之前写进 config.toml。
            self.external_library.apply_switches(
                self.meme_config.enable_builtin_library, external_enabled
            )
            self._external_load_guard = bool(
                external_enabled and self.external_library.has_libraries()
            )
        except ExternalLibraryError as exc:
            logger.warning("初始化外部库开关失败，按无外部库启动: %s", exc)

        no_templates = not self.meme_config.enable_builtin_library and (
            not external_enabled or not self.external_library.has_libraries()
        )
        if no_templates:
            logger.warning("内置库已关闭且没有可用的外部库，当前不会有任何表情模板")

        # 初始化核心管理器；原生库加载和生成都在 worker 子进程里完成。
        self.meme_manager = MemeManager(
            self.meme_config,
            data_dir,
            on_backend_starting=self._on_backend_starting,
            on_backend_started=self._on_backend_started,
            on_backend_error=self._on_backend_error,
        )

        # 初始化命令处理器
        self.template_handlers = TemplateHandlers(self.meme_manager, self.meme_config)
        self.generation_handler = GenerationHandler(self.meme_manager)
        self.admin_handlers = AdminHandlers(self.meme_config)

        if not gif_toolbox.is_video_supported():
            logger.warning(
                "缺少 imageio，「视频转gif」不可用。安装：pip install imageio imageio-ffmpeg"
            )

    def _on_backend_starting(self):
        """worker 启动前落崩溃标记；只有存在外部库时才需要。"""
        if self._external_load_guard:
            self.external_library.mark_loading()

    def _on_backend_started(self):
        """worker 成功加载注册表后清除崩溃标记。"""
        if self._external_load_guard:
            self.external_library.clear_loading()

    async def _on_backend_error(self, exc: Exception) -> bool:
        """worker 启动失败时，若像动态库加载问题就隔离并回退内置库。"""
        record = self.external_library.loading_record()
        if record is None:
            return False

        message = str(exc).lower()
        tail = " ".join(getattr(exc, "stderr_tail", []) or []).lower()
        combined = f"{message} {tail}"
        exit_code = getattr(exc, "exit_code", None)
        if exit_code == 3 or "another meme worker" in combined:
            # 旧 worker 还占着锁，不是 DLL 的问题，别误隔离。
            self.external_library.clear_loading()
            return False
        load_markers = (
            "loadlibrary",
            "failed to load library",
            "is compiled with rustc",
        )
        crashed = bool(
            exit_code is not None
            and exit_code not in (0, 2, 3, 4, 5, 6)
            and (exit_code < 0 or exit_code > 128)
        )
        if not any(marker in combined for marker in load_markers) and not crashed:
            # 配置、端口、Python 导入等失败与 DLL 无关，清标记避免下次误隔离。
            self.external_library.clear_loading()
            return False

        moved = self.external_library.quarantine(record)
        self.external_library.clear_loading()
        if not moved:
            return False

        logger.error(
            "表情包 worker 加载外部库失败，已把 %s 移到 libraries/disabled/，"
            "回退内置库",
            "、".join(moved),
        )
        self._external_load_guard = False
        try:
            self.external_library.apply_switches(
                self.meme_config.enable_builtin_library, False
            )
            await self.meme_manager.meme_service.restart()
        except Exception as restart_exc:
            logger.error("回退内置库后重启 worker 仍失败: %s", restart_exc)
            return False
        return True

    async def terminate(self):
        """AstrBot 禁用/重载插件时停止 worker。"""
        try:
            await self.meme_manager.shutdown()
        except Exception as exc:
            logger.error("停止表情包 worker 失败: %s", exc)

    async def __aenter__(self):
        """异步上下文管理器入口"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口 - 清理资源"""
        await self.cleanup()
        return False  # 不抑制异常

    async def cleanup(self):
        """清理资源"""
        try:
            await self.meme_manager.shutdown()
        except (AttributeError, RuntimeError) as e:
            logger.error(f"清理表情包后台任务时出错: {e}")

    async def _render_menu_result(
            self,
            event: AstrMessageEvent,
            template_name: str,
            template_data: dict[str, object],
            fallback_text: str,
    ):
        """渲染本地菜单；浏览器不可用时保留可操作的纯文本结果。"""
        template = _load_static_template(template_name)

        async def _render() -> str:
            return await self.html_render(template, template_data)

        mode, payload = await render_with_fallback(_render, fallback_text)
        if mode == "image":
            return event.image_result(payload)
        logger.warning("%s 渲染失败，已回退到纯文本输出", template_name)
        return event.plain_result(payload)

    @filter.command("表情帮助", alias={"meme帮助", "meme菜单"})
    async def meme_help_menu(self, event: AstrMessageEvent):
        """查看meme插件帮助菜单"""
        # 检查插件是否启用
        if not self.meme_config.is_plugin_enabled():
            if PermissionUtils.is_bot_admin(event):
                yield event.plain_result(PermissionUtils.get_plugin_disabled_message())
            return

        template_data = _load_static_data("meme_help.json")

        # 如果加载失败，使用默认的空数据
        if template_data is None:
            template_data = {
                "basic_commands": [],
                "toolbox_commands": [],
                "admin_commands": []
            }

        if not PermissionUtils.is_bot_admin(event):
            template_data["admin_commands"] = []

        if not self.meme_config.gif_enable_toolbox:
            template_data["toolbox_commands"] = []

        template_data["version"] = _metadata.get("version")
        template_data["author"] = _metadata.get("author")
        template_data["trigger_prefix"] = self.meme_config.trigger_prefix

        fallback_text = format_help_menu_text(template_data)
        yield await self._render_menu_result(
            event, "meme_help.html", template_data, fallback_text
        )

    def _resolve_list_style(
        self, args: list[str], total: int
    ) -> tuple[str, int | None]:
        """解析 /表情列表 后面的模式与页码。"""
        style = ""
        page: int | None = None
        for arg in args:
            lowered = arg.lower()
            if lowered in _LIST_TEXT_WORDS:
                style = "text"
            elif lowered in _LIST_IMAGE_WORDS:
                style = "image"
            elif arg.isdigit():
                page = max(1, int(arg))
        if not style:
            style = self.meme_config.list_style
        if style == "auto":
            style = "image" if total <= _LIST_IMAGE_LIMIT else "text"
        return style, page

    async def _send_text_template_list(
        self, event: AstrMessageEvent, template_data: dict, page: int | None = None
    ):
        """纯文本目录：多页时用合并转发一次发出，单页直接回文本。"""
        pages = format_template_text_pages(template_data)
        total_pages = len(pages)

        if page is not None:
            index = min(max(page, 1), total_pages) - 1
            text = pages[index]
            if total_pages > 1:
                if index + 1 < total_pages:
                    text += f"\n发送 /表情列表 文字 {index + 2} 看下一页"
                else:
                    text += "\n已是最后一页"
            yield event.plain_result(text)
            return

        if total_pages == 1:
            yield event.plain_result(pages[0])
            return

        contents = [[Comp.Plain(text)] for text in pages]
        async for result in self._send_forward(
            event, "表情模板目录", contents, max_fallback=1
        ):
            yield result

    @filter.command("表情列表", alias={"meme列表"})
    async def template_list(self, event: AstrMessageEvent):
        """查看所有可用的表情包模板"""
        # 检查插件是否启用
        if not self.meme_config.is_plugin_enabled():
            if PermissionUtils.is_bot_admin(event):
                yield event.plain_result(PermissionUtils.get_plugin_disabled_message())
            return

        memes = await self.meme_manager.template_manager.get_all_memes()
        disabled = set(self.meme_config.disabled_templates)
        templates = []
        for index, meme in enumerate(memes, 1):
            keywords = list(meme.info.keywords)
            display_name = keywords[0] if keywords else meme.key
            aliases = keywords[1:5]
            templates.append({
                "index": index,
                "key": meme.key,
                "display_name": display_name,
                "aliases": " · ".join(aliases),
                "keyword_count": len(keywords),
                "disabled": meme.key in disabled or bool(disabled.intersection(keywords)),
            })

        template_data: dict[str, object] = {
            "templates": templates,
            "total_templates": len(templates),
            "total_keywords": sum(item["keyword_count"] for item in templates),
            "disabled_templates_count": sum(item["disabled"] for item in templates),
            "version": _metadata.get("version"),
        }
        style, page = self._resolve_list_style(
            _split_command_args(event.message_str, {"表情列表", "meme列表"}),
            len(templates),
        )
        if style == "text":
            async for result in self._send_text_template_list(
                event, template_data, page
            ):
                yield result
            return

        fallback_text = format_template_list_text(template_data)
        yield await self._render_menu_result(
            event, "meme_list.html", template_data, fallback_text
        )

    @filter.command("随机表情")
    async def random_meme(self, event: AstrMessageEvent):
        """随机抽取一张可用表情，并显示其可复用的指令。"""
        if not self.meme_config.is_plugin_enabled():
            if PermissionUtils.is_bot_admin(event):
                yield event.plain_result(PermissionUtils.get_plugin_disabled_message())
            return

        async for result in self.generation_handler.handle_random_meme(event):
            yield result

    @filter.command("表情信息", alias={"meme信息"})
    async def template_info(
            self, event: AstrMessageEvent, keyword: str | int | None = None
    ):
        """查看指定表情包模板的详细信息"""
        # 检查插件是否启用
        if not self.meme_config.is_plugin_enabled():
            if PermissionUtils.is_bot_admin(event):
                yield event.plain_result(PermissionUtils.get_plugin_disabled_message())
            return

        async for result in self.template_handlers.handle_template_info(event, keyword):
            yield result

    @filter.command("单表情禁用", alias={"单meme禁用"})
    async def disable_template(
            self, event: AstrMessageEvent, template_name: str | None = None
    ):
        """禁用指定的表情包模板（仅限Bot管理员）"""
        # 检查管理员权限
        if not PermissionUtils.is_bot_admin(event):
            return

        async for result in self.template_handlers.handle_disable_template(event, template_name):
            yield result

    @filter.command("单表情启用", alias={"单meme启用"})
    async def enable_template(
            self, event: AstrMessageEvent, template_name: str | None = None
    ):
        """启用指定的表情包模板（仅限Bot管理员）"""
        # 检查管理员权限
        if not PermissionUtils.is_bot_admin(event):
            return

        async for result in self.template_handlers.handle_enable_template(event, template_name):
            yield result

    @filter.command("禁用列表")
    async def list_disabled(self, event: AstrMessageEvent):
        """查看被禁用的模板列表（仅限Bot管理员）"""
        # 检查管理员权限
        if not PermissionUtils.is_bot_admin(event):
            return

        async for result in self.template_handlers.handle_list_disabled(event):
            yield result

    @filter.command("表情启用", alias={"meme启用"})
    async def enable_plugin(self, event: AstrMessageEvent):
        """启用表情包生成功能（仅限Bot管理员）"""
        # 检查管理员权限
        if not PermissionUtils.is_bot_admin(event):
            return

        async for result in self.admin_handlers.handle_enable_plugin(event):
            yield result

    @filter.command("表情禁用", alias={"meme禁用"})
    async def disable_plugin(self, event: AstrMessageEvent):
        """禁用表情包生成功能（仅限Bot管理员）"""
        # 检查管理员权限
        if not PermissionUtils.is_bot_admin(event):
            return

        async for result in self.admin_handlers.handle_disable_plugin(event):
            yield result

    @filter.command("表情资源", alias={"meme资源", "表情资源状态"})
    async def resource_status(self, event: AstrMessageEvent):
        """查看表情包资源的初始化/下载进度"""
        if not PermissionUtils.is_bot_admin(event):
            return
        status = self.meme_manager.resource_status
        yield event.plain_result(status.format_status())

    async def _reload_meme_backend(self) -> int:
        """应用加载开关并重启 worker，返回当前模板数。"""
        # WebUI 改完配置后可能没有重建插件实例，这里重新读一遍当前配置对象。
        self.meme_config._load_config()
        self.external_library.refresh_proxy(_astrbot_http_proxy())
        self.external_library.github_proxy = str(
            self.meme_config.github_proxy or ""
        ).strip()
        self.external_library.apply_switches(
            self.meme_config.enable_builtin_library,
            self.meme_config.enable_external_libraries,
        )
        self._external_load_guard = bool(
            self.meme_config.enable_external_libraries
            and self.external_library.has_libraries()
        )
        count = await self.meme_manager.restart_backend()
        logger.info("表情包 worker 已重载，共 %d 个模板", count)
        return count

    async def _reload_after_library_change(self) -> tuple[int, str | None]:
        """重载 worker；新库加载失败时自动隔离并回退内置库。"""
        try:
            return await self._reload_meme_backend(), None
        except Exception as exc:
            recovered = await self._on_backend_error(exc)
            if not recovered:
                return 0, str(exc)
            try:
                count = await self.meme_manager.refresh_backend()
            except Exception as retry_exc:
                return 0, f"{exc}；回退内置库后重新加载仍失败：{retry_exc}"
            return count, f"外部库加载失败，已隔离并回退内置库：{exc}"

    @filter.command("表情包库", alias={"meme库"})
    async def meme_library(self, event: AstrMessageEvent):
        """查看表情包库，安装或卸载外部库（仅限 Bot 管理员）"""
        if not PermissionUtils.is_bot_admin(event):
            return

        args = _split_command_args(event.message_str, {"表情包库", "meme库"})
        action = args[0].lower() if args else ""
        target = " ".join(args[1:]).strip()

        if action in ("安装", "install"):
            target = target or DEFAULT_LIBRARY
            yield event.plain_result(
                f"⏳ 正在安装 {target}：先下载并预检动态库，再同步素材"
                "（已有的会跳过，中断后重试可续传）…"
            )
            backend_stopped = False

            async def _stop_for_replace() -> None:
                nonlocal backend_stopped
                await self.meme_manager.stop_backend()
                backend_stopped = True

            try:
                result = await asyncio.wait_for(
                    self.external_library.install(
                        target, before_replace=_stop_for_replace
                    ),
                    timeout=1800,
                )
            except asyncio.TimeoutError:
                if backend_stopped:
                    await self._reload_after_library_change()
                yield event.plain_result(
                    "❌ 超时（30 分钟）。已下载的素材会保留，重试会跳过已存在的文件。"
                )
                return
            except ExternalLibraryError as exc:
                if backend_stopped:
                    await self._reload_after_library_change()
                yield event.plain_result(f"❌ 安装失败：{exc}")
                return
            except Exception as exc:
                if backend_stopped:
                    await self._reload_after_library_change()
                yield event.plain_result(f"❌ 安装失败：{exc}")
                return

            count, reload_error = await self._reload_after_library_change()
            warnings = self.meme_manager.meme_service.load_warnings
            if reload_error:
                status_line = f"⚠️ {reload_error}"
                if count:
                    status_line += f"\n当前可用 {count} 个模板"
            else:
                status_line = f"🔄 已自动重载，共 {count} 个模板"
            yield event.plain_result(
                f"✅ 已安装 {result.repo}\n"
                + (f"{result.note}\n" if result.note else "")
                + f"动态库 {result.dll_name} · {_format_bytes(result.dll_bytes)}\n"
                f"素材 {result.image_files} 个文件 · "
                f"{_format_bytes(result.image_bytes)}\n"
                + (f"{result.tip}\n" if result.tip else "")
                + status_line
                + (
                    "\n⚠️ 外部库开关当前是关的，记得在配置里打开 "
                    "enable_external_libraries"
                    if not self.meme_config.enable_external_libraries
                    else ""
                )
                + (
                    "\n⚠️ worker 日志：" + "；".join(warnings[-2:])
                    if warnings
                    else ""
                )
            )
            return

        if action in ("重载", "reload"):
            count, reload_error = await self._reload_after_library_change()
            if reload_error:
                yield event.plain_result(
                    f"⚠️ {reload_error}\n当前可用 {count} 个模板"
                )
            else:
                yield event.plain_result(
                    f"✅ 已重载表情包引擎，共 {count} 个模板"
                )
            return

        if action in ("恢复", "restore"):
            if not target:
                yield event.plain_result("用法：/表情包库 恢复 meme-emoji")
                return
            try:
                await self.meme_manager.stop_backend()
            except Exception as exc:
                yield event.plain_result(f"❌ 停止表情包 worker 失败：{exc}")
                return
            try:
                name = await asyncio.to_thread(self.external_library.restore, target)
            except ExternalLibraryError as exc:
                await self._reload_after_library_change()
                yield event.plain_result(f"❌ 恢复失败：{exc}")
                return
            count, reload_error = await self._reload_after_library_change()
            status_line = (
                f"⚠️ {reload_error}" if reload_error else "🔄 已自动重载"
            )
            yield event.plain_result(
                f"✅ 已把 {name} 移回 libraries/\n"
                f"{status_line}，共 {count} 个模板"
            )
            return

        if action in ("卸载", "uninstall"):
            if not target:
                yield event.plain_result("用法：/表情包库 卸载 meme-emoji")
                return
            try:
                await self.meme_manager.stop_backend()
            except Exception as exc:
                yield event.plain_result(f"❌ 停止表情包 worker 失败：{exc}")
                return
            try:
                library, files, freed = await asyncio.to_thread(
                    self.external_library.uninstall, target
                )
            except ExternalLibraryError as exc:
                await self._reload_after_library_change()
                yield event.plain_result(f"❌ 卸载失败：{exc}")
                return
            count, reload_error = await self._reload_after_library_change()
            status_line = (
                f"⚠️ {reload_error}" if reload_error else "🔄 已自动重载"
            )
            yield event.plain_result(
                f"🗑️ 已卸载 {library.dll_name}，删除 {files} 个素材文件"
                f"（{_format_bytes(freed)}）\n"
                f"{status_line}，共 {count} 个模板"
            )
            return

        status = self.external_library.format_status(
            self.meme_manager.resource_status.ready
        )
        warnings = self.meme_manager.meme_service.load_warnings
        if warnings:
            status += "\n\n⚠️ worker 加载告警：\n" + "\n".join(warnings[-3:])
            status += (
                "\n可执行 /表情包库 安装 <库> 重新预检并换用可加载的 DLL，"
                "或 /表情包库 重载 重试。"
            )
        yield event.plain_result(status)

    @filter.command("清空表情", alias={"清空表情缓存", "清空缓存"})
    async def clear_meme_cache(self, event: AstrMessageEvent):
        """清空生成表情时缓存的头像（仅限 Bot 管理员）"""
        if not PermissionUtils.is_bot_admin(event):
            return

        stats = await asyncio.to_thread(self.meme_manager.clear_avatar_cache)
        if stats["removed"] == 0:
            yield event.plain_result("🧹 缓存已经是空的，没有需要清理的文件")
            return
        yield event.plain_result(
            f"🧹 已清空表情缓存\n"
            f"删除 {stats['removed']} 个文件，释放 {_format_bytes(stats['freed'])}\n"
            "下次生成表情时会重新拉取头像。"
        )

    @filter.command("表情状态", alias={"meme状态"})
    async def plugin_info(self, event: AstrMessageEvent):
        """查看表情状态（仅限Bot管理员）"""
        # 检查管理员权限
        if not PermissionUtils.is_bot_admin(event):
            return

        # 获取统计信息
        total_templates = 0
        total_keywords = 0
        poke_candidates = 0
        try:
            all_memes = await self.meme_manager.template_manager.get_all_memes()
            total_templates = len(all_memes)
            all_keywords = await self.meme_manager.template_manager.get_all_keywords()
            total_keywords = len(all_keywords)
            poke_candidates = len(await self.meme_manager.get_poke_candidates())
        except Exception:
            pass

        # 准备模板数据
        template_data = {
            "plugin_enabled": self.meme_config.is_plugin_enabled(),
            "avatar_cache_enabled": self.meme_config.enable_avatar_cache,
            "poke_response_enabled": self.meme_config.enable_poke_response,
            "poke_any_target": self.meme_config.poke_response_any_target,
            "poke_candidate_count": poke_candidates,
            "libraries": self.external_library.library_summary(),
            "cooldown_seconds": self.meme_config.cooldown_seconds,
            "generation_timeout": self.meme_config.generation_timeout,
            "cache_expire_hours": self.meme_config.cache_expire_hours,
            "trigger_prefix": self.meme_config.trigger_prefix,
            "disabled_templates_count": len(self.meme_config.disabled_templates),
            "total_templates": total_templates,
            "total_keywords": total_keywords,
            "version": _metadata.get("version", "unknown"),
            "author": _metadata.get("author", "unknown")
        }

        fallback_text = format_plugin_status_text(template_data)
        yield await self._render_menu_result(
            event, "meme_info.html", template_data, fallback_text
        )

    # --- GIF 工具箱 ---

    async def _send_forward(self, event: AstrMessageEvent, title: str,
                            contents: list[list], max_fallback: int | None = None):
        """把多组消息内容打包成合并转发；平台不支持时退化为逐条发送。

        ``max_fallback`` 用于限制退化时最多发几段——上千个模板会切成十几页，
        逐条发出去就是刷屏，这时只发第一段并提示按页查看。
        """
        try:
            nodes = [Comp.Node(name=title, content=c) for c in contents]
            chain = [Comp.Nodes(nodes=nodes)]
        except (AttributeError, TypeError) as exc:
            logger.warning("合并转发不可用，改为逐条发送: %s", exc)
            fallback = contents if max_fallback is None else contents[:max_fallback]
            for content in fallback:
                yield event.chain_result(content)
            if max_fallback is not None and len(contents) > max_fallback:
                yield event.plain_result(
                    f"共 {len(contents)} 段，发送 /表情列表 文字 <页码> 逐页查看"
                )
            return
        yield event.chain_result(chain)

    async def _load_image(
        self, event: AstrMessageEvent, action: str, semantic: bool = False
    ) -> bytes | None:
        """取出本次指令要处理的图片。

        自然语言触发（例如聊天里只说了「加速」）没带图片时返回 None，由调用方
        静默跳过：这种消息大概率不是要给机器人下指令，回一句「请发送图片」反而
        是在打搅聊天。显式指令没带图片才提示用法。
        """
        ref = await media_source.first_image_ref(event)
        if not ref:
            if semantic:
                return None
            raise GifToolboxError(f"没有收到图片，请发送或引用一张图片后再执行「{action}」")
        data = await media_source.download(ref)
        if not data:
            raise GifToolboxError("图片下载失败，请确认图片仍可访问")
        return data

    async def _run_image_job(
        self, event: AstrMessageEvent, action: str, worker, semantic: bool = False
    ):
        """下载一张图片 → 线程池中执行 worker → 只回发结果图。"""
        try:
            data = await self._load_image(event, action, semantic)
            if data is None:
                return
            image = await asyncio.to_thread(worker, data)
        except GifToolboxError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("%s失败: %s", action, exc, exc_info=True)
            yield event.plain_result(f"{action}失败：{exc}")
            return
        yield event.chain_result([Comp.Image.fromBytes(image)])

    async def _run_frames_job(
        self, event: AstrMessageEvent, action: str, worker, semantic: bool = False
    ):
        """下载一张图片 → 线程池中拆成多张结果图 → 合并转发发送。"""
        try:
            data = await self._load_image(event, action, semantic)
            if data is None:
                return
            pieces = await asyncio.to_thread(worker, data)
        except GifToolboxError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("%s失败: %s", action, exc, exc_info=True)
            yield event.plain_result(f"{action}失败：{exc}")
            return
        contents = [
            [Comp.Plain(label), Comp.Image.fromBytes(piece)]
            for label, piece in pieces
        ]
        async for result in self._send_forward(event, action, contents):
            yield result

    def _speed_worker(self, value: float, is_fps_mode: bool):
        output_format = self.meme_config.gif_output_format
        return lambda data: gif_toolbox.change_speed(data, value, is_fps_mode, output_format)

    @filter.regex("(?i)" + _TOOLBOX_ANY.pattern)
    async def gif_toolbox(self, event: AstrMessageEvent):
        """GIF 工具箱统一入口。

        用正则而不是 @filter.command：CommandFilter 要求消息带唤醒前缀或 @机器人，
        正则过滤器不受 wake_prefix 制约，所以带前缀的指令和裸发的自然语言都能进到
        这里，再由 _match_toolbox_intent 按语义开关决定放不放行。
        """
        if not self.meme_config.gif_enable_toolbox:
            return
        matched = _match_toolbox_intent(
            event.message_str,
            self.meme_config.gif_enable_semantic,
            _is_explicit_command(event),
        )
        if matched is None:
            return
        intent, semantic = matched
        implementations = {
            "video": self._toolbox_video,
            "lineart": self._toolbox_lineart,
            "age": self._toolbox_age,
            "concat": self._toolbox_concat,
            "decompose": self._toolbox_decompose,
            "reverse": self._toolbox_reverse,
            "crop": self._toolbox_crop,
            "speed": self._toolbox_speed,
            "accelerate": self._toolbox_accelerate,
            "decelerate": self._toolbox_decelerate,
        }
        async for result in implementations[intent](event, semantic):
            yield result

    async def _toolbox_speed(self, event: AstrMessageEvent, semantic: bool = False):
        """动图变速：变速 2x 为 2 倍速，变速 30fps 为指定帧率"""
        text = event.message_str
        fps = re.search(r"(\d+(?:\.\d+)?)\s*(?:fps|帧率|帧)", text, re.IGNORECASE)
        multiple = re.search(r"(\d+(?:\.\d+)?)\s*[xX×]\s*(?:倍速|倍)?", text)
        if fps:
            value, is_fps_mode = float(fps.group(1)), True
        elif multiple:
            value, is_fps_mode = float(multiple.group(1)), False
        else:
            value, is_fps_mode = _first_number(text, 2.0), False

        async for result in self._run_image_job(
            event, "变速", self._speed_worker(value, is_fps_mode), semantic
        ):
            yield result

    async def _toolbox_accelerate(self, event: AstrMessageEvent, semantic: bool = False):
        """动图加速：加速 5 / 5倍加速 都表示 5 倍速，默认 2 倍速"""
        factor = _first_number(event.message_str, 2.0)
        if factor <= 0:
            yield event.plain_result("加速倍数必须大于 0")
            return
        async for result in self._run_image_job(
            event, "加速", self._speed_worker(factor, False), semantic
        ):
            yield result

    async def _toolbox_decelerate(self, event: AstrMessageEvent, semantic: bool = False):
        """动图减速：减速 3 / 3倍减速 都表示放慢到 1/3，默认 2 倍慢放"""
        factor = _first_number(event.message_str, 2.0)
        if factor <= 0:
            yield event.plain_result("减速倍数必须大于 0")
            return
        async for result in self._run_image_job(
            event, "减速", self._speed_worker(1.0 / factor, False), semantic
        ):
            yield result

    async def _toolbox_reverse(self, event: AstrMessageEvent, semantic: bool = False):
        """动图倒放"""
        async for result in self._run_image_job(
            event, "倒放", gif_toolbox.reverse_gif, semantic
        ):
            yield result

    async def _toolbox_decompose(self, event: AstrMessageEvent, semantic: bool = False):
        """把动图拆成逐帧图片，以合并转发发送"""
        async for result in self._run_frames_job(
            event, "分解", self._decompose_worker, semantic
        ):
            yield result

    @staticmethod
    def _decompose_worker(data: bytes) -> list[tuple[str, bytes]]:
        return [
            (f"第{index}帧", piece)
            for index, piece in enumerate(gif_toolbox.decompose_gif(data), 1)
        ]

    async def _toolbox_crop(self, event: AstrMessageEvent, semantic: bool = False):
        """网格裁剪：裁剪 3x4，可附「边距上20」一类参数先裁边"""
        clean_text, margins = gif_toolbox.parse_margins(event.message_str)
        rows, cols = gif_toolbox.parse_grid(clean_text)

        try:
            data = await self._load_image(event, "裁剪", semantic)
            if data is None:
                return
            pieces = await asyncio.to_thread(
                gif_toolbox.crop_grid, data, rows, cols, margins
            )
        except GifToolboxError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("裁剪失败: %s", exc, exc_info=True)
            yield event.plain_result(f"裁剪失败：{exc}")
            return

        contents = [[Comp.Image.fromBytes(piece)] for piece in pieces]
        async for result in self._send_forward(event, "裁剪", contents):
            yield result

    async def _toolbox_lineart(self, event: AstrMessageEvent, semantic: bool = False):
        """把图片转成素描线稿"""
        async for result in self._run_image_job(
            event, "线稿转换", gif_toolbox.to_line_art, semantic
        ):
            yield result

    async def _toolbox_age(self, event: AstrMessageEvent, semantic: bool = False):
        """表情包做旧：做旧 10 表示处理 10 轮，数字越大越绿越糊（1~50）"""
        times = int(_first_number(event.message_str, 5))
        async for result in self._run_image_job(
            event, "做旧", lambda data: gif_toolbox.age_meme(data, times), semantic
        ):
            yield result

    async def _toolbox_concat(self, event: AstrMessageEvent, semantic: bool = False):
        """表情合成：把多张表情按顺序接成一条动图，动图沿用各自原速"""
        duration = _parse_frame_duration(event.message_str)
        refs = await media_source.all_image_refs(event)
        if not refs:
            if semantic:
                return
            yield event.plain_result(
                "没有收到图片。可以把多张图一起发送、引用含图消息，或使用合并转发"
            )
            return

        images = await media_source.download_all(refs)
        if not images:
            yield event.plain_result("图片下载失败，请确认图片仍可访问")
            return
        # 单张图接出来只有一帧，发出去是不会动的，容易被当成功能坏了
        if len(images) < 2:
            yield event.plain_result("只收到 1 张图，接不出会动的表情，请一次发送多张")
            return

        output_format = self.meme_config.gif_output_format
        try:
            image = await asyncio.to_thread(
                gif_toolbox.concat_images, images, duration, output_format
            )
        except GifToolboxError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("表情合成失败: %s", exc, exc_info=True)
            yield event.plain_result(f"表情合成失败：{exc}")
            return
        yield event.chain_result([Comp.Image.fromBytes(image)])

    async def _toolbox_video(self, event: AstrMessageEvent, semantic: bool = False):
        """视频转动图：视频转gif 1s-5.5s fps 10 0.5"""
        source = media_source.video_source(event)
        if not source:
            if semantic:
                return
            yield event.plain_result("没有收到视频，请回复一条视频消息或附上视频链接")
            return

        if not gif_toolbox.is_video_supported():
            yield event.plain_result(
                "缺少 imageio 依赖，无法转换视频。请执行：pip install imageio imageio-ffmpeg"
            )
            return

        options = self.meme_config.gif_video_options()
        params = gif_toolbox.parse_video_args(event.message_str, options)

        if not source.startswith("http") and not os.path.exists(source):
            source = await media_source.resolve_file_id(event, source)
            if not source:
                yield event.plain_result("无法解析该视频，请改用视频链接或直接发送视频")
                return

        temp_path, is_temp = "", False
        try:
            temp_path, is_temp = await media_source.fetch_video_to_temp(
                source, self.meme_config.gif_max_video_size_mb
            )
            image = await asyncio.to_thread(
                gif_toolbox.video_to_gif, temp_path, params, options
            )
        except (GifToolboxError, MediaError) as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("视频转gif失败: %s", exc, exc_info=True)
            yield event.plain_result(f"视频转gif失败：{exc}")
            return
        finally:
            if is_temp:
                media_source.remove_temp(temp_path)

        yield event.chain_result([Comp.Image.fromBytes(image)])

    @filter.event_message_type(EventMessageType.ALL)
    async def generate_meme(self, event: AstrMessageEvent):
        """
        表情包生成主流程处理器
        """
        if _is_poke_to_bot(event, self.meme_config.poke_response_any_target):
            if self.meme_config.is_plugin_enabled() and self.meme_config.enable_poke_response:
                async for result in self.generation_handler.handle_random_meme(event):
                    yield result
            elif not self.meme_config.enable_poke_response:
                logger.info("收到戳一戳事件，但 enable_poke_response 未开启")
            return

        # 检查是否是管理员命令，如果是则不处理
        message_str = event.message_str.strip()
        admin_commands = [
            "启用表情包", "meme启用", "启用插件",
            "禁用表情包", "meme禁用", "禁用插件", "关闭表情包",
            "表情状态", "meme状态",
            "表情帮助", "meme帮助",
            "表情列表", "meme列表",
            "随机表情",
            "禁用列表",
            "表情包库", "meme库",
            "清空表情", "清空表情缓存", "清空缓存"
        ]

        # 如果消息以管理员命令开头，则不处理
        for cmd in admin_commands:
            if message_str.startswith(cmd):
                return

        # GIF 工具箱由正则分发器接管，命中意图时不能再进表情生成流程。
        # 工具箱或自然语言识别关闭时这里放行，短词于是恢复成普通的表情关键词。
        if self.meme_config.gif_enable_toolbox and _match_toolbox_intent(
            message_str,
            self.meme_config.gif_enable_semantic,
            _is_explicit_command(event),
        ):
            return

        # 检查插件是否启用
        if not self.meme_config.is_plugin_enabled():
            # 插件被禁用时不响应普通用户，但Bot管理员可以看到提示
            if PermissionUtils.is_bot_admin(event):
                yield event.plain_result(PermissionUtils.get_plugin_disabled_message())
            return

        async for result in self.generation_handler.handle_generate_meme(event):
            yield result
