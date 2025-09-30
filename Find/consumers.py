import json
from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async
from django.template.loader import render_to_string
from .models import PassengerRequest, DriverTrip


class FindConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # 所有人加入同一個 group，方便廣播
        await self.channel_layer.group_add("find_group", self.channel_name)
        await self.accept()

        # 初次連線就推送一次最新資料
        await self.send_current_data()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard("find_group", self.channel_name)

    async def receive(self, text_data):
        # 前端如果有傳資料，可以在這裡處理
        pass

    async def send_current_data(self):
        """整理最新資料並推送給前端 (第一次連線時用)"""

        # ORM 查詢必須用 sync_to_async 包裝
        passengers = await sync_to_async(
            lambda: list(PassengerRequest.objects.using("find_db").filter(is_matched=False))
        )()
        drivers = await sync_to_async(
            lambda: list(DriverTrip.objects.using("find_db").filter(is_active=True))
        )()

        passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})
        drivers_html = render_to_string("Find/_driver_list.html", {"drivers": drivers})

        await self.send(text_data=json.dumps({
            "type": "update",
            "passengers_html": passengers_html,
            "drivers_html": drivers_html,
        }))

    async def send_update(self, event):
        """被 signals 呼叫，用來廣播最新資料"""
        await self.send(text_data=json.dumps({
            "type": "update",
            "passengers_html": event["passengers_html"],
            "drivers_html": event["drivers_html"],
        }))
