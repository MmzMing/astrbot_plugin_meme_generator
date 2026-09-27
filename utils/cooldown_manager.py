import time
from typing import Dict


class CooldownManager:
    """用户冷却管理器"""
    
    def __init__(self, cooldown_seconds: int = 3):
        self.cooldown_seconds = cooldown_seconds
        self._user_last_use: Dict[str, float] = {}
    
    def is_user_in_cooldown(self, user_id: str) -> bool:
        """
        检查用户是否在冷却期内
        
        Args:
            user_id: 用户ID
            
        Returns:
            是否在冷却期内
        """
        if self.cooldown_seconds <= 0:
            return False
            
        current_time = time.time()
        last_use_time = self._user_last_use.get(user_id, 0)
        
        return (current_time - last_use_time) < self.cooldown_seconds
    
    def get_remaining_cooldown(self, user_id: str) -> float:
        """
        获取用户剩余冷却时间
        
        Args:
            user_id: 用户ID
            
        Returns:
            剩余冷却时间(秒)，如果不在冷却期则返回0
        """
        if self.cooldown_seconds <= 0:
            return 0.0
            
        current_time = time.time()
        last_use_time = self._user_last_use.get(user_id, 0)
        remaining = self.cooldown_seconds - (current_time - last_use_time)
        
        return max(0.0, remaining)
    
    def record_user_use(self, user_id: str):
        """
        记录用户使用时间
        
        Args:
            user_id: 用户ID
        """
        self._user_last_use[user_id] = time.time()
    
    def update_cooldown_seconds(self, cooldown_seconds: int):
        """
        更新冷却时间
        
        Args:
            cooldown_seconds: 新的冷却时间
        """
        self.cooldown_seconds = cooldown_seconds
    
    def clear_user_cooldown(self, user_id: str):
        """
        清除用户冷却记录
        
        Args:
            user_id: 用户ID
        """
        self._user_last_use.pop(user_id, None)
    
    def clear_all_cooldowns(self):
        """清除所有用户的冷却记录"""
        self._user_last_use.clear()
