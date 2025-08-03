import json
import os
import threading
from queue import Queue, Empty

import comtypes
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, OpenApiResponse, inline_serializer
from rest_framework import serializers # 导入 serializers
from rest_framework.decorators import api_view # 导入 api_view

from .models import WeChatConfig, ClientConnection
from .ui_auto_wechat import WeChat
from .client_manager import client_manager, start_client_manager, send_command_to_client
from django.contrib import messages
from django.shortcuts import get_object_or_404
from django.core.paginator import Paginator


# 为 send_message 定义请求体的序列化器
class SendMessageSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="接收消息的联系人或群聊名称")
    text = serializers.CharField(help_text="要发送的文本消息内容")

# 为 at_user 定义请求体的序列化器
class AtUserSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="群聊名称")
    at_name = serializers.CharField(help_text="要@的用户名称，空字符串表示@所有人")
    text = serializers.CharField(help_text="要发送的文本消息内容（可选）", required=False, allow_blank=True)

# 通用的消息/操作响应序列化器
class OperationResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    name = serializers.CharField(required=False)
    error = serializers.CharField(required=False)

# 为 ping 定义响应体的序列化器
class PingResponseSerializer(serializers.Serializer):
    status = serializers.CharField()

# 为 send_file_view 定义请求体的序列化器
class SendFileSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="接收文件的联系人或群聊名称")
    file_path = serializers.CharField(help_text="要发送的文件的绝对路径")

# 为 check_wechat_status 定义响应体的序列化器
class CheckStatusResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    error = serializers.CharField(required=False)

# 为 get_dialogs_view 定义请求体的序列化器
class GetDialogsSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="要获取聊天记录的联系人或群聊名称")
    n_msg = serializers.IntegerField(help_text="要获取的聊天记录条数")

# 为 get_dialogs_view 和 get_dialogs_by_time_blocks_view 定义通用的包含聊天记录的响应序列化器
class DialogsDataResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    dialogs = serializers.JSONField(help_text="聊天记录内容，具体结构依赖于后端实现") # 使用 JSONField 以适应复杂结构
    error = serializers.CharField(required=False)

# 为 get_dialogs_by_time_blocks_view 定义请求体的序列化器
class GetDialogsByTimeBlocksSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="要获取聊天记录的联系人或群聊名称")
    n_time_blocks = serializers.IntegerField(help_text="要获取的时间分块数量")


def home(request):
    return render(request, 'home.html')

# WeChat实例将在需要时动态创建，避免在模块导入时初始化Qt
wechat = None

def get_wechat_instance():
    """
    获取WeChat实例，延迟初始化以避免Qt错误
    """
    global wechat
    if wechat is None:
        try:
            config = WeChatConfig.objects.first()
            if config:
                wechat = WeChat(path=config.path, locale=config.locale)
            else:
                # 使用默认配置创建
                wechat = WeChat(path="C:/Program Files/Tencent/WeChat/WeChat.exe", locale="zh-CN")
        except Exception as e:
            # 如果创建失败，创建一个默认实例
            wechat = WeChat(path="C:/Program Files/Tencent/WeChat/WeChat.exe", locale="zh-CN")
    return wechat

# 创建队列
message_queue = Queue()
file_queue = Queue()
# 创建一个锁
lock = threading.Lock()


# 处理消息队列中的消息
def process_queue():
    while True:
        try:
            name, text, response_queue = message_queue.get()
            try:
                comtypes.CoInitialize()
                with lock:  # 确保微信操作的线程安全
                    success = get_wechat_instance().send_msg(name, text)
                if success:
                    response_queue.put({'status': 'Message sent', 'name': name})
                else:
                    response_queue.put({'status': 'Failed to send message', 'name': name})
            except Exception as e:
                response_queue.put({'status': 'Error sending message', 'name': name, 'error': str(e)})
            message_queue.task_done()
        except Empty:
            pass


# 处理文件队列中的发送文件任务
def process_file_queue():
    while True:
        try:
            name, file_path, response_queue = file_queue.get()
            try:
                comtypes.CoInitialize()
                with lock:  # 确保微信操作的线程安全
                    get_wechat_instance().send_file(name, file_path)
                response_queue.put({'status': 'File sent', 'name': name})
            except Exception as e:
                response_queue.put({'status': 'Error sending file', 'name': name, 'error': str(e)})
            file_queue.task_done()
        except Empty:
            pass


