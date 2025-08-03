# Create your models here.
from django.db import models
import uuid


class WeChatConfig(models.Model):
    path = models.CharField(max_length=255, verbose_name="WeChat Path",
                            default="C:/Program Files/Tencent/WeChat/WeChat.exe")
    locale = models.CharField(max_length=10, verbose_name="Locale", default="zh-CN")

    def __str__(self):
        return f"WeChat Path: {self.path} (Locale: {self.locale})"


class ClientConnection(models.Model):
    """
    客户端连接模型
    用于管理和存储客户端连接信息
    """
    # 固定的服务器ID，用于稳定标识
    stable_id = models.CharField(max_length=64, unique=True, default=uuid.uuid4, verbose_name="稳定ID")
    
    # 连接基本信息
    name = models.CharField(max_length=100, verbose_name="连接名称", default="未命名客户端")
    ip_address = models.GenericIPAddressField(verbose_name="IP地址")
    port = models.PositiveIntegerField(verbose_name="端口", default=8765)
    connection_key = models.CharField(max_length=255, verbose_name="连接密钥")
    
    # 连接状态
    is_active = models.BooleanField(default=False, verbose_name="是否活跃")
    is_connected = models.BooleanField(default=False, verbose_name="是否已连接")
    
    # 时间字段
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")
    last_connected_at = models.DateTimeField(null=True, blank=True, verbose_name="最后连接时间")
    last_heartbeat_at = models.DateTimeField(null=True, blank=True, verbose_name="最后心跳时间")
    
    # 重连统计
    reconnect_count = models.PositiveIntegerField(default=0, verbose_name="重连次数")
    total_connect_time = models.DurationField(null=True, blank=True, verbose_name="总连接时长")
    
    # 客户端信息
    client_version = models.CharField(max_length=50, blank=True, verbose_name="客户端版本")
    client_features = models.JSONField(default=list, blank=True, verbose_name="客户端功能列表")
    
    class Meta:
        verbose_name = "客户端连接"
        verbose_name_plural = "客户端连接"
        ordering = ['-last_connected_at', '-created_at']
    
    def __str__(self):
        status = "已连接" if self.is_connected else "未连接"
        return f"{self.name} ({self.ip_address}:{self.port}) - {status}"
    
    @property
    def connection_url(self):
        """获取WebSocket连接URL"""
        return f"ws://{self.ip_address}:{self.port}"
