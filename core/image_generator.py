"""图片生成模块"""

import asyncio
from typing import Dict, List, Union

from astrbot.api import logger

from .meme_service import MemeGenerationError, MemeService, MemeServiceError
from .meme_types import MemeImage, WorkerMeme


class ImageGenerator:
    """图片生成器（通过 worker 子进程执行）"""

    def __init__(self, meme_service: MemeService):
        self.meme_service = meme_service

    async def generate_image(
            self,
            meme: WorkerMeme,
            meme_images: List[MemeImage],
            texts: List[str],
            options: Dict[str, Union[bool, str, int, float]],
            timeout: int = 30
    ) -> bytes:
        """
        调用 worker 创建表情包图片

        Args:
            meme: 表情包模板
            meme_images: 图片列表
            texts: 文本列表
            options: 选项参数
            timeout: 超时时间(秒)

        Returns:
            生成的图片字节数据

        Raises:
            RuntimeError: 生成失败时抛出
        """
        try:
            return await self.meme_service.generate(
                meme.key, meme_images, texts, options, timeout
            )
        except asyncio.TimeoutError as exc:
            logger.error("表情包生成超时(%s秒)", timeout)
            raise RuntimeError("表情包生成超时") from exc
        except MemeGenerationError as exc:
            logger.error("表情包生成失败: %s", exc)
            raise RuntimeError(str(exc)) from exc
        except MemeServiceError as exc:
            logger.error("表情包 worker 异常: %s", exc)
            raise RuntimeError("表情包生成服务暂时不可用，请稍后重试") from exc
