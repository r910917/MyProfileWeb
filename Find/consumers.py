import json
from channels.generic.websocket import AsyncWebsocketConsumer
from django.template.loader import render_to_string
from .models import PassengerRequest, DriverTrip
from asgiref.sync import sync_to_async

class FindConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.channel_layer.group_add("find_group", self.channel_name)
        await self.accept()
        await self.send_current_data()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard("find_group", self.channel_name)

    async def receive(self, text_data):
        data = json.loads(text_data)

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



    @sync_to_async
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

    async def send_update(self, event):
        await self.send(text_data=json.dumps({
            "type": "update",
            "passengers_html": event["passengers_html"],
            "drivers_html": event["drivers_html"],
        }))

    @sync_to_async
    def render_lists(self):
        """把查詢和模板渲染放進 sync_to_async"""
        passengers = PassengerRequest.objects.using("find_db").filter(is_matched=False)
        drivers = DriverTrip.objects.using("find_db").filter(is_active=True)

        passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})
        drivers_html = render_to_string("Find/_driver_list.html", {"drivers": drivers})

        return passengers_html, drivers_html
