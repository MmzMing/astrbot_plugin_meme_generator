import asyncio
import json
import os
import re
import sys
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

# meme_generator reads MEME_HOME while its native module is being imported.
# Bootstrap it before importing any module under core, because those modules
# import meme_generator at module scope. StarTools supplies AstrBot's
# cross-platform, plugin-specific data directory. The explicit plugin name is
# required here because AstrBot has not registered this module's metadata yet.
PLUGIN_NAME = "astrbot_plugin_meme_generator"
MEME_HOME = Path(StarTools.get_data_dir(PLUGIN_NAME))
if "meme_generator" in sys.modules:
    logger.warning(
        "meme_generator 已在资源目录初始化前被加载，MEME_HOME 可能无法在本次运行中生效"
    )
os.environ["MEME_HOME"] = str(MEME_HOME)
logger.info("表情包资源目录: %s", MEME_HOME)

from .core import gif_toolbox
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
    render_with_fallback,
)


PLUGIN_DIR = Path(__file__).parent
STATIC_DIR = PLUGIN_DIR / "static"
STATIC_HTML_DIR = STATIC_DIR / "html"
STATIC_DATA_DIR = STATIC_DIR / "data"

# GIF 工具箱的意图表。AstrBot 的 CommandFilter 要求 event.is_at_or_wake_command
# （必须带唤醒前缀或 @机器人），而 RegexFilter 不受 wake_prefix 制约，所以这批指令
# 全部走正则：裸发「加速5」也能触发，带前缀或自然说法同样命中。
# 列表顺序就是优先级，长形态必须排在短形态之前。
_WAKE_PREFIX = r"^\s*[/#>›]?\s*(?:@\S+\s*)?(?:请|帮我|帮忙|麻烦)?\s*(?:把|将)?\s*(?:这个|这张|那张)?\s*"

_TOOLBOX_INTENTS = [
    (r"视频\s*转\s*(?:成|为|换)?\s*(?:gif|动图|图)", "video"),
    (r"(?:图片?|照片)?\s*(?:转|画|变|生成|做成)?\s*(?:成|为)?\s*(?:线稿|素描)", "lineart"),
    (r"(?:表情包?|表情|动图|图)?\s*(?:做旧|做古|变旧|电子包浆)", "age"),
    (r"(?:表情包?|表情|动图|图)?\s*(?:合成|拼接|接成一条|拼成一条|连成一条)", "concat"),
    (r"(?:gif|动图)?\s*(?:分解|拆帧|逐帧|拆开|拆成?单帧)", "decompose"),
    (r"(?:gif|动图)?\s*(?:倒放|倒着(?:放|播)|反过来(?:放|播)|反向(?:放|播))", "reverse"),
    (r"(?:gif|动图|图片)?\s*(?:裁剪|切图|网格切|切成?\s*\d+\s*[*x×]\s*\d+)", "crop"),
    (r"\d+(?:\.\d+)?\s*(?:fps|帧率)\s*(?:变速|加速|调速)", "speed"),
    (r"(?:gif|动图)?\s*(?:变速|调速|改速度)", "speed"),
    (r"\d+(?:\.\d+)?\s*(?:倍速|倍|[xX×])\s*(?:加速|快进|调快)", "accelerate"),
    (r"(?:gif|动图)?\s*(?:加速|快进|变快|调快)", "accelerate"),
    (r"\d+(?:\.\d+)?\s*(?:倍速|倍|[xX×])\s*(?:减速|慢放|放慢|调慢)", "decelerate"),
    (r"(?:gif|动图)?\s*(?:减速|慢放|放慢|变慢|调慢)", "decelerate"),
    # 只说「3倍速」也算变速：必须排在带方向词的形态之后，避免抢走「3倍加速」。
    (r"\d+(?:\.\d+)?\s*(?:倍速|[xX×])", "speed"),
]
_TOOLBOX_PATTERNS = [
    (re.compile(_WAKE_PREFIX + body, re.IGNORECASE), intent)
    for body, intent in _TOOLBOX_INTENTS
]
# 只要任一意图能命中，这条消息就归工具箱，不能再进表情生成流程。
_TOOLBOX_ANY = re.compile(
    _WAKE_PREFIX + "(?:" + "|".join(body for body, _ in _TOOLBOX_INTENTS) + ")",
    re.IGNORECASE,
)


def _match_toolbox_intent(message: str) -> str | None:
    for pattern, intent in _TOOLBOX_PATTERNS:
        if pattern.search(message or ""):
            return intent
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




