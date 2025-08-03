"""
客户端连接管理器
负责管理WebSocket客户端连接、自动重连等功能
"""

import asyncio
import websockets
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from django.utils import timezone
from .models import ClientConnection

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('client_manager')

# 存储活跃连接的字典
active_connections = {}

# 重连任务字典
reconnect_tasks = {}

# 锁，用于线程安全
connection_lock = threading.Lock()


class ClientConnectionManager:
    """客户端连接管理器"""
    
    def __init__(self):
        self.is_running = False
        self.event_loop = None
        self.monitor_task = None
    
    def start(self):
        """启动客户端连接管理器"""
        if self.is_running:
            return
        
        self.is_running = True
        
        # 在新线程中启动事件循环
        self.thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self.thread.start()
        
        logger.info("客户端连接管理器已启动")
    
    def stop(self):
        """停止客户端连接管理器"""
        if not self.is_running:
            return
        
        self.is_running = False
        
        if self.event_loop:
            # 停止监控任务
            if self.monitor_task:
                self.monitor_task.cancel()
            
            # 关闭所有连接
            asyncio.run_coroutine_threadsafe(self._close_all_connections(), self.event_loop)
            
            # 停止事件循环
            self.event_loop.call_soon_threadsafe(self.event_loop.stop)
        
        logger.info("客户端连接管理器已停止")
    
    def _run_event_loop(self):
        """在新线程中运行事件循环"""
        self.event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.event_loop)
        
        # 启动监控任务
        self.monitor_task = self.event_loop.create_task(self._monitor_connections())
        
        # 运行事件循环
        try:
            self.event_loop.run_forever()
        except Exception as e:
            logger.error(f"事件循环异常: {e}")
        finally:
            self.event_loop.close()
    
    async def _monitor_connections(self):
        """监控连接状态和自动重连"""
        while self.is_running:
            try:
                # 在线程池中执行数据库查询
                loop = asyncio.get_event_loop()
                connections = await loop.run_in_executor(
                    None, 
                    lambda: list(ClientConnection.objects.filter(is_active=True))
                )
                
                for conn in connections:
                    await self._check_and_reconnect(conn)
                
                # 每30秒检查一次
                await asyncio.sleep(30)
                
            except Exception as e:
                logger.error(f"监控连接时发生错误: {e}")
                await asyncio.sleep(10)
    
    def _save_connection_status(self, connection, is_connected):
        """同步方法：保存连接状态到数据库"""
        connection.is_connected = is_connected
        connection.save()
    
    def _update_heartbeat(self, connection):
        """同步方法：更新心跳时间"""
        connection.last_heartbeat_at = timezone.now()
        connection.save()
    
    def _update_connection_stats(self, connection):
        """同步方法：更新连接统计信息"""
        connection.last_connected_at = timezone.now()
        connection.last_heartbeat_at = timezone.now()
        connection.reconnect_count += 1
        connection.save()
    
    def _save_connected_status(self, connection):
        """同步方法：保存已连接状态"""
        connection.is_connected = True
        connection.last_connected_at = timezone.now()
        connection.last_heartbeat_at = timezone.now()
        connection.save()
    
    def _execute_send_message(self, name, text):
        """同步方法：执行发送消息"""
        from .views import get_wechat_instance, lock
        import comtypes
        
        comtypes.CoInitialize()
        with lock:
            return get_wechat_instance().send_msg(name, text)
    
    def _execute_at_user(self, name, at_name, text):
        """同步方法：执行@用户"""
        from .views import get_wechat_instance, lock
        import comtypes
        
        comtypes.CoInitialize()
        with lock:
            get_wechat_instance().at(name, at_name, search_user=True, text=text)
    
    def _execute_send_file(self, name, file_path):
        """同步方法：执行发送文件"""
        from .views import get_wechat_instance, lock
        import comtypes
        
        comtypes.CoInitialize()
        with lock:
            get_wechat_instance().send_file(name, file_path)
    
    def _execute_check_status(self):
        """同步方法：执行状态检查"""
        from .views import get_wechat_instance, lock
        import comtypes
        
        comtypes.CoInitialize()
        with lock:
            get_wechat_instance().prevent_offline()
    
    async def _check_and_reconnect(self, connection):
        """检查连接状态并在需要时重连"""
        conn_id = str(connection.stable_id)
        
        # 检查是否已经有活跃连接
        if conn_id in active_connections:
            websocket = active_connections[conn_id]['websocket']
            if websocket.closed:
                # 连接已断开，清理并重连
                del active_connections[conn_id]
                # 在线程池中执行数据库保存操作
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._save_connection_status, connection, False)
                logger.info(f"检测到连接 {conn_id} 已断开")
            else:
                # 连接正常，发送心跳
                try:
                    await self._send_heartbeat(websocket)
                    # 在线程池中更新心跳时间
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, self._update_heartbeat, connection)
                    return
                except Exception as e:
                    logger.warning(f"心跳发送失败 {conn_id}: {e}")
                    del active_connections[conn_id]
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, self._save_connection_status, connection, False)
        
        # 需要重连
        if conn_id not in reconnect_tasks:
            reconnect_tasks[conn_id] = asyncio.create_task(self._reconnect_with_backoff(connection))
    
    async def _reconnect_with_backoff(self, connection):
        """使用指数退避算法重连"""
        conn_id = str(connection.stable_id)
        retry_count = 0
        max_retry_interval = 300  # 5分钟
        
        while self.is_running and connection.is_active:
            try:
                # 计算重连延迟（指数退避）
                delay = min(2 ** retry_count, max_retry_interval)
                
                if retry_count > 0:
                    logger.info(f"等待 {delay} 秒后重连 {conn_id} (第{retry_count}次尝试)")
                    await asyncio.sleep(delay)
                
                # 尝试连接
                if await self._connect_to_client(connection):
                    logger.info(f"重连成功: {conn_id}")
                    # 在线程池中更新重连统计
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, self._update_connection_stats, connection)
                    break
                else:
                    retry_count += 1
                    logger.warning(f"重连失败: {conn_id}, 将在 {min(2 ** retry_count, max_retry_interval)} 秒后重试")
                
            except asyncio.CancelledError:
                logger.info(f"重连任务被取消: {conn_id}")
                break
            except Exception as e:
                logger.error(f"重连过程中发生错误 {conn_id}: {e}")
                retry_count += 1
                await asyncio.sleep(min(2 ** retry_count, max_retry_interval))
        
        # 清理重连任务
        if conn_id in reconnect_tasks:
            del reconnect_tasks[conn_id]
    
    async def _connect_to_client(self, connection):
        """连接到客户端WebSocket服务器"""
        try:
            conn_id = str(connection.stable_id)
            uri = connection.connection_url
            
            logger.info(f"尝试连接到客户端: {uri}")
            
            # 构建认证信息
            auth_info = {
                'connection_key': connection.connection_key,
                'client_info': {
                    'stable_server_id': conn_id,
                    'server_type': 'YuYuWechatV2_Server',
                    'api_url': 'http://localhost:8000',
                    'version': '2.0.0',
                    'features': ['send_message', 'send_file', 'get_dialogs', 'at_user']
                }
            }
            
            # 连接到WebSocket服务器
            websocket = await websockets.connect(uri, timeout=10)
            
            # 发送认证信息
            await websocket.send(json.dumps(auth_info))
            
            # 等待认证响应
            response = await websocket.recv()
            response_data = json.loads(response)
            
            if response_data.get('status') == 'connected':
                # 连接成功
                active_connections[conn_id] = {
                    'websocket': websocket,
                    'connection': connection,
                    'connected_at': timezone.now()
                }
                
                # 在线程池中保存连接状态
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._save_connected_status, connection)
                
                # 启动消息处理
                asyncio.create_task(self._handle_messages(websocket, connection))
                
                logger.info(f"成功连接到客户端: {conn_id}")
                return True
            else:
                logger.error(f"客户端认证失败: {response_data.get('message', '未知错误')}")
                await websocket.close()
                return False
                
        except Exception as e:
            logger.error(f"连接到客户端失败 {connection.stable_id}: {e}")
            return False
    
    async def _handle_client_command(self, websocket, connection, data):
        """
        处理来自客户端的命令请求
        """
        command = data.get('command')
        params = data.get('params', {})
        request_id = data.get('request_id')
        
        logger.info(f"处理客户端命令: {command}, 参数: {params}")
        
        try:
            # 根据命令类型调用相应的WeChat方法
            if command == 'send_message':
                name = params.get('name')
                text = params.get('text')
                
                if not name or not text:
                    raise ValueError("缺少必要参数: name 或 text")
                
                # 在线程池中执行微信操作
                loop = asyncio.get_event_loop()
                success = await loop.run_in_executor(None, self._execute_send_message, name, text)
                
                # 发送响应给客户端
                response = {
                    'type': 'command_response',
                    'request_id': request_id,
                    'success': success,
                    'command': command,
                    'message': '消息发送成功' if success else '消息发送失败'
                }
                
            elif command == 'at_user':
                name = params.get('name')
                at_name = params.get('at_name', '')
                text = params.get('text', '')
                
                if not name:
                    raise ValueError("缺少必要参数: name")
                
                # 在线程池中执行微信操作
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._execute_at_user, name, at_name, text)
                
                response = {
                    'type': 'command_response',
                    'request_id': request_id,
                    'success': True,
                    'command': command,
                    'message': '@用户成功'
                }
                
            elif command == 'send_file':
                name = params.get('name')
                file_path = params.get('file_path')
                
                if not name or not file_path:
                    raise ValueError("缺少必要参数: name 或 file_path")
                
                # 在线程池中执行微信操作
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._execute_send_file, name, file_path)
                
                response = {
                    'type': 'command_response',
                    'request_id': request_id,
                    'success': True,
                    'command': command,
                    'message': '文件发送成功'
                }
                
            elif command == 'check_status':
                # 在线程池中执行微信操作
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._execute_check_status)
                
                response = {
                    'type': 'command_response',
                    'request_id': request_id,
                    'success': True,
                    'command': command,
                    'message': '微信状态检查成功'
                }
                
            else:
                raise ValueError(f"未知命令: {command}")
                
        except Exception as e:
            logger.error(f"执行命令失败 {command}: {e}")
            response = {
                'type': 'command_response',
                'request_id': request_id,
                'success': False,
                'command': command,
                'error': str(e),
                'message': f'命令执行失败: {str(e)}'
            }
        
        # 发送响应给客户端
        try:
            await websocket.send(json.dumps(response))
            logger.info(f"命令响应已发送: {response}")
        except Exception as e:
            logger.error(f"发送命令响应失败: {e}")
    
    async def _handle_messages(self, websocket, connection):
        """处理来自客户端的消息"""
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    message_type = data.get('type')
                    
                    if message_type == 'heartbeat':
                        # 处理客户端发送的心跳请求
                        await websocket.send(json.dumps({
                            'type': 'heartbeat_response',
                            'timestamp': timezone.now().isoformat()
                        }))
                        # 在线程池中更新心跳时间
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, self._update_heartbeat, connection)
                    
                    elif message_type == 'heartbeat_response':
                        # 处理客户端对我们心跳的响应
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, self._update_heartbeat, connection)
                        logger.debug(f"收到客户端心跳响应: {connection.stable_id}")
                    
                    elif message_type == 'status_update':
                        # 处理状态更新
                        logger.info(f"收到客户端状态更新: {data}")
                    
                    elif message_type == 'command':
                        # 处理来自客户端的命令
                        await self._handle_client_command(websocket, connection, data)
                    
                    elif message_type == 'command_response':
                        # 处理命令响应
                        logger.info(f"收到命令响应: {data}")
                    
                    else:
                        logger.warning(f"收到未知类型消息: {message_type}, 数据: {data}")
                        
                except json.JSONDecodeError:
                    logger.warning(f"收到无效JSON消息: {message}")
                
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"客户端连接断开: {connection.stable_id}")
        except Exception as e:
            logger.error(f"处理客户端消息时发生错误: {e}")
        finally:
            # 清理连接
            conn_id = str(connection.stable_id)
            if conn_id in active_connections:
                del active_connections[conn_id]
            # 在线程池中保存断开状态
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._save_connection_status, connection, False)
    
    async def _send_heartbeat(self, websocket):
        """发送心跳"""
        await websocket.send(json.dumps({
            'type': 'heartbeat',
            'timestamp': timezone.now().isoformat()
        }))
    
    async def _close_all_connections(self):
        """关闭所有连接"""
        for conn_id, conn_info in list(active_connections.items()):
            try:
                await conn_info['websocket'].close()
            except Exception as e:
                logger.error(f"关闭连接时发生错误 {conn_id}: {e}")
        
        active_connections.clear()
    
    def connect_client(self, connection):
        """连接指定的客户端"""
        if not self.is_running:
            self.start()
        
        if self.event_loop:
            asyncio.run_coroutine_threadsafe(self._connect_to_client(connection), self.event_loop)
    
    def disconnect_client(self, connection):
        """断开指定的客户端连接"""
        conn_id = str(connection.stable_id)
        
        if conn_id in active_connections:
            websocket = active_connections[conn_id]['websocket']
            if self.event_loop:
                asyncio.run_coroutine_threadsafe(websocket.close(), self.event_loop)
        
        # 使用同步方法保存状态
        connection.is_connected = False
        connection.save()
    
    def get_connection_status(self):
        """获取所有连接状态"""
        status = []
        
        for conn_id, conn_info in active_connections.items():
            connection = conn_info['connection']
            status.append({
                'stable_id': conn_id,
                'name': connection.name,
                'ip_address': str(connection.ip_address),
                'port': connection.port,
                'is_connected': True,
                'connected_at': conn_info['connected_at'].isoformat(),
                'last_heartbeat': connection.last_heartbeat_at.isoformat() if connection.last_heartbeat_at else None
            })
        
        return status


# 全局实例
client_manager = ClientConnectionManager()


def start_client_manager():
    """启动客户端管理器"""
    client_manager.start()


def stop_client_manager():
    """停止客户端管理器"""
    client_manager.stop()


def get_active_connections():
    """获取活跃连接"""
    return active_connections


def send_command_to_client(connection, command, params=None):
    """向客户端发送命令"""
    if not client_manager.is_running:
        return False, "客户端管理器未运行"
    
    conn_id = str(connection.stable_id)
    if conn_id not in active_connections:
        return False, "客户端未连接"
    
    websocket = active_connections[conn_id]['websocket']
    
    message = {
        'type': 'command',
        'command': command,
        'params': params or {},
        'timestamp': timezone.now().isoformat()
    }
    
    try:
        # 在事件循环中发送消息
        future = asyncio.run_coroutine_threadsafe(
            websocket.send(json.dumps(message)), 
            client_manager.event_loop
        )
        future.result(timeout=5)  # 5秒超时
        return True, "命令发送成功"
    except Exception as e:
        logger.error(f"发送命令失败: {e}")
        return False, f"发送命令失败: {str(e)}"