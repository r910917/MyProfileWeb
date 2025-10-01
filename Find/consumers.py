import json
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from asgiref.sync import sync_to_async

from django.db.models import Prefetch, Case, When, Value, IntegerField
from django.template.loader import render_to_string

from .models import PassengerRequest, DriverTrip


# ---- 與 views 對齊的排序工具 ----
CITY_N2S = [
    "基隆市","台北市","新北市","桃園市","新竹市","新竹縣","苗栗縣",
    "台中市","彰化縣","南投縣","雲林縣",
    "嘉義市","嘉義縣",
    "台南市","高雄市","屏東縣",
    "宜蘭縣","花蓮縣","台東縣",
    "澎湖縣","金門縣","連江縣",
]

def _dep_rank_case():
    whens = [When(departure=city, then=Value(i)) for i, city in enumerate(CITY_N2S)]
    return Case(*whens, default=Value(999), output_field=IntegerField())

SORT_MAP = {
    "date_desc": ["-date", "-id"],
    "date_asc" : ["date", "id"],
    "dep_asc"  : ["departure", "date", "id"],
    "dep_desc" : ["-departure", "date", "id"],
    "dep_n2s"  : None,  # 特製
    "dep_s2n"  : None,  # 特製
}

def _normalize_sort(s: str | None) -> str:
    s = (s or "").strip() or "date_desc"
    return s if s in SORT_MAP or s in ("dep_n2s", "dep_s2n") else "date_desc"


class FindConsumer(AsyncWebsocketConsumer):
    """
    WebSocket 事件對齊 views 的廣播：
      - send.update: {drivers_html, passengers_html, sort}
      - send.partial: {payload: {type:'driver_partial', driver_id, driver_html, active, sort}}
    連線參數：ws://.../find?sort=date_desc
    """

    async def connect(self):
        # 讀取目前排序（預設 date_desc）
        qs = parse_qs(self.scope.get("query_string", b"").decode())
        self.sort = _normalize_sort((qs.get("sort", ["date_desc"]) or ["date_desc"])[0])

        # 群組：共用 + 依排序
        self.common_group = "find_group"
        self.sort_group = f"find:{self.sort}"

        await self.channel_layer.group_add(self.common_group, self.channel_name)
        await self.channel_layer.group_add(self.sort_group, self.channel_name)
        await self.accept()

        # 初次回填當前列表（避免等下一次廣播才看到畫面）
        drivers_html, passengers_html = await self._render_lists(self.sort)
        await self.send(text_data=json.dumps({
            "type": "update",
            "drivers_html": drivers_html,
            "passengers_html": passengers_html,
            "sort": self.sort,
        }))

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.common_group, self.channel_name)
        await self.channel_layer.group_discard(self.sort_group, self.channel_name)

    # ========== 事件處理（與 views 對齊） ==========

    async def send_update(self, event):
        """
        由 views.broadcast_lists(sort) 觸發。
        event: {"drivers_html": "...", "passengers_html": "...", "sort": "date_desc"}
        """
        sort = _normalize_sort(event.get("sort"))
        # 若你想對不同排序的訊息做過濾，可開啟下面這行：
        # if sort != self.sort: return

        await self.send(text_data=json.dumps({
            "type": "update",
            "drivers_html": event.get("drivers_html") or "",
            "passengers_html": event.get("passengers_html") or "",
            "sort": sort,
        }))

    async def send_partial(self, event):
        """
        由 views.broadcast_driver_card(driver_id, sort) 或 delete_driver() 觸發。
        event 可能有：
          - {"payload": {...}}  # 推薦格式
          - 或舊格式 {"driver_id": ..., "html": "..."}（已相容）
        """
        payload = event.get("payload")
        if not payload:
            # 相容舊格式
            payload = {
                "type": "driver_partial",
                "driver_id": event.get("driver_id"),
                "driver_html": event.get("driver_html") or event.get("html") or "",
                "active": event.get("active", True),
                "sort": _normalize_sort(event.get("sort")),
            }

        # 若想依排序過濾：
        # if _normalize_sort(payload.get("sort")) != self.sort:
        #     return

        await self.send(text_data=json.dumps(payload))

    # ========== 前端自發請求（可選） ==========

    async def receive(self, text_data=None, bytes_data=None):
        """
        支援三種 action（可按需使用）：
          - {"action":"refresh"}       -> 立即回傳目前排序的整包列表
          - {"action":"set_sort","sort":"date_asc"} -> 動態切換排序群組並回填
          - {"action":"ping"}          -> 心跳
        """
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except Exception:
            return

        action = data.get("action")
        if action == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))
            return

        if action == "refresh":
            drivers_html, passengers_html = await self._render_lists(self.sort)
            await self.send(text_data=json.dumps({
                "type": "update",
                "drivers_html": drivers_html,
                "passengers_html": passengers_html,
                "sort": self.sort,
            }))
            return

        if action == "set_sort":
            new_sort = _normalize_sort(data.get("sort"))
            if new_sort == self.sort:
                return
            # 退舊群組、加新群組
            await self.channel_layer.group_discard(self.sort_group, self.channel_name)
            self.sort = new_sort
            self.sort_group = f"find:{self.sort}"
            await self.channel_layer.group_add(self.sort_group, self.channel_name)

            # 回填新排序的列表
            drivers_html, passengers_html = await self._render_lists(self.sort)
            await self.send(text_data=json.dumps({
                "type": "update",
                "drivers_html": drivers_html,
                "passengers_html": passengers_html,
                "sort": self.sort,
            }))
            return

    # ========== 查詢＋渲染（包成 async） ==========

    @database_sync_to_async
    def _fetch_lists(self, sort: str):
        """讀 DB（使用與 views 相同的預取策略與排序）。"""
        order_by = SORT_MAP.get(sort)
        pending_qs  = PassengerRequest.objects.using("find_db").filter(is_matched=False).order_by("-id")
        accepted_qs = PassengerRequest.objects.using("find_db").filter(is_matched=True).order_by("-id")

        drivers = DriverTrip.objects.using("find_db").filter(is_active=True)

        if sort == "dep_n2s":
            drivers = drivers.annotate(dep_rank=_dep_rank_case()).order_by("dep_rank", "date", "id")
        elif sort == "dep_s2n":
            drivers = drivers.annotate(dep_rank=_dep_rank_case()).order_by("-dep_rank", "date", "id")
        elif order_by:
            drivers = drivers.order_by(*order_by)
        else:
            drivers = drivers.order_by("-date", "-id")

        drivers = drivers.prefetch_related(
            Prefetch("passengers", queryset=pending_qs,  to_attr="pending"),
            Prefetch("passengers", queryset=accepted_qs, to_attr="accepted"),
        )

        passengers = PassengerRequest.objects.using("find_db")\
            .filter(is_matched=False, driver__isnull=True)\
            .order_by("-id")

        # 轉 list 以避免模板二次 hit DB
        return list(drivers), list(passengers)

    @sync_to_async
    def _render_to_strings(self, drivers, passengers):
        drivers_html    = render_to_string("Find/_driver_list.html", {"drivers": drivers})
        passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})
        return drivers_html, passengers_html

    async def _render_lists(self, sort: str):
        """整包：查詢 + 模板渲染 -> (drivers_html, passengers_html)"""
        drivers, passengers = await self._fetch_lists(sort)
        return await self._render_to_strings(drivers, passengers)