# 启动一个线程来处理文件队列
threading.Thread(target=process_file_queue, daemon=True).start()
# 启动一个线程来处理消息队列
threading.Thread(target=process_queue, daemon=True).start()


@extend_schema(
    summary="发送文本消息给指定联系人或群聊",
    request=SendMessageSerializer,
    responses={
        200: OpenApiResponse(response=OperationResponseSerializer, description='消息发送成功'),
        400: OpenApiResponse(response=OperationResponseSerializer, description='无效的请求参数'),
        500: OpenApiResponse(response=OperationResponseSerializer, description='发送消息失败或发生内部错误')
    },
    tags=['WeChat Actions']
)
@api_view(['POST']) # 添加 @api_view 装饰器
@csrf_exempt
def send_message(request):
    try:
        data = json.loads(request.body) # request.body 仍然可用，或使用 request.data
        name = data['name']
        text = data['text']

        # 用于存储处理结果的队列
        response_queue = Queue()

        # 将消息加入队列
        message_queue.put((name, text, response_queue))

        # 等待处理结果
        result = response_queue.get()

        if result['status'] == 'Message sent':
            return JsonResponse(result, status=200)
        else:
            return JsonResponse(result, status=500)
    except (KeyError, json.JSONDecodeError):
        return JsonResponse({'error': 'Invalid request, missing name or text'}, status=400)


@extend_schema(
    summary="发送文件给指定联系人或群聊",
    request=SendFileSerializer,
    responses={
        200: OpenApiResponse(response=OperationResponseSerializer, description='文件发送成功'),
        400: OpenApiResponse(response=OperationResponseSerializer, description='无效的请求参数'),
        500: OpenApiResponse(response=OperationResponseSerializer, description='发送文件失败或发生内部错误')
    },
    tags=['WeChat Actions']
)
@api_view(['POST'])
@csrf_exempt
def send_file_view(request):
    try:
        data = json.loads(request.body)
        name = data['name']
        file_path = data['file_path']

        # 检查参数
        if not name:
            return JsonResponse({'error': 'Missing name parameter'}, status=400)
        if not file_path or not os.path.exists(file_path):
            return JsonResponse({'error': 'Invalid or missing file_path'}, status=400)

        # 用于存储处理结果的队列
        response_queue = Queue()

        # 将文件发送任务加入文件队列
        file_queue.put((name, file_path, response_queue))

        # 等待处理结果
        result = response_queue.get()

        if result['status'] == 'File sent':
            return JsonResponse(result, status=200)
        else:
            return JsonResponse(result, status=500)
    except (KeyError, json.JSONDecodeError):
        return JsonResponse({'error': 'Invalid request, missing name or file_path'}, status=400)


@extend_schema(
    summary="测试服务是否可用 (Ping)",
    responses={
        200: OpenApiResponse(response=PingResponseSerializer, description='服务可用，返回 pong')
    },
    tags=['Health Check']
)
@api_view(['GET']) # 添加 @api_view 装饰器
@csrf_exempt
def ping(request):
    return JsonResponse({'status': 'pong'})


@extend_schema(
    summary="检查微信状态并尝试防止离线",
    request=None, # POST请求，但无特定请求体
    responses={
        200: OpenApiResponse(response=CheckStatusResponseSerializer, description='微信状态检查并执行防离线操作成功'),
        500: OpenApiResponse(response=CheckStatusResponseSerializer, description='操作发生错误')
    },
    tags=['WeChat Actions']
)
@api_view(['POST'])
@csrf_exempt
def check_wechat_status(request):
    try:
        comtypes.CoInitialize()
        with lock:  # 确保微信操作的线程安全
            get_wechat_instance().prevent_offline()
        return JsonResponse({'status': 'WeChat checked and prevent offline executed'}, status=200)
    except Exception as e:
        return JsonResponse({'status': 'Error', 'error': str(e)}, status=500)


