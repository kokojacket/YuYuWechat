# wechat_app/urls.py

from django.urls import path

from .views import (
    send_message, ping, check_wechat_status, get_dialogs_view, get_dialogs_by_time_blocks_view,
    send_file_view, at_user, client_management, create_client_connection, connect_client,
    disconnect_client, delete_client_connection, get_client_status, send_test_command
)

urlpatterns = [
    # 原有的微信API
    path('ping/', ping, name='ping'),
    path('send_message/', send_message, name='send_message'),
    path('check_wechat_status/', check_wechat_status, name='check_wechat_status'),
    path('get_dialogs/', get_dialogs_view, name='get_dialogs'),
    path('get_dialogs_by_time_blocks/', get_dialogs_by_time_blocks_view, name='get_dialogs_by_time_blocks'),
    path('send_file/', send_file_view, name='send_file'),
    path('at_user/', at_user, name='at_user'),
    
    # 客户端管理相关
    path('client-management/', client_management, name='client_management'),
    path('client-connections/create/', create_client_connection, name='create_client_connection'),
    path('client-connections/connect/', connect_client, name='connect_client'),
    path('client-connections/disconnect/', disconnect_client, name='disconnect_client'),
    path('client-connections/delete/', delete_client_connection, name='delete_client_connection'),
    path('client-connections/status/', get_client_status, name='get_client_status'),
    path('client-connections/test/', send_test_command, name='send_test_command'),
]