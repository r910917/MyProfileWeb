import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from asgiref.sync import sync_to_async
from django.db import models
from channels.db import database_sync_to_async
from django.template.loader import render_to_string
from .models import PassengerRequest, DriverTrip
from django.db import transaction

class DriverManageConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.driver_id = self.scope["url_route"]["kwargs"]["driver_id"]
        sess_key = f"driver_auth_{self.driver_id}"
        if not self.scope.get("session") or not self.scope["session"].get(sess_key):
            await self.close(code=4403)  # Forbidden
            return
        self.group_name = f"driver_manage_{self.driver_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    # 你後端廣播時用的 type，要對應這個 handler 名稱
    async def manage_panels_update(self, event):
        # 直接把後端送的 payload 回傳給前端
        await self.send_json(event["payload"])

    # 後端用 group_send(type="manage_panels", ...) 會進到這裡
    async def manage_panels(self, event):
        await self.send_json({
            "type": "manage_panels",
            "html": event.get("html", ""),
        })

    async def replace_pax_item(self, event):
        await self.send_json({
            "type": "replace_pax_item",
            "pax_id": event.get("pax_id"),
            "html": event.get("html",""),
        })
class FindConsumer(AsyncWebsocketConsumer):
    
    async def connect(self):
        self.group = "find_group"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await self.send_current_data()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard("find_group", self.channel_name)

    # 刪掉重複/舊的 send_update，只保留這個版本：
    async def send_update(self, event):
        await self.send(text_data=json.dumps({
            "type": "update",
            "drivers_html": event.get("drivers_html", ""),
            "passengers_html": event.get("passengers_html", ""),
            "sort": event.get("sort"),   # ★ 重要：把 sort 往前端帶
        }))

    async def receive(self, text_data=None, bytes_data=None):
        data = json.loads(text_data)
        pass

        if data.get("action") == "join":
            passenger_id = data.get("passenger_id")

            try:
                passenger = await sync_to_async(PassengerRequest.objects.get)(id=passenger_id)
            except PassengerRequest.DoesNotExist:
                await self.send(text_data=json.dumps({
                    "type": "join_result",
                    "success": False,
                    "message": "乘客不存在"
                }))
                return

            driver = await sync_to_async(
                lambda: DriverTrip.objects.filter(is_active=True, seats_filled__lt=models.F("seats_total")).first()
            )()

            if driver:
                passenger.is_matched = True
                driver.seats_filled += passenger.seats_needed

                await sync_to_async(passenger.save)()
                await sync_to_async(driver.save)()

                # 成功回應
                await self.send(text_data=json.dumps({
                    "type": "join_result",
                    "success": True
                }))

                # 廣播最新資料
                await self.channel_layer.group_send(
                    "find_group",
                    {"type": "broadcast_update"}
                )
            else:
                await self.send(text_data=json.dumps({
                    "type": "join_result",
                    "success": False,
                    "message": "目前沒有可用的司機"
                }))
    # Single driver card patch
    async def send_partial(self, event):
        driver_html = event.get("driver_html") or event.get("html") or ""
        payload = event.get("payload", {})
        await self.send(text_data=json.dumps(payload))


     # ---- 將「同步 ORM 查詢」包成 async 可用 ----
    @database_sync_to_async
    def _fetch_lists(self):
        drivers = list(
            DriverTrip.objects.using("find_db")
            .filter(is_active=True)
            .prefetch_related("passengers")      # ★ 預抓乘客，避免模板裡再查 DB
            .order_by("-id")
        )
        passengers = list(
            PassengerRequest.objects.using("find_db")
            .filter(is_matched=False)
            .order_by("-id")
        )
        # 分組；注意：不要用底線開頭的屬性名
        for d in drivers:
            all_pax = list(d.passengers.all())
            d.pending = [p for p in all_pax if not p.is_matched]
            d.accepted = [p for p in all_pax if p.is_matched]
        return drivers, passengers

    @sync_to_async
    def _render_partials(self, drivers, passengers):
        drivers_html = render_to_string("Find/_driver_list.html", {"drivers": drivers})
        passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})
        return drivers_html, passengers_html
    def update_driver(self, driver_id, seats_total, is_active):
        try:
            driver = DriverTrip.objects.using("find_db").get(id=driver_id)
            driver.seats_total = seats_total
            driver.is_active = is_active
            driver.save()
        except DriverTrip.DoesNotExist:
            print(f"⚠️ DriverTrip {driver_id} 不存在")

    async def send_current_data(self):
        passengers_html, drivers_html = await self.render_lists()
        await self.send(text_data=json.dumps({
            "type": "update",
            "passengers_html": passengers_html,
            "drivers_html": drivers_html,
        }))

    async def broadcast_update(self):
        passengers_html, drivers_html = await self.render_lists()
        await self.channel_layer.group_send(
            "find_group",
            {
                "type": "send_update",
                "passengers_html": passengers_html,
                "drivers_html": drivers_html,
            }
        )
        

    @sync_to_async
    def render_lists(self):
        """把查詢和模板渲染放進 sync_to_async"""
        passengers = PassengerRequest.objects.using("find_db").filter(is_matched=False)
        drivers = build_driver_cards()
        html = render_to_string("Find/_driver_list.html", {"drivers": drivers})

        passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})
        drivers_html = render_to_string("Find/_driver_list.html", {"drivers": drivers})

        return passengers_html, drivers_html

# views.py 或 utils.py
def build_driver_cards():
    qs = (DriverTrip.objects.using("find_db")
          .filter(is_active=True)
          .prefetch_related("passengers"))
    drivers = []
    for d in qs:
        plist = list(d.passengers.all())
        d.pending  = [p for p in plist if not p.is_matched]  # 待確認
        d.accepted = [p for p in plist if p.is_matched]      # 已接受
        drivers.append(d)
    return drivers
