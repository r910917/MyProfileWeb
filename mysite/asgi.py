# mysite/asgi.py
import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mysite.settings")

# 先建立一次 Django ASGI app
django_asgi_app = get_asgi_application()

# 注意：一定要在 get_asgi_application() 之後再 import 你的 routing
from Find.routing import websocket_urlpatterns

application = ProtocolTypeRouter({
    # 這裡用上面那個 django_asgi_app，不要再呼叫一次
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(websocket_urlpatterns)
    ),
})
