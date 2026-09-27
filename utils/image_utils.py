"""图片处理工具模块"""

import io
from PIL import Image
from astrbot.api import logger


class ImageUtils:
    """图片处理工具类"""

    @staticmethod
    def compress_image(image: bytes, max_size: int = 512) -> bytes | None:
        """
        压缩静态图片或GIF到max_size大小
        
        Args:
            image: 图片字节数据
            max_size: 最大尺寸
            
        Returns:
            压缩后的图片字节数据，如果是GIF则返回None
        """
        try:
            # 将输入的bytes加载为图片
            img = Image.open(io.BytesIO(image))
            output = io.BytesIO()

            if img.format == "GIF":
                return None
            else:
                # 如果是静态图片，检查尺寸并压缩
                if img.width > max_size or img.height > max_size:
                    img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
                # 保存处理后的图片到内存中的BytesIO对象
                img.save(output, format=img.format)

            # 返回处理后的图片数据（bytes）
            return output.getvalue()

        except Exception as e:
            logger.error(f"图片压缩失败: {e}")
            raise ValueError(f"图片压缩失败: {e}")
