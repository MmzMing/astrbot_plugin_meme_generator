"""模板管理模块

模板元数据由 worker 子进程提供；主进程只缓存可序列化的 ``WorkerMeme``，
因此重载插件或重启 worker 后可以重新拉取列表。
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

from astrbot.api import logger

from .meme_service import MemeService
from .meme_types import WorkerMeme


class TemplateManager:
    """表情包模板管理器"""

    def __init__(self, meme_service: MemeService):
        self.meme_service = meme_service
        self._memes: Optional[List[WorkerMeme]] = None
        self._meme_keywords: Optional[List[str]] = None
        self._load_lock = asyncio.Lock()

    async def _load_locked(self) -> None:
        try:
            memes = await self.meme_service.list_memes()
        except Exception as exc:
            logger.error("读取表情包模板失败: %s", exc)
            # 保留 None，下一次命令或资源检查完成后可以重试。
            self._memes = None
            self._meme_keywords = []
            return

        self._memes = memes
        self._meme_keywords = [
            keyword for meme in memes for keyword in meme.info.keywords
        ]
        if memes:
            logger.debug("📦 已从 worker 加载 %d 个表情包模板", len(memes))
        else:
            logger.warning("worker 没有返回任何表情包模板")

    async def _ensure_templates_loaded(self) -> None:
        """确保模板已加载（懒加载机制）。"""
        if self._memes is not None:
            return
        async with self._load_lock:
            if self._memes is None:
                await self._load_locked()

    async def refresh_templates(self) -> None:
        """重新从 worker 拉取模板列表。"""
        async with self._load_lock:
            await self._load_locked()

    async def find_meme(self, keyword: str) -> Optional[WorkerMeme]:
        """
        根据关键词查找表情包模板

        Args:
            keyword: 表情包关键词

        Returns:
            找到的模板，未找到返回None
        """
        await self._ensure_templates_loaded()
        for meme in self._memes or []:
            if keyword == meme.key or any(k == keyword for k in meme.info.keywords):
                return meme
        return None

    @staticmethod
    def normalize_trigger_message(
        message_str: str,
        trigger_prefix: str = "",
    ) -> str:
        """标准化触发消息，可按需移除首个关键词前缀。"""
        message_str = (message_str or "").strip()
        if not message_str:
            return ""

        if not trigger_prefix:
            return message_str

        words = message_str.split(maxsplit=1)
        first_word = words[0]
        if not first_word.startswith(trigger_prefix):
            return ""

        normalized_first = first_word[len(trigger_prefix):].strip()
        if not normalized_first:
            return ""

        if len(words) == 1:
            return normalized_first
        return f"{normalized_first} {words[1]}"

    async def find_keyword(
        self,
        message_str: str,
        trigger_prefix: str = "",
    ) -> Optional[str]:
        """
        从消息中查找匹配的关键词

        Args:
            message_str: 消息字符串

        Returns:
            匹配的关键词，未找到返回None
        """
        await self._ensure_templates_loaded()
        normalized_message = self.normalize_trigger_message(message_str, trigger_prefix)
        # 精确匹配：检查关键词是否等于消息字符串的第一个单词
        words = normalized_message.split()
        if not words:
            return None
        return next((k for k in self._meme_keywords or [] if k == words[0]), None)

    async def get_all_keywords(self) -> List[str]:
        """获取所有关键词"""
        await self._ensure_templates_loaded()
        return (self._meme_keywords or []).copy()

    async def get_all_memes(self) -> List[WorkerMeme]:
        """获取所有表情包模板"""
        await self._ensure_templates_loaded()
        return (self._memes or []).copy()

    async def keyword_exists(self, keyword: str) -> bool:
        """检查关键词是否存在"""
        await self._ensure_templates_loaded()
        return keyword in (self._meme_keywords or [])
