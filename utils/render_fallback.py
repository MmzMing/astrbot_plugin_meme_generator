from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


async def render_with_fallback(
    render_func: Callable[[], Awaitable[str]],
    fallback_text: str,
) -> tuple[str, str]:
    """Render an image when possible, otherwise fall back to plain text."""
    try:
        return "image", await render_func()
    except Exception:
        return "text", fallback_text


def format_help_menu_text(template_data: dict[str, Any]) -> str:
    version = template_data.get("version") or "unknown"
    author = template_data.get("author") or "unknown"
    trigger_prefix = template_data.get("trigger_prefix") or ""
    basic_commands = _format_command_lines(template_data.get("basic_commands", []))
    toolbox_commands = _format_command_lines(template_data.get("toolbox_commands", []))
    admin_commands = _format_command_lines(template_data.get("admin_commands", []))
    has_toolbox_commands = bool(
        isinstance(template_data.get("toolbox_commands"), list)
        and template_data.get("toolbox_commands")
    )
    has_admin_commands = bool(
        isinstance(template_data.get("admin_commands"), list)
        and template_data.get("admin_commands")
    )

    lines = [
        "Meme 表情菜单",
        f"版本：{version} | 作者：{author}",
        "",
        "基础命令：",
        *basic_commands,
    ]
    if has_toolbox_commands:
        lines.extend(
            [
                "",
                "GIF 工具箱（回复图片/视频后直接发指令，不需要前缀）：",
                *toolbox_commands,
            ]
        )
    if has_admin_commands:
        lines.extend(
            [
                "",
                "管理员命令：",
                *admin_commands,
            ]
        )

    lines.extend(
        [
            "",
            "使用建议：",
            (
                "1. 直接发送表情包关键词即可生成，例如“加载中”或“挠头”。"
                if not trigger_prefix
                else f"1. 当前需携带触发前缀“{trigger_prefix}”生成，例如“{trigger_prefix}加载中”。"
            ),
            "2. 支持 @用户 或直接填写 QQ 号自动获取头像，也可以上传图片作为输入。",
            "3. 引用他人消息时，可直接沿用对方头像与昵称信息。",
            "4. 帮助、列表、信息、管理命令与 GIF 工具箱不受表情触发前缀影响。",
        ]
    )
    return "\n".join(lines)


def format_plugin_status_text(template_data: dict[str, Any]) -> str:
    version = template_data.get("version") or "unknown"
    author = template_data.get("author") or "unknown"
    plugin_status = "启用" if template_data.get("plugin_enabled") else "禁用"
    avatar_cache_status = "开启" if template_data.get("avatar_cache_enabled") else "关闭"
    trigger_prefix = template_data.get("trigger_prefix") or ""
    trigger_prefix_display = trigger_prefix if trigger_prefix else "未启用"

    lines = [
        "Meme 插件状态",
        f"版本：{version} | 作者：{author}",
        "",
        "运行状态：",
        f"插件状态：{plugin_status}",
        f"头像缓存：{avatar_cache_status}",
        (
            "戳一戳响应：开启"
            if template_data.get("poke_response_enabled")
            else "戳一戳响应：关闭"
        ),
        f"可随机模板：{template_data.get('poke_candidate_count', 0)} 个",
        "",
        "配置参数：",
        f"触发前缀：{trigger_prefix_display}",
        f"冷却时间：{template_data.get('cooldown_seconds', 0)}秒",
        f"生成超时：{template_data.get('generation_timeout', 0)}秒",
        f"缓存过期：{template_data.get('cache_expire_hours', 0)}小时",
        f"禁用模板：{template_data.get('disabled_templates_count', 0)}个",
        "",
        "统计信息：",
        f"可用模板：{template_data.get('total_templates', 0)}",
        f"关键词数：{template_data.get('total_keywords', 0)}",
    ]

    libraries = template_data.get("libraries")
    if isinstance(libraries, list) and libraries:
        lines.extend(["", "表情包库："])
        for lib in libraries:
            if not isinstance(lib, dict):
                continue
            lines.append(
                f"- {lib.get('name')}：{lib.get('state')}（{lib.get('detail', '')}）"
            )
    return "\n".join(lines)


def format_template_list_text(template_data: dict[str, Any]) -> str:
    templates = template_data.get("templates")
    if not isinstance(templates, list) or not templates:
        return "表情模板库\n暂无可用模板"

    lines = [
        "表情模板库",
        (
            f"模板 {template_data.get('total_templates', 0)} 个 | "
            f"关键词 {template_data.get('total_keywords', 0)} 个"
        ),
        "",
    ]
    for item in templates:
        if not isinstance(item, dict):
            continue
        state = " [已禁用]" if item.get("disabled") else ""
        aliases = item.get("aliases")
        alias_text = f"（{aliases}）" if aliases else ""
        lines.append(
            f"{item.get('index')}. {item.get('display_name')}{alias_text}{state}"
        )
    return "\n".join(lines)


def format_template_text_pages(
    template_data: dict[str, Any], budget: int = 1800
) -> list[str]:
    """把模板目录切成多段纯文本，每段控制在字符预算内。

    上千个模板塞不进一条消息，这里按行累积切页，交给调用方用合并转发
    分节点发送；平台不支持合并转发时再按页码逐页查看。
    """
    templates = template_data.get("templates")
    if not isinstance(templates, list) or not templates:
        return ["表情模板库\n暂无可用模板"]

    total = template_data.get("total_templates", 0)
    keywords = template_data.get("total_keywords", 0)
    header = f"表情模板库　{total} 个模板 / {keywords} 个关键词"

    pages: list[list[str]] = []
    current: list[str] = [header, ""]
    size = len(header) + 1
    for item in templates:
        if not isinstance(item, dict):
            continue
        line = _format_template_line(item)
        if size + len(line) + 1 > budget and len(current) > 2:
            pages.append(current)
            current = ["表情模板库（续）", ""]
            size = len(current[0]) + 1
        current.append(line)
        size += len(line) + 1
    if len(current) > 2 or not pages:
        pages.append(current)

    total_pages = len(pages)
    return [
        "\n".join(lines + ["", f"第 {index} / {total_pages} 页"])
        for index, lines in enumerate(pages, 1)
    ]


def _format_template_line(item: dict[str, Any]) -> str:
    aliases = str(item.get("aliases") or "")
    parts = [part for part in aliases.split(" · ") if part][:2]
    alias_text = f"（{' / '.join(parts)}）" if parts else ""
    state = " [禁用]" if item.get("disabled") else ""
    return f"{item.get('index')}. {item.get('display_name')}{alias_text}{state}"


def _format_command_lines(commands: list[dict[str, Any]] | Any) -> list[str]:
    if not isinstance(commands, list) or not commands:
        return ["- 暂无数据"]

    lines: list[str] = []
    for command in commands:
        if not isinstance(command, dict):
            continue
        name = str(command.get("name") or "未命名命令")
        desc = str(command.get("desc") or "无说明")
        lines.append(f"- {name}：{desc}")

    return lines or ["- 暂无数据"]