@extend_schema(
    summary="获取指定联系人或群聊的最近N条聊天记录",
    request=GetDialogsSerializer,
    responses={
        200: OpenApiResponse(response=DialogsDataResponseSerializer, description='成功获取聊天记录'),
        400: OpenApiResponse(response=OperationResponseSerializer, description='无效的请求参数'),
        500: OpenApiResponse(response=OperationResponseSerializer, description='获取聊天记录失败或发生内部错误')
    },
    tags=['WeChat Data']
)
@api_view(['POST'])
@csrf_exempt
def get_dialogs_view(request):
    """
    获取指定联系人或群聊的聊天记录
    """
    try:
        # 解析请求体
        data = json.loads(request.body)
        name = data.get('name')  # 联系人或群聊的名称
        n_msg = data.get('n_msg')  # 获取的聊天记录条数，必须指定

        # 检查是否提供了 name 和 n_msg 参数
        if not name:
            return JsonResponse({'error': 'Missing name parameter'}, status=400)

        if n_msg is None: # 明确检查 None，因为 n_msg 可以是0（虽然逻辑上不允许<=0）
            return JsonResponse({'error': 'Missing n_msg parameter'}, status=400)

        # 确保 n_msg 是一个正整数
        try:
            n_msg = int(n_msg)
            if n_msg <= 0:
                raise ValueError("n_msg must be a positive integer")
        except (ValueError, TypeError):
            return JsonResponse({'error': 'n_msg must be a positive integer'}, status=400)

        # 使用全局锁来保证线程安全
        with lock:
            comtypes.CoInitialize()  # 初始化COM接口，防止线程冲突
            dialogs = get_wechat_instance().get_dialogs(name, n_msg)

        # 返回获取到的聊天记录，并禁用ensure_ascii
        return JsonResponse({'status': 'success', 'dialogs': dialogs}, status=200, json_dumps_params={'ensure_ascii': False})

    except Exception as e:
        return JsonResponse({'status': 'error', 'error': str(e)}, status=500)


@extend_schema(
    summary="按时间分块获取指定联系人或群聊的聊天记录",
    request=GetDialogsByTimeBlocksSerializer,
    responses={
        200: OpenApiResponse(response=DialogsDataResponseSerializer, description='成功获取按时间分块的聊天记录'),
        400: OpenApiResponse(response=OperationResponseSerializer, description='无效的请求参数'),
        500: OpenApiResponse(response=OperationResponseSerializer, description='获取聊天记录失败或发生内部错误')
    },
    tags=['WeChat Data']
)
@api_view(['POST'])
@csrf_exempt
def get_dialogs_by_time_blocks_view(request):
    """
    获取指定联系人或群聊的聊天记录，按时间信息分组
    """
    try:
        # 解析请求体
        data = json.loads(request.body)
        name = data.get('name')  # 联系人或群聊的名称
        n_time_blocks = data.get('n_time_blocks')  # 获取的时间分块数量，必须指定

        # 检查是否提供了 name 和 n_time_blocks 参数
        if not name:
            return JsonResponse({'error': 'Missing name parameter'}, status=400)

        if n_time_blocks is None: # 明确检查 None
            return JsonResponse({'error': 'Missing n_time_blocks parameter'}, status=400)

        # 确保 n_time_blocks 是一个正整数
        try:
            n_time_blocks = int(n_time_blocks)
            if n_time_blocks <= 0:
                raise ValueError("n_time_blocks must be a positive integer")
        except (ValueError, TypeError):
            return JsonResponse({'error': 'n_time_blocks must be a positive integer'}, status=400)

        # 使用全局锁来保证线程安全
        with lock:
            comtypes.CoInitialize()  # 初始化COM接口，防止线程冲突
            groups = get_wechat_instance().get_dialogs_by_time_blocks(name, n_time_blocks)

        # 返回获取到的按时间分组的聊天记录，并禁用ensure_ascii
        return JsonResponse({'status': 'success', 'dialogs': groups}, status=200,
                            json_dumps_params={'ensure_ascii': False})

    except Exception as e:
        return JsonResponse({'status': 'error', 'error': str(e)}, status=500)


