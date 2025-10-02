from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"ws/find/$", consumers.FindConsumer.as_asgi()),
    # 新增：司機管理頁—帶 driver_id 的路徑
    # 例：ws/find/driver/85/
    re_path(
        r"^ws/find/driver/(?P<driver_id>\d+)/$",
        consumers.DriverManageConsumer.as_asgi()
    ),
]
