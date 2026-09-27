"""权限检查工具模块"""

from astrbot.core.platform import AstrMessageEvent


class PermissionUtils:
    """权限检查工具类"""

    @staticmethod
    def is_bot_admin(event: AstrMessageEvent) -> bool:
        """
        检查用户是否为Bot管理员

        Args:
            event: 消息事件

        Returns:
            是否为Bot管理员
        """
        try:
            # 检查是否为超级用户（AstrBot框架的管理员检查）
            if hasattr(event, 'is_admin') and callable(event.is_admin):
                return event.is_admin()

            return False

        except Exception:

            return False
    
    @staticmethod
    def get_plugin_disabled_message() -> str:
        """
        获取插件已禁用的提示消息
        
        Returns:
            插件禁用提示
        """
        return "🔒 表情包生成功能已被管理员禁用"