@extend_schema(
    summary="在群聊中@用户或@所有人，可选发送文本消息",
    request=AtUserSerializer,
    responses={
        200: OpenApiResponse(response=OperationResponseSerializer, description='@用户成功'),
        400: OpenApiResponse(response=OperationResponseSerializer, description='无效的请求参数'),
        500: OpenApiResponse(response=OperationResponseSerializer, description='@用户失败或发生内部错误')
    },
    tags=['WeChat Actions']
)
@api_view(['POST'])
@csrf_exempt
def at_user(request):
    """
    在群聊中@用户或@所有人，可选发送文本消息
    """
    try:
        data = json.loads(request.body)
        name = data.get('name')
        at_name = data.get('at_name')
        text = data.get('text', '')  # 可选参数，默认为空字符串

        if not name:
            return JsonResponse({'status': 'error', 'error': 'Missing name parameter'}, status=400)

        # 创建响应队列
        response_queue = Queue()

        try:
            # 直接调用at方法，不使用队列
            comtypes.CoInitialize()
            with lock:  # 确保微信操作的线程安全
                # 调用修改后的at方法，传入文本参数
                get_wechat_instance().at(name, at_name, search_user=True, text=text)
            return JsonResponse({'status': 'At user success', 'name': name})
        except Exception as e:
            return JsonResponse({'status': 'Error at user', 'name': name, 'error': str(e)}, status=500)

    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'status': 'error', 'error': str(e)}, status=500)


# === 客户端管理相关视图 ===

def client_management(request):
    """
    客户端管理页面
    """
    # 启动客户端管理器
    start_client_manager()
    
    connections = ClientConnection.objects.all().order_by('-last_connected_at')
    
    # 分页
    paginator = Paginator(connections, 10)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    # 获取活跃连接状态
    active_status = client_manager.get_connection_status()
    active_dict = {status['stable_id']: status for status in active_status}
    
    context = {
        'page_obj': page_obj,
        'active_connections': active_dict,
        'total_connections': connections.count(),
        'active_count': len(active_status),
    }
    
    return render(request, 'client_management.html', context)


@csrf_exempt
def create_client_connection(request):
    """
    创建新的客户端连接
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            
            # 验证必需字段
            name = data.get('name', '').strip()
            ip_address = data.get('ip_address', '').strip()
            port = data.get('port')
            connection_key = data.get('connection_key', '').strip()
            
            if not all([name, ip_address, port, connection_key]):
                return JsonResponse({
                    'success': False,
                    'error': '所有字段都是必需的'
                }, status=400)
            
            try:
                port = int(port)
                if port <= 0 or port > 65535:
                    raise ValueError("端口范围无效")
            except (ValueError, TypeError):
                return JsonResponse({
                    'success': False,
                    'error': '端口必须是1-65535之间的数字'
                }, status=400)
            
            # 检查是否已存在相同的连接
            existing = ClientConnection.objects.filter(
                ip_address=ip_address, 
                port=port
            ).first()
            
            if existing:
                return JsonResponse({
                    'success': False,
                    'error': f'连接 {ip_address}:{port} 已存在'
                }, status=400)
            
            # 创建新连接
            connection = ClientConnection.objects.create(
                name=name,
                ip_address=ip_address,
                port=port,
                connection_key=connection_key,
                is_active=True,
                client_version=data.get('client_version', ''),
                client_features=data.get('client_features', [])
            )
            
            return JsonResponse({
                'success': True,
                'message': '客户端连接创建成功',
                'connection_id': str(connection.stable_id),
                'redirect': '/wechat/client-management/'
            })
            
        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': '无效的JSON数据'
            }, status=400)
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': f'创建连接失败: {str(e)}'
            }, status=500)
    
    return JsonResponse({'success': False, 'error': '仅支持POST请求'}, status=405)


@csrf_exempt
def connect_client(request):
    """
    连接到指定客户端
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            connection_id = data.get('connection_id')
            
            if not connection_id:
                return JsonResponse({
                    'success': False,
                    'error': '缺少connection_id参数'
                }, status=400)
            
            connection = get_object_or_404(ClientConnection, stable_id=connection_id)
            
            # 启动客户端管理器
            start_client_manager()
            
            # 尝试连接
            client_manager.connect_client(connection)
            
            return JsonResponse({
                'success': True,
                'message': f'正在连接到 {connection.name}...'
            })
            
        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': '无效的JSON数据'
            }, status=400)
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': f'连接失败: {str(e)}'
            }, status=500)
    
    return JsonResponse({'success': False, 'error': '仅支持POST请求'}, status=405)