class MemeConfig:
    """表情包生成器配置管理类"""

    def __init__(self, config: AstrBotConfig):
        self.config = config
        self._load_config()

    def _load_config(self):
        """加载配置"""
        self.enable_plugin: bool = self.config.get("enable_plugin", True)
        self.trigger_prefix: str = str(self.config.get("trigger_prefix", "") or "")
        self.generation_timeout: int = self.config.get("generation_timeout", 30)
        self.cooldown_seconds: int = self.config.get("cooldown_seconds", 3)
        self.enable_avatar_cache: bool = self.config.get("enable_avatar_cache", True)
        self.cache_expire_hours: int = self.config.get("cache_expire_hours", 24)
        self.disabled_templates: list[str] = self.config.get("disabled_templates", [])
        self.enable_poke_response: bool = self.config.get("enable_poke_response", False)
        self.poke_response_any_target: bool = self.config.get(
            "poke_response_any_target", False
        )
        self.gif_enable_toolbox: bool = self.config.get("gif_enable_toolbox", True)
        self.gif_output_format: str = gif_toolbox.normalize_format(
            str(self.config.get("gif_output_format", "GIF"))
        )
        self.gif_default_scale: float = float(self.config.get("gif_default_scale", 0.3))
        self.gif_default_fps: int = int(self.config.get("gif_default_fps", 10))
        self.gif_max_duration: float = float(self.config.get("gif_max_duration", 10.0))
        self.gif_max_video_size_mb: float = float(
            self.config.get("gif_max_video_size_mb", 50.0)
        )
        self.gif_max_colors: int = int(self.config.get("gif_max_colors", 256))

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
        """保存特定配置项的专用方法"""
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

        # 初始化核心管理器
        self.meme_manager = MemeManager(self.meme_config, data_dir)

        # 初始化命令处理器
        self.template_handlers = TemplateHandlers(self.meme_manager, self.meme_config)
        self.generation_handler = GenerationHandler(self.meme_manager)
        self.admin_handlers = AdminHandlers(self.meme_config)

        if not gif_toolbox.is_video_supported():
            logger.warning(
                "缺少 imageio，「视频转gif」不可用。安装：pip install imageio imageio-ffmpeg"
            )

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
            # 停止缓存清理任务
            await self.meme_manager.cache_manager.stop_cleanup_task()
        except (AttributeError, RuntimeError) as e:
            logger.error(f"清理缓存管理器时出错: {e}")

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

    @filter.command("表情状态", alias={"meme状态"})
    async def plugin_info(self, event: AstrMessageEvent):
        """查看表情状态（仅限Bot管理员）"""
        # 检查管理员权限
        if not PermissionUtils.is_bot_admin(event):
            return

        # 获取统计信息
        total_templates = 0
        total_keywords = 0
        try:
            all_memes = await self.meme_manager.template_manager.get_all_memes()
            total_templates = len(all_memes)
            all_keywords = await self.meme_manager.template_manager.get_all_keywords()
            total_keywords = len(all_keywords)
        except Exception:
            pass

        # 准备模板数据
        template_data = {
            "plugin_enabled": self.meme_config.is_plugin_enabled(),
            "avatar_cache_enabled": self.meme_config.enable_avatar_cache,
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
                            contents: list[list]):
        """把多组消息内容打包成合并转发；平台不支持时退化为逐条发送。"""
        try:
            chain = [Comp.Nodes(nodes=[Comp.Node(name=title, content=c) for c in contents])]
        except (AttributeError, TypeError) as exc:
            logger.warning("合并转发不可用，改为逐条发送: %s", exc)
            for content in contents:
                yield event.chain_result(content)
            return
        yield event.chain_result(chain)

    async def _load_image(self, event: AstrMessageEvent, action: str) -> bytes:
        """取出本次指令要处理的图片；拿不到时抛出 GifToolboxError。"""
        ref = await media_source.first_image_ref(event)
        if not ref:
            raise GifToolboxError(f"没有收到图片，请发送或引用一张图片后再执行「{action}」")
        data = await media_source.download(ref)
        if not data:
            raise GifToolboxError("图片下载失败，请确认图片仍可访问")
        return data

    async def _run_image_job(self, event: AstrMessageEvent, action: str, worker):
        """下载一张图片 → 线程池中执行 worker → 只回发结果图。"""
        try:
            data = await self._load_image(event, action)
            image = await asyncio.to_thread(worker, data)
        except GifToolboxError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("%s失败: %s", action, exc, exc_info=True)
            yield event.plain_result(f"{action}失败：{exc}")
            return
        yield event.chain_result([Comp.Image.fromBytes(image)])

    async def _run_frames_job(self, event: AstrMessageEvent, action: str, worker):
        """下载一张图片 → 线程池中拆成多张结果图 → 合并转发发送。"""
        try:
            data = await self._load_image(event, action)
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
        正则过滤器不受 wake_prefix 制约，所以「加速5」「gif倒放」「帮我3倍速」
        这类裸发写法也能触发。
        """
        if not self.meme_config.gif_enable_toolbox:
            return
        intent = _match_toolbox_intent(event.message_str)
        if intent is None:
            return
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
        async for result in implementations[intent](event):
            yield result

    async def _toolbox_speed(self, event: AstrMessageEvent):
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
            event, "变速", self._speed_worker(value, is_fps_mode)
        ):
            yield result

    async def _toolbox_accelerate(self, event: AstrMessageEvent):
        """动图加速：加速 5 / 5倍加速 都表示 5 倍速，默认 2 倍速"""
        factor = _first_number(event.message_str, 2.0)
        if factor <= 0:
            yield event.plain_result("加速倍数必须大于 0")
            return
        async for result in self._run_image_job(
            event, "加速", self._speed_worker(factor, False)
        ):
            yield result

    async def _toolbox_decelerate(self, event: AstrMessageEvent):
        """动图减速：减速 3 / 3倍慢放 都表示放慢到 1/3，默认 2 倍慢放"""
        factor = _first_number(event.message_str, 2.0)
        if factor <= 0:
            yield event.plain_result("减速倍数必须大于 0")
            return
        async for result in self._run_image_job(
            event, "减速", self._speed_worker(1.0 / factor, False)
        ):
            yield result

    async def _toolbox_reverse(self, event: AstrMessageEvent):
        """动图倒放"""
        async for result in self._run_image_job(event, "倒放", gif_toolbox.reverse_gif):
            yield result

    async def _toolbox_decompose(self, event: AstrMessageEvent):
        """把动图拆成逐帧图片，以合并转发发送"""
        async for result in self._run_frames_job(event, "分解", self._decompose_worker):
            yield result

    @staticmethod
    def _decompose_worker(data: bytes) -> list[tuple[str, bytes]]:
        return [
            (f"第{index}帧", piece)
            for index, piece in enumerate(gif_toolbox.decompose_gif(data), 1)
        ]

    async def _toolbox_crop(self, event: AstrMessageEvent):
        """网格裁剪：裁剪 3x4，可附「边距上20」一类参数先裁边"""
        clean_text, margins = gif_toolbox.parse_margins(event.message_str)
        rows, cols = gif_toolbox.parse_grid(clean_text)

        try:
            data = await self._load_image(event, "裁剪")
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

    async def _toolbox_lineart(self, event: AstrMessageEvent):
        """把图片转成素描线稿"""
        async for result in self._run_image_job(
            event, "线稿转换", gif_toolbox.to_line_art
        ):
            yield result

    async def _toolbox_age(self, event: AstrMessageEvent):
        """表情包做旧：做旧 10 表示处理 10 轮，数字越大越绿越糊（1~50）"""
        times = int(_first_number(event.message_str, 5))
        async for result in self._run_image_job(
            event, "做旧", lambda data: gif_toolbox.age_meme(data, times)
        ):
            yield result

    async def _toolbox_concat(self, event: AstrMessageEvent):
        """表情合成：把多张表情按顺序接成一条动图，动图沿用各自原速"""
        duration = _parse_frame_duration(event.message_str)
        refs = await media_source.all_image_refs(event)
        if not refs:
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

    async def _toolbox_video(self, event: AstrMessageEvent):
        """视频转动图：视频转gif 1s-5.5s fps 10 0.5"""
        if not gif_toolbox.is_video_supported():
            yield event.plain_result(
                "缺少 imageio 依赖，无法转换视频。请执行：pip install imageio imageio-ffmpeg"
            )
            return

        options = self.meme_config.gif_video_options()
        params = gif_toolbox.parse_video_args(event.message_str, options)
        source = media_source.video_source(event)
        if not source:
            yield event.plain_result("没有收到视频，请回复一条视频消息或附上视频链接")
            return

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
            "禁用列表"
        ]

        # 如果消息以管理员命令开头，则不处理
        for cmd in admin_commands:
            if message_str.startswith(cmd):
                return

        # GIF 工具箱由正则分发器接管，命中意图时不能再进表情生成流程。
        # 工具箱关闭时这里放行，短词于是恢复成普通的表情关键词。
        if self.meme_config.gif_enable_toolbox and _TOOLBOX_ANY.search(message_str):
            return

        # 检查插件是否启用
        if not self.meme_config.is_plugin_enabled():
            # 插件被禁用时不响应普通用户，但Bot管理员可以看到提示
            if PermissionUtils.is_bot_admin(event):
                yield event.plain_result(PermissionUtils.get_plugin_disabled_message())
            return

        async for result in self.generation_handler.handle_generate_meme(event):
            yield result
