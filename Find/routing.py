from django.urls import re_path
from django.urls import path
from . import consumers

websocket_urlpatterns = [
    # 首頁用（若有）
    re_path(r"^ws/find/$", consumers.FindConsumer.as_asgi()),

    # 司機管理頁（建議主用這條）
    re_path(r"^ws/find/driver/(?P<driver_id>\d+)/$", consumers.DriverManageConsumer.as_asgi()),

    # 兼容你目前前端用的 manage/<id>/ 寫法（避免 404）
    re_path(r"^ws/find/manage/(?P<driver_id>\d+)/$", consumers.DriverManageConsumer.as_asgi()),
]
