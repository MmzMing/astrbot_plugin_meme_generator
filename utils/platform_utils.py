"""平台适配工具模块"""

from astrbot.core.platform import AstrMessageEvent


class PlatformUtils:
    """平台适配工具类"""

    @staticmethod
    async def get_user_extra_info(event: AstrMessageEvent, target_id: str):
        """
        从消息平台获取用户额外信息
        
        Args:
            event: 消息事件
            target_id: 目标用户ID
            
        Returns:
            用户信息元组 (nickname, sex)，失败返回None
        """
        if event.get_platform_name() == "aiocqhttp":
            try:
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                assert isinstance(event, AiocqhttpMessageEvent)
                client = event.bot
                user_info = await client.get_stranger_info(user_id=int(target_id))
                raw_nickname = user_info.get("nickname")
                nickname = str(raw_nickname if raw_nickname is not None else "Unknown")
                sex = user_info.get("sex")
                return nickname, sex
            except Exception:
                return None

        return None

    @staticmethod
    def is_platform_supported(platform_name: str) -> bool:
        """
        检查平台是否支持
        
        Args:
            platform_name: 平台名称
            
        Returns:
            是否支持该平台
        """
        supported_platforms = ["aiocqhttp"]
        return platform_name in supported_platforms
