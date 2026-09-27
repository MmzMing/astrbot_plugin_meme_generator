import asyncio
import time
from typing import Optional
from astrbot.api import logger
from .avatar_cache import AvatarCache


class CacheManager:
    """缓存管理器 - 负责定期清理过期缓存"""
    
    def __init__(self, avatar_cache: AvatarCache, cleanup_interval_hours: int = 6):
        self.avatar_cache = avatar_cache
        self.cleanup_interval_hours = cleanup_interval_hours
        self.cleanup_task: Optional[asyncio.Task] = None
        self._running = False
    
    async def start_cleanup_task(self):
        """启动定期清理任务"""
        if self._running:
            return
        
        self._running = True
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.debug(f"缓存清理任务已启动，清理间隔: {self.cleanup_interval_hours}小时")
    
    async def stop_cleanup_task(self):
        """停止定期清理任务"""
        self._running = False
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
        logger.debug("缓存清理任务已停止")
    
    async def _cleanup_loop(self):
        """清理循环"""
        while self._running:
            try:
                # 等待清理间隔
                await asyncio.sleep(self.cleanup_interval_hours * 3600)
                
                if not self._running:
                    break
                
                # 执行清理
                await self.cleanup_expired_cache()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"缓存清理任务出错: {e}")
                # 出错后等待一段时间再继续
                await asyncio.sleep(300)  # 5分钟
    
    async def cleanup_expired_cache(self):
        """清理过期缓存"""
        try:
            start_time = time.time()
            
            # 获取清理前的统计信息
            stats_before = self.avatar_cache.get_cache_stats()
            
            # 清理过期缓存
            self.avatar_cache.clear_expired_cache()
            
            # 获取清理后的统计信息
            stats_after = self.avatar_cache.get_cache_stats()
            
            # 计算清理结果
            cleaned_count = stats_before["total_cached"] - stats_after["total_cached"]
            cleaned_size = stats_before["cache_size_bytes"] - stats_after["cache_size_bytes"]
            elapsed_time = time.time() - start_time
            
            if cleaned_count > 0:
                logger.info(
                    f"缓存清理完成: 清理了 {cleaned_count} 个过期缓存文件, "
                    f"释放了 {cleaned_size / 1024:.1f} KB 空间, "
                    f"耗时 {elapsed_time:.2f} 秒"
                )
            else:
                logger.debug("缓存清理完成: 没有发现过期缓存")
                
        except Exception as e:
            logger.error(f"清理过期缓存时出错: {e}")
    
    async def force_cleanup(self):
        """强制执行一次清理"""
        await self.cleanup_expired_cache()
    
    def get_cleanup_status(self) -> dict:
        """获取清理任务状态"""
        return {
            "running": self._running,
            "cleanup_interval_hours": self.cleanup_interval_hours,
            "task_status": "running" if self.cleanup_task and not self.cleanup_task.done() else "stopped"
        }