@csrf_exempt
def disconnect_client(request):
    """
    断开指定客户端连接
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            connection_id = data.get('connection_id')
            
            if not connection_id:
                return JsonResponse({
                    'success': False,
                    'error': '缺少connection_id参数'
                }, status=400)
            
            connection = get_object_or_404(ClientConnection, stable_id=connection_id)
            
            # 断开连接
            client_manager.disconnect_client(connection)
            
            return JsonResponse({
                'success': True,
                'message': f'已断开与 {connection.name} 的连接'
            })
            
        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': '无效的JSON数据'
            }, status=400)
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': f'断开连接失败: {str(e)}'
            }, status=500)
    
    return JsonResponse({'success': False, 'error': '仅支持POST请求'}, status=405)


@csrf_exempt
def delete_client_connection(request):
    """
    删除客户端连接
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            connection_id = data.get('connection_id')
            
            if not connection_id:
                return JsonResponse({
                    'success': False,
                    'error': '缺少connection_id参数'
                }, status=400)
            
            connection = get_object_or_404(ClientConnection, stable_id=connection_id)
            
            # 先断开连接
            if connection.is_connected:
                client_manager.disconnect_client(connection)
            
            # 删除记录
            connection_name = connection.name
            connection.delete()
            
            return JsonResponse({
                'success': True,
                'message': f'已删除客户端连接 {connection_name}'
            })
            
        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': '无效的JSON数据'
            }, status=400)
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': f'删除连接失败: {str(e)}'
            }, status=500)
    
    return JsonResponse({'success': False, 'error': '仅支持POST请求'}, status=405)


@csrf_exempt
def get_client_status(request):
    """
    获取客户端连接状态
    """
    try:
        # 获取活跃连接状态
        active_status = client_manager.get_connection_status()
        
        # 获取数据库中的所有连接
        all_connections = ClientConnection.objects.all()
        
        connections_data = []
        active_dict = {status['stable_id']: status for status in active_status}
        
        for conn in all_connections:
            conn_id = str(conn.stable_id)
            active_info = active_dict.get(conn_id, {})
            
            connections_data.append({
                'stable_id': conn_id,
                'name': conn.name,
                'ip_address': str(conn.ip_address),
                'port': conn.port,
                'is_active': conn.is_active,
                'is_connected': bool(active_info),  # 实际连接状态
                'created_at': conn.created_at.isoformat(),
                'last_connected_at': conn.last_connected_at.isoformat() if conn.last_connected_at else None,
                'last_heartbeat_at': conn.last_heartbeat_at.isoformat() if conn.last_heartbeat_at else None,
                'reconnect_count': conn.reconnect_count,
                'client_version': conn.client_version,
                'connected_at': active_info.get('connected_at'),
            })
        
        return JsonResponse({
            'success': True,
            'connections': connections_data,
            'total_count': len(connections_data),
            'active_count': len(active_status),
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': f'获取状态失败: {str(e)}'
        }, status=500)


@csrf_exempt
def send_test_command(request):
    """
    向客户端发送测试命令
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            connection_id = data.get('connection_id')
            command = data.get('command', 'ping')
            params = data.get('params', {})
            
            if not connection_id:
                return JsonResponse({
                    'success': False,
                    'error': '缺少connection_id参数'
                }, status=400)
            
            connection = get_object_or_404(ClientConnection, stable_id=connection_id)
            
            # 发送命令
            success, message = send_command_to_client(connection, command, params)
            
            return JsonResponse({
                'success': success,
                'message': message
            })
            
        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': '无效的JSON数据'
            }, status=400)
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': f'发送命令失败: {str(e)}'
            }, status=500)
    
    return JsonResponse({'success': False, 'error': '仅支持POST请求'}, status=405)
