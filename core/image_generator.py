"""图片生成模块"""

import asyncio
from typing import List, Union, Dict
from meme_generator import (
    Meme,
    DeserializeError,
    ImageAssetMissing,
    ImageDecodeError,
    ImageEncodeError,
    ImageNumberMismatch,
    MemeFeedback,
    TextNumberMismatch,
    TextOverLength,
)
from meme_generator import Image as MemeImage
from astrbot.api import logger


class ImageGenerator:
    """图片生成器"""

    @staticmethod
    async def generate_image(
            meme: Meme,
            meme_images: List[MemeImage],
            texts: List[str],
            options: Dict[str, Union[bool, str, int, float]],
            timeout: int = 30
    ) -> bytes:
        """
        调用生成引擎创建表情包图片

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
            asyncio.TimeoutError: 生成超时时抛出
        """
        try:
            # 在线程池中异步执行生成任务，带超时控制
            result = await asyncio.wait_for(
                asyncio.to_thread(meme.generate, meme_images, texts, options),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.error(f"表情包生成超时({timeout}秒)")
            raise RuntimeError("表情包生成超时")

        # 处理各种错误情况
        if result is None:
            logger.error("生成结果为空")
        elif isinstance(result, ImageDecodeError):
            logger.error(f"图片解码失败：{result.error}")
        elif isinstance(result, ImageEncodeError):
            logger.error(f"图片编码失败：{result.error}")
        elif isinstance(result, ImageAssetMissing):
            logger.error(f"缺少图片资源：{result.path}")
        elif isinstance(result, DeserializeError):
            logger.error(f"参数解析失败：{result.error}")
        elif isinstance(result, ImageNumberMismatch):
            num = (
                f"{result.min} ~ {result.max}"
                if result.min != result.max
                else str(result.min)
            )
            logger.error(f"图片数量不符，应为 {num}，实际传入 {result.actual}")
        elif isinstance(result, TextNumberMismatch):
            num = (
                f"{result.min} ~ {result.max}"
                if result.min != result.max
                else str(result.min)
            )
            logger.error(f"文字数量不符，应为 {num}，实际传入 {result.actual}")
        elif isinstance(result, TextOverLength):
            text = result.text
            repr_text = text if len(text) <= 10 else (text[:10] + "...")
            logger.error(f"文字过长：{repr_text}")
        elif isinstance(result, MemeFeedback):
            logger.error(result.feedback)

        if not isinstance(result, bytes):
            raise RuntimeError("表情包生成失败")

        return result
