from decimal import Decimal, InvalidOperation
from collections.abc import Sequence
from datetime import date as _date

from django.http import JsonResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.http import JsonResponse, HttpResponseForbidden, Http404
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.db import transaction, connection
from django.views.decorators.http import require_POST
from django.template.loader import render_to_string
from django.utils.dateparse import parse_date
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.csrf import csrf_protect
import json

from django.db.models import (
    Prefetch, Q, F, Case, When, Value, IntegerField, ExpressionWrapper, Count, Func
)
from django.db.models.functions import Cast, Coalesce, NullIf, Replace, Trim

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from .models import DriverTrip, PassengerRequest
from django.core.exceptions import ValidationError

import re
from django.db.models import CharField

try:
    from django.contrib.postgres.functions import RegexpReplace as PGRegexpReplace  # type: ignore
except Exception:  # 不是 Postgres 就沒有
    PGRegexpReplace = None
DB_ALIAS = "find_db"  # 依你的設定

def _json_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or "{}")
    except Exception:
        return {}

def _driver_authed(request, driver: DriverTrip) -> bool:
    """
    授權規則：
    1) 已在 driver_auth 成功 → session 有 driver_auth_<id>=True，直接通過
    2) 沒有 session 時，允許一次性提供密碼（Header 或表單或 JSON）：
       - Header: X-Driver-Password
       - POST form: password
       - JSON body: {"password": "..."}
    """
    key = f"driver_auth_{driver.id}"
    if request.session.get(key, False):
        return True

    pw = request.headers.get("X-Driver-Password") or request.POST.get("password")

    # 可能是 JSON
    if not pw and request.method == "POST" and request.content_type.startswith("application/json"):
        try:
            body = json.loads(request.body.decode("utf-8") or "{}")
            pw = body.get("password")
        except Exception:
            pw = None

    if not pw:
        return False

    return constant_time_compare((pw or "").strip(), (driver.password or "").strip())

def _repaint_lists():
    """
    若你要像 join_driver 一樣同時刷新列表（drivers_html / passengers_html），
    可以把那段重算 + render_to_string 搬過來呼叫。
    這裡先做 NO-OP，保留 broadcast_driver_card 即時更新卡片即可。
    """
    pass

def _manage_payload(driver, pax):
    status = "accepted" if pax.is_matched else "pending"
    html = render_to_string("Find/_driver_manage_pax_item.html", {"p": pax})
    return {"ok": True, "driver_id": driver.id, "pax_id": pax.id, "status": status, "html": html}

def broadcast_full_lists():
    pending_qs  = PassengerRequest.objects.using("find_db").filter(is_matched=False).order_by("-id")
    accepted_qs = PassengerRequest.objects.using("find_db").filter(is_matched=True ).order_by("-id")

    d = (
        DriverTrip.objects.using("find_db")
        .filter(is_active=True)
        .prefetch_related(
            Prefetch("passengers", queryset=pending_qs,  to_attr="pending_list"),
            Prefetch("passengers", queryset=accepted_qs, to_attr="accepted_list"),
        )
    )

    passengers = PassengerRequest.objects.using("find_db").filter(
        is_matched=False, driver__isnull=True
    ).order_by("-id")

    drivers_html    = render_to_string("Find/_driver_list.html",    {"drivers": d})
    passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})

    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        "find_group",
        {
            "type": "send.update",
            "drivers_html": drivers_html,
            "passengers_html": passengers_html,
        },
    )
def broadcast_driver_card(driver_id: int):
    channel_layer = get_channel_layer()
    d = DriverTrip.objects.using("find_db").filter(id=driver_id).first()
    driver = (driver_cards_qs(only_active=False)
              .filter(id=driver_id)
              .first())
    if not driver:
        # 可回傳移除卡片的訊息（如果被下架/刪除）
        async_to_sync(channel_layer.group_send)("find_group", {
            "payload": {
                    "type": "driver_partial",
                    "driver_id": driver_id,
                    "driver_html": "",
                    "active": False,
                },
        })
        return
    # 準備這張卡片需要的 pending / accepted
    pending_qs  = PassengerRequest.objects.using("find_db").filter(driver_id=driver_id, is_matched=False).order_by("-id")
    accepted_qs = PassengerRequest.objects.using("find_db").filter(driver_id=driver_id, is_matched=True ).order_by("-id")

    # 兩種作法：要嘛 prefetch 到 driver，要嘛直接掛暫時屬性給模板用
    driver.pending  = list(pending_qs)
    driver.accepted = list(accepted_qs)

    # 準備這張卡片需要的兩個清單（對應模板的 d.pending_list / d.accepted_list）
    d.pending_list  = list(
        PassengerRequest.objects.using("find_db")
        .filter(driver_id=driver_id, is_matched=False)
        .order_by("-id")
    )
    d.accepted_list = list(
        PassengerRequest.objects.using("find_db")
        .filter(driver_id=driver_id, is_matched=True)
        .order_by("-id")
    )

    # 渲染「單一卡片模板」(下一節會給)
    driver_html = render_to_string("Find/_driver_card.html", {"d": d})

    # 廣播
    async_to_sync(channel_layer.group_send)(
        "find_group",
        {
            "type": "send.partial",
            "payload": {
                "type": "driver_partial",
                "driver_id": d.id,
                "driver_html": driver_html,
                "active": bool(d.is_active),
            },
        },
    )

    html = render_to_string("Find/_driver_card.html", {"d": driver})
    async_to_sync(channel_layer.group_send)("find_group", {
        "type": "send.partial",
        "driver_id": driver_id,
        "html": html,
    })


def _broadcast_lists():
    channel_layer = get_channel_layer()

    pending_qs  = PassengerRequest.objects.using("find_db").filter(is_matched=False).order_by("-id")
    accepted_qs = PassengerRequest.objects.using("find_db").filter(is_matched=True ).order_by("-id")

    d = (
        DriverTrip.objects.using("find_db")
        .filter(is_active=True)
        .prefetch_related(
            Prefetch("passengers", queryset=pending_qs,  to_attr="pending_list"),
            Prefetch("passengers", queryset=accepted_qs, to_attr="accepted_list"),
        )
    )

    passengers = PassengerRequest.objects.using("find_db").filter(
        is_matched=False, driver__isnull=True
    ).order_by("-id")

    drivers_html    = render_to_string("Find/_driver_list.html",    {"drivers": d})
    passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})

    async_to_sync(channel_layer.group_send)(
        "find_group",
        {"type": "send.update", "drivers_html": drivers_html, "passengers_html": passengers_html},
    )
def broadcast_full_update():
    """把 drivers / passengers 兩個片段一起廣播出去（所有使用者即時更新）"""
    channel_layer = get_channel_layer()

    # 乘客快取 queryset
    pending_qs  = PassengerRequest.objects.using("find_db").filter(is_matched=False).order_by("-id")
    accepted_qs = PassengerRequest.objects.using("find_db").filter(is_matched=True).order_by("-id")

    # 只有上架中的司機要顯示
    d = (
        DriverTrip.objects.using("find_db")
        .filter(is_active=True)
        .prefetch_related(
            Prefetch("passengers", queryset=pending_qs,  to_attr="pending_list"),
            Prefetch("passengers", queryset=accepted_qs, to_attr="accepted_list"),
        )
        .order_by("-id")
    )

    # 尚未媒合、未指派司機的乘客
    passengers = (
        PassengerRequest.objects.using("find_db")
        .filter(is_matched=False, driver__isnull=True)
        .order_by("-id")
    )

    drivers_html = render_to_string("Find/_driver_list.html", {"drivers": d})
    passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})

    # 傳給同一個 group（你的 consumer 會把它包成 {"type":"update", ...} 給前端）
    async_to_sync(channel_layer.group_send)(
        "find_group",
        {
            "type": "send.update",          # 對應 consumer 的 handler，例如 async def send_update(...)
            "drivers_html": drivers_html,
            "passengers_html": passengers_html,
        },
    )
def broadcast_update(message):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        "find_updates",
        {"type": "send_update", "message": message}
    )


def _broadcast_manage(driver, pax):
    channel_layer = get_channel_layer()
    payload = _manage_payload(driver, pax)
    async_to_sync(channel_layer.group_send)(
        f"manage_driver_{driver.id}",
        {"type": "manage.pax", "payload": payload},
    )
def broadcast_driver_manage(driver_id, payload):
    layer = get_channel_layer()
    async_to_sync(layer.group_send)(
        f"driver_manage_{driver_id}",
        {"type": "driver.manage.update", "payload": payload}
    )

def broadcast_manage_panels(driver_id: int):
    """給司機管理頁用：推送待確認／已接受兩個 UL 的整段 HTML"""
    from .models import DriverTrip, PassengerRequest
    d = DriverTrip.objects.using(DB_ALIAS).get(pk=driver_id)
    pending_qs = (PassengerRequest.objects.using(DB_ALIAS)
                  .filter(driver=d, is_matched=False)
                  .order_by("-id"))

    accepted_qs = (PassengerRequest.objects.using(DB_ALIAS)
                   .filter(driver=d, is_matched=True)
                   .order_by("-id"))

    html = render_to_string(
        "Find/_manage_panels.html",
        {"driver": d, "pending": pending_qs, "accepted": accepted_qs},
    )

    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"driver_{driver_id}",
        {"type": "manage.panels", "html": html},
    )

@require_POST
@transaction.atomic(using=DB_ALIAS)
def pax_accept(request, pax_id: int):
    """司機接受乘客：若已接受則視為 idempotent。"""
    p = get_object_or_404(PassengerRequest.objects.using(DB_ALIAS), id=pax_id)

    # 需有掛載司機
    if not p.driver_id:
        return JsonResponse({"ok": False, "error": "NO_DRIVER"}, status=400)

    # 鎖定司機行程、驗證授權
    d = DriverTrip.objects.using(DB_ALIAS).select_for_update().get(id=p.driver_id)
    if not _driver_authed(request, d):
        return JsonResponse({"ok": False, "error": "FORBIDDEN"}, status=403)

    

    # 尚未接受 → 檢查座位、寫入
    remaining = max(0, d.seats_total - d.seats_filled)
    if remaining < p.seats_needed:
        return JsonResponse({"ok": False, "error": "FULL", "remaining": remaining}, status=400)

    if not p.is_matched:
        d.seats_filled += p.seats_needed
        p.is_matched = True
        d.save(using=DB_ALIAS, update_fields=["seats_filled"])
        p.save(using=DB_ALIAS, update_fields=["is_matched"])

    transaction.on_commit(lambda: (
        broadcast_driver_card(d.id),
        broadcast_manage_panels(d.id),
    ))
    return JsonResponse({"ok": True, "remaining": max(0, d.seats_total - d.seats_filled)})


@require_POST
@transaction.atomic(using=DB_ALIAS)
def pax_reject(request, pax_id: int):
    """司機拒絕/取消乘客：若原本已接受需釋放座位，並從司機底下移除。"""
    p = get_object_or_404(PassengerRequest.objects.using(DB_ALIAS), id=pax_id)
    d = DriverTrip.objects.using(DB_ALIAS).select_for_update().get(id=p.driver_id) if p.driver_id else None

    # ✅ 一樣用 JsonResponse
    if d and not _driver_authed(request, d):
        return JsonResponse({"ok": False, "error": "FORBIDDEN"}, status=403)

    if p.is_matched and d:
        d.seats_filled = max(0, d.seats_filled - p.seats_needed)
        d.save(using=DB_ALIAS, update_fields=["seats_filled"])

    # 從司機底下移除、回到未媒合
    p.driver = None
    p.is_matched = False
    p.save(using=DB_ALIAS, update_fields=["driver", "is_matched"])

    transaction.on_commit(lambda: (
        d and broadcast_driver_card(d.id),
        d and broadcast_manage_panels(d.id),
    ))
    return JsonResponse({"ok": True})

@require_POST
def pax_memo(request, pax_id: int):
    # 1) 取乘客 + 所屬 driver
    p = get_object_or_404(
        PassengerRequest.objects.using("find_db").select_related("driver"),
        id=pax_id
    )
    d = p.driver
    if not d:
        raise Http404("no driver")

    # 2) 驗證司機授權（沿用你的 session key）
    sess_key = f"driver_auth_{d.id}"
    if not request.session.get(sess_key):
        return HttpResponseForbidden("未授權")

    # 3) 寫入備忘錄（限制長度）
    memo = (request.POST.get("memo") or "").strip()
    if len(memo) > 2000:
        memo = memo[:2000]
    p.driver_memo = memo or None
    p.save(using="find_db", update_fields=["driver_memo"])

    # 4) 準備管理頁要用的 payload（單一卡片 HTML + 狀態）
    status = "accepted" if p.is_matched else "pending"
    item_html = render_to_string("Find/_driver_manage_pax_item.html", {"p": p})
    payload = {
        "ok": True,
        "driver_id": d.id,
        "pax_id": p.id,
        "status": status,     # 'pending' | 'accepted'
        "html": item_html,    # 單一 <li> 片段
        "memo": p.driver_memo # 也回傳 memo 方便前端直接更新 data-current
    }

    # 5) 廣播到管理頁（讓 driver_manage 即時替換該卡片）
    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"manage_driver_{d.id}",
            {"type": "manage.pax", "payload": payload},
        )
    except Exception:
        pass

    # 6) 同步刷新公開列表的司機卡（你現有的）
    try:
        broadcast_driver_card(d.id)
        broadcast_manage_panels(p.driver_id)
    except Exception:
        pass

    # 7) 回傳 payload，讓本頁直接就地替換
    return JsonResponse(payload)


@require_POST
def driver_toggle_privacy(request, driver_id:int):
    d = get_object_or_404(DriverTrip.objects.using("find_db"), id=driver_id)
    hide = (request.POST.get("hide") or "").lower() in ("1","true","yes","on")
    if hide and not d.email:
        return JsonResponse({"ok": False, "error": "要隱藏聯絡方式，必須先填寫 Email"}, status=400)
    d.hide_contact = hide
    try:
        d.full_clean()
    except ValidationError as e:
        return JsonResponse({"ok": False, "error": "; ".join(sum(e.message_dict.values(), []))}, status=400)
    d.save(using="find_db")
    try:
        broadcast_driver_card(driver_id)
    except Exception:
        pass
    return JsonResponse({"ok": True, "hide": d.hide_contact})

@require_POST
def pax_toggle_privacy(request, pax_id:int):
    p = get_object_or_404(PassengerRequest.objects.using("find_db"), id=pax_id)
    hide = (request.POST.get("hide") or "").lower() in ("1","true","yes","on")
    if hide and not p.email:
        return JsonResponse({"ok": False, "error": "要隱藏聯絡方式，必須先填寫 Email"}, status=400)
    p.hide_contact = hide
    try:
        p.full_clean()
    except ValidationError as e:
        return JsonResponse({"ok": False, "error": "; ".join(sum(e.message_dict.values(), []))}, status=400)
    p.save(using="find_db")
    # 你若有 WS 廣播可選擇通知
    try:
        if p.driver_id:
            broadcast_driver_card(p.driver_id)
    except Exception:
        pass
    return JsonResponse({"ok": True, "hide": p.hide_contact})


@require_POST
def driver_pax_memo(request, driver_id:int, pax_id:int):
    p = get_object_or_404(
        PassengerRequest.objects.using("find_db"),
        id=pax_id, driver_id=driver_id
    )
    memo = (request.POST.get("memo") or "").strip()
    p.driver_memo = memo
    p.full_clean()  # 保險
    p.save(using="find_db")

    # 局部重繪該司機卡片（如果你已有）
    try:
        broadcast_driver_card(driver_id)
    except Exception:
        pass

    return JsonResponse({"ok": True})


def fare_text_to_int(field_name: str = "fare_note"):
    """
    回傳一個可用於 annotate 的表達式：
    - 從文字欄位抽出所有阿拉伯數字（移除非 0-9），轉成整數
    - 抽不到數字 -> 變成 NULL
    - 盡量跨 DB：Postgres/MySQL 用 REGEXP_REPLACE；SQLite 用多重 Replace 做近似清理
    """
    vendor = connection.vendor  # 'postgresql' | 'mysql' | 'sqlite' | 'oracle'...

    if vendor == "postgresql" and PGRegexpReplace is not None:
        # 把非數字全部清成空字串
        cleaned = PGRegexpReplace(F(field_name), r"[^0-9]+", Value(""))
    elif vendor == "mysql":
        # MySQL 8 有 REGEXP_REPLACE
        cleaned = Func(F(field_name), Value(r"[^0-9]+"), Value(""), function="REGEXP_REPLACE")
    else:
        # SQLite / 其他：盡量把常見符號先清掉（$、NT、NT$、NTD、元、逗號、空白等）
        cleaned = Replace(
            Replace(
                Replace(
                    Replace(
                        Replace(
                            Replace(
                                Replace(Trim(F(field_name)), Value("NT$"), Value("")),
                                Value("NTD"), Value(""),
                            ),
                            Value("NT"), Value(""),
                        ),
                        Value("$"), Value(""),
                    ),
                    Value("元"), Value(""),
                ),
                Value(","), Value(""),
            ),
            Value(" "), Value(""),
        )
        # 注意：SQLite 沒有 regex，只能先把常見符號去掉；如果還有其它字，Cast 會失敗 -> 我們用 NullIf 處理

    # 變空字串時 -> NULL；然後 Cast 成整數
    numeric_or_empty = NullIf(cleaned, Value(""))
    as_int = Cast(numeric_or_empty, IntegerField())
    # 不要用 0 填補，讓「免費」等無數字的直接變成 NULL；數字篩選時自然被排除
    return as_int  # 交給外層用 Coalesce 或直接拿來做條件

CITY_N2S = [
    "需清淤地區","光復鄉糖廠","花蓮縣光復車站以外火車站","花蓮縣光復鄉","基隆市","台北市","新北市","桃園市","新竹市","新竹縣","苗栗縣",
    "台中市","彰化縣","南投縣","雲林縣",
    "嘉義市","嘉義縣",
    "台南市","高雄市","屏東縣",
    "宜蘭縣","花蓮縣","台東縣",
    "澎湖縣","金門縣","連江縣",
]

def _city_rank_case(field: str = "departure") -> Case:
    """將欄位值（城市名）轉為排序權重。"""
    whens = [When(**{field: name}, then=Value(idx)) for idx, name in enumerate(CITY_N2S)]
    return Case(*whens, default=Value(999), output_field=IntegerField())

def get_active_location_choices():
    """從已上架司機中抓『出發地 / 目的地』的候選值（去空白、去重），並提供數量。"""
    base = DriverTrip.objects.using("find_db").filter(is_active=True)

    dep_qs = (
        base.exclude(departure__isnull=True)
            .exclude(departure__exact="")
            .values("departure")
            .annotate(n=Count("id"))
            .annotate(rank=_city_rank_case("departure"))
            .order_by("rank", "departure")
    )
    des_qs = (
        base.exclude(destination__isnull=True)
            .exclude(destination__exact="")
            .values("destination")
            .annotate(n=Count("id"))
            .annotate(rank=_city_rank_case("destination"))
            .order_by("rank", "destination")
    )

    dep_choices = [row["departure"] for row in dep_qs]
    des_choices = [row["destination"] for row in des_qs]

    # 如果你想在前端顯示數量，可一併傳過去
    dep_with_count = [(row["departure"], row["n"]) for row in dep_qs]
    des_with_count = [(row["destination"], row["n"]) for row in des_qs]
    return dep_choices, des_choices, dep_with_count, des_with_count

def _dep_rank_case():
    whens = [When(departure=city, then=Value(i)) for i, city in enumerate(CITY_N2S)]
    return Case(*whens, default=Value(999), output_field=IntegerField())

def get_order_by(sort: str | None) -> list[str]:
    order_map = {
        "date_desc": ["-date", "id"],
        "date_asc" : ["date", "id"],
        "dep_asc"  : ["departure", "date", "id"],
        "dep_desc" : ["-departure", "date", "id"],
    }
    return order_map.get((sort or "").strip() or "date_desc", ["-date", "id"])

def _getlist_qs(request, key: str) -> list[str]:
    """GET 支援單值或多選陣列（key 或 key[] 都吃）。"""
    vals = request.GET.getlist(key) or request.GET.getlist(f"{key}[]")
    return [v for v in (vals or []) if str(v).strip()]

def _parse_int(val, default=None):
    try:
        return int(str(val).strip())
    except Exception:
        return default


def create_driver(request):
    # ... validate & save
    driver = DriverTrip.objects.using("find_db").create(...)
    broadcast_driver_card(driver.id)  # or broadcast_full_lists()
    return redirect("find_index")


def driver_cards_qs(
    *, 
    only_active: bool = True,
    order_by: list[str] | None = None,
    sort: str | None = None,
    filters: dict | None = None,
):
    """
    回傳已帶好 passengers 的 DriverTrip QuerySet：
      - d.pending_list：未媒合乘客
      - d.accepted_list：已媒合乘客

    filters 支援 keys：
      dep_in, des_in, date_in, ret_in, gender_in (list)
      need_seats (int)
      fare_num (int), fare_mode ('lte'|'gte'), fare_q (str)
    """
    DB_ALIAS = "find_db"

    # 1) 起手式：一定要有初值，避免 UnboundLocalError
    qs = DriverTrip.objects.using(DB_ALIAS).all()
    if only_active:
        qs = qs.filter(is_active=True)

    # 2) 篩選
    f = filters or {}
    dep_in     = tuple(sorted(set(f.get("dep_in")    or [])))
    des_in     = tuple(sorted(set(f.get("des_in")    or [])))
    date_in    = tuple(sorted(set(f.get("date_in")   or [])))
    ret_in     = tuple(sorted(set(f.get("ret_in")    or [])))
    gender_in  = tuple(sorted(set(f.get("gender_in") or [])))
    need_seats = f.get("need_seats", None)
    fare_num   = f.get("fare_num", None)
    fare_mode  = (f.get("fare_mode") or "").strip()          # 'lte'|'gte'
    fare_q     = (f.get("fare_q") or "").strip()

    if dep_in:    qs = qs.filter(departure__in=dep_in)
    if des_in:    qs = qs.filter(destination__in=des_in)
    if date_in:   qs = qs.filter(date__in=date_in)
    if ret_in:    qs = qs.filter(return_date__in=ret_in)
    if gender_in: qs = qs.filter(gender__in=gender_in)

    # 可用座位（剩餘座位 >= 需求）
    if need_seats is not None:
        qs = qs.annotate(
            available=ExpressionWrapper(F("seats_total") - F("seats_filled"), output_field=IntegerField())
        )

    # 酌收費用：數字優先；否則退回關鍵字（避免 CAST 在不同 DB 行為不一致）
    if fare_num is not None and fare_mode in ("lte", "gte"):
        if hasattr(DriverTrip, "fare_amount"):
            fld = "fare_amount"
            comp = f"{fld}__{fare_mode}"
            qs = qs.filter(**{f"{fld}__isnull": False, comp: fare_num})
        else:
            # 沒有數字欄位時，不做 CAST（跨 DB 不穩）；交由關鍵字或忽略
            pass
    if fare_q:
        if hasattr(DriverTrip, "fare_note"):
            qs = qs.filter(fare_note__icontains=fare_q)

    # ---- 整卡片關鍵字搜尋：對常見欄位 + 關聯乘客欄位做 icontains，並對每個詞做 AND 疊加 ----
    qkw = (f.get("q") or "").strip()
    if qkw:
        # 以空白切成多個詞，逐字 AND 篩（使用 .distinct() 避免 join 重覆）
        terms = [t for t in re.split(r"\s+", qkw) if t]
        for t in terms:
            qs = qs.filter(
                Q(driver_name__icontains=t) |
                Q(contact__icontains=t) |
                Q(email__icontains=t) |
                Q(departure__icontains=t) |
                Q(destination__icontains=t) |
                Q(note__icontains=t) |
                Q(fare_note__icontains=t) |
                Q(flexible_pickup__icontains=t) |
                Q(passengers__passenger_name__icontains=t) |
                Q(passengers__note__icontains=t)
            )
        # 若使用者輸入像日期的字串（YYYY-MM-DD），嘗試也比對日期等於
        if len(qkw) <= 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", qkw):
            qs = qs.filter(Q(date=qkw) | Q(return_date=qkw)) | qs

        qs = qs.distinct()

    # 3) 排序：order_by 優先，其次由 sort 決定
    if order_by:
        qs = qs.order_by(*order_by)
    else:
        if sort == "dep_n2s":
            qs = qs.annotate(dep_rank=_city_rank_case("departure")).order_by("dep_rank", "date", "id")
        elif sort == "dep_s2n":
            qs = qs.annotate(dep_rank=_city_rank_case("departure")).order_by("-dep_rank", "date", "id")
        elif sort == "dep_asc":
            qs = qs.order_by("departure", "id")
        elif sort == "dep_desc":
            qs = qs.order_by("-departure", "id")
        elif sort == "date_asc":
            qs = qs.order_by("date", "id")
        else:
            qs = qs.order_by("-date", "-id")

    # 4) passengers 預抓（to_attr）
    pending_qs  = PassengerRequest.objects.using(DB_ALIAS).filter(is_matched=False).order_by("-id")
    accepted_qs = PassengerRequest.objects.using(DB_ALIAS).filter(is_matched=True).order_by("-id")

    return qs.prefetch_related(
        Prefetch("passengers", queryset=pending_qs,  to_attr="pending_list"),
        Prefetch("passengers", queryset=accepted_qs, to_attr="accepted_list"),
    )
# ---- 共用：取單一司機，並帶 pending/accepted 兩個清單 ----
def fetch_driver_with_lists(driver_id: int):
    pending_qs  = PassengerRequest.objects.using("find_db").filter(is_matched=False).order_by("id")
    accepted_qs = PassengerRequest.objects.using("find_db").filter(is_matched=True).order_by("id")
    d = (
        DriverTrip.objects.using("find_db")
        .filter(id=driver_id)
        .prefetch_related(
            Prefetch("passengers", queryset=pending_qs,  to_attr="pending"),
            Prefetch("passengers", queryset=accepted_qs, to_attr="accepted"),
        )
        .first()
    )
    if not d:
        return None, [], []
    # 確保都有屬性且是 list（就算空也給空 list）
    pending_list  = list(getattr(d, "pending",  []))
    accepted_list = list(getattr(d, "accepted", []))
    return d, pending_list, accepted_list



# 統一的 session key
SESSION_PAX = "pax_auth_{}"

def _pax_authorized(request, pid: int) -> bool:
    return request.session.get(SESSION_PAX.format(pid)) is True

@require_POST
def pax_auth(request, pid: int):
    """乘客編輯前的密碼驗證（設定 session 授權標記）。"""
    p = get_object_or_404(PassengerRequest.objects.using("find_db"), id=pid)
    password = request.POST.get("password", "")
    if not password:
        return JsonResponse({"ok": False, "error": "請輸入密碼"}, status=400)

    if password != p.password:
        return JsonResponse({"ok": False, "error": "密碼錯誤"}, status=403)

    request.session[SESSION_PAX.format(pid)] = True
    request.session.modified = True
    return JsonResponse({"ok": True})

def pax_get(request, pid: int):
    """回傳乘客資料（需要先通過 pax_auth）。"""
    p = get_object_or_404(PassengerRequest.objects.using("find_db"), id=pid)
    if not _pax_authorized(request, pid):
        return JsonResponse({"ok": False, "error": "未授權"}, status=403)

    data = {
        "id": p.id,
        "passenger_name": p.passenger_name,
        "gender": p.gender,
        "email": p.email or "",
        "contact": p.contact or "",
        "seats_needed": p.seats_needed,
        "willing_to_pay": str(p.willing_to_pay) if p.willing_to_pay is not None else "",
        "departure": p.departure or "",
        "destination": p.destination or "",
        "date": p.date.isoformat() if p.date else "",
        "return_date": p.return_date.isoformat() if p.return_date else "",
        "together_return": "" if p.together_return is None else ("true" if p.together_return else "false"),
        "note": p.note or "",
    }
    return JsonResponse({"ok": True, "p": data})

# 取得單一乘客資料（給編輯 Modal 預填）
def passenger_json(request, pid: int):
    p = get_object_or_404(PassengerRequest.objects.using("find_db"), id=pid)
    data = {
        "id": p.id,
        "passenger_name": p.passenger_name,
        "gender": p.gender,
        "email": p.email or "",
        "contact": p.contact or "",
        "seats_needed": p.seats_needed,
        "willing_to_pay": str(p.willing_to_pay or ""),
        "departure": p.departure or "",
        "destination": p.destination or "",
        "date": p.date.isoformat() if p.date else "",
        "return_date": p.return_date.isoformat() if p.return_date else "",
        "together_return": None if p.together_return is None else bool(p.together_return),
        "note": p.note or "",
        "driver_id": p.driver_id,
        "is_matched": p.is_matched,
    }
    return JsonResponse({"ok": True, "data": data})

@require_POST
@transaction.atomic(using=DB_ALIAS)
def pax_update(request, pid: int):
    """更新乘客資料（需要先通過 pax_auth）。"""
    p = get_object_or_404(PassengerRequest.objects.using(DB_ALIAS), id=pid)

    # 授權（以 session 驗證乘客密碼）
    if not _pax_authorized(request, pid):
        return JsonResponse({"ok": False, "error": "未授權"}, status=403)

    # ---- 基本欄位 ----
    name_in = request.POST.get("passenger_name")
    if name_in is not None:
        name_in = name_in.strip()
        if name_in:
            p.passenger_name = name_in

    gender = request.POST.get("gender")
    if gender:
        p.gender = gender

    # Email（允許空→None）
    email = request.POST.get("email")
    p.email = (email or None)

    contact_in = request.POST.get("contact")
    if contact_in is not None:
        p.contact = contact_in.strip()

    # 座位數（容錯）
    seats_in = request.POST.get("seats_needed")
    if seats_in not in (None, ""):
        try:
            p.seats_needed = int(seats_in)
        except (TypeError, ValueError):
            pass

    # 願付（允許空→None；保留字串/數字皆可）
    wpay = request.POST.get("willing_to_pay")
    p.willing_to_pay = (wpay if wpay not in (None, "",) else None)

    # 地點
    dep_in = request.POST.get("departure")
    if dep_in is not None:
        p.departure = dep_in.strip() or p.departure

    des_in = request.POST.get("destination")
    if des_in is not None:
        p.destination = des_in.strip() or p.destination

    # 日期
    date_val = request.POST.get("date")
    if date_val:
        p.date = date_val

    ret_val = request.POST.get("return_date")
    p.return_date = (ret_val or None)

    # 是否一起回程（"true"/"false"/""）
    tr = request.POST.get("together_return")
    if tr == "true":
        p.together_return = True
    elif tr == "false":
        p.together_return = False
    elif tr == "":
        p.together_return = None
    # 其他值 -> 維持原值

    # 備註
    note_in = request.POST.get("note")
    if note_in is not None:
        p.note = note_in.strip()

    # ---- 隱私設定：需有 Email 才允許隱藏 ----
    hide_raw = (request.POST.get("hide_contact") or "").lower()
    want_hide = hide_raw in ("1", "true", "on", "yes")
    p.hide_contact = (want_hide and bool(p.email))  # 沒 Email 一律 False


    # 寫入
    p.save(using=DB_ALIAS)

    # ---- 即時更新：在交易提交後廣播 ----
    driver_id = p.driver_id

    def _after_commit(d_id=driver_id):
        if d_id:
            # 更新：司機卡片（首頁/清單）＋ 司機管理頁（左右兩欄）
            broadcast_driver_card(d_id)
            broadcast_manage_panels(d_id)
        else:
            # 沒綁司機：更新乘客列表（你的既有函式）
            _broadcast_lists()

    transaction.on_commit(_after_commit)

    return JsonResponse({"ok": True})

@require_POST
def pax_delete(request, pid: int):
    """
    刪除乘客紀錄（需先授權）：
    - 若該乘客已被接受 (is_matched=True)，會回沖司機 seats_filled。
    """
    p = get_object_or_404(PassengerRequest.objects.using("find_db"), id=pid)
    if not _pax_authorized(request, pid):
        return JsonResponse({"ok": False, "error": "未授權"}, status=403)

    # 若是已接受的乘客，回沖座位
    if p.is_matched and p.driver_id:
        try:
            d = DriverTrip.objects.using("find_db").select_for_update().get(id=p.driver_id)
            d.seats_filled = max(0, d.seats_filled - (p.seats_needed or 0))
            # 回沖後座位未滿，可自動重新上架（看你需求；不想自動上架就註解掉）
            if d.seats_filled < d.seats_total:
                d.is_active = True
            d.save(using="find_db")
        except DriverTrip.DoesNotExist:
            pass

    p.delete(using="find_db")
    driver_id = p.driver_id  # 廣播用
    transaction.on_commit(lambda: broadcast_driver_card(driver_id))
    if driver_id:
        broadcast_driver_card(driver_id)
    
    return JsonResponse({"ok": True})


@transaction.atomic(using="find_db")
def delete_driver(request, driver_id: int):
    if request.method != "POST":
        raise Http404()

    d = (DriverTrip.objects.using("find_db")
         .select_for_update()
         .filter(pk=driver_id)
         .first())
    if not d:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": "Driver not found"}, status=404)
        raise Http404("Driver not found")

    # 釋放該司機底下的乘客
    PassengerRequest.objects.using("find_db").filter(driver_id=driver_id).update(
        driver=None,
        is_matched=False,
    )
    # 硬刪除
    d.delete(using="find_db")

    # ✅ 讓所有人同步到最新清單（這是「交易外」或「交易完成後」也 OK）
    broadcast_full_update()

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "deleted_id": driver_id})
    return redirect("find_index")

@csrf_protect
@require_POST
def driver_manage_auth(request, driver_id: int):
    """
    接收密碼，驗證正確就回傳管理頁 URL 讓前端跳轉
    """
    """
    驗證司機密碼，成功則回傳管理頁面 URL，讓前端跳轉
    """
    driver = get_object_or_404(DriverTrip.objects.using(DB_ALIAS), id=driver_id)
    pwd = (request.POST.get("password") or "").strip()
    if not pwd:
        return JsonResponse({"ok": False, "error": "請輸入密碼"}, status=400)

    if pwd != driver.password:
        return JsonResponse({"ok": False, "error": "密碼錯誤"}, status=403)
    
    # 檢查密碼是否正確
    if constant_time_compare(pwd, driver.password or ""):
        # 密碼正確，設定 session，並讓前端跳轉
        sess_key = f"driver_auth_{driver.id}"
        url = reverse("driver_manage", args=[driver_id])
        request.session[sess_key] = True
        request.session.modified = True
        request.session.set_expiry(1800)  # 設定 session 失效時間（例如：60 秒後過期）
        return JsonResponse({"ok": True, "url": url})

    return JsonResponse({"ok": False, "error": "密碼錯誤"}, status=403)
    

    

@ensure_csrf_cookie
def driver_manage(request, driver_id: int):
    # 1) 先抓 driver
    driver = get_object_or_404(DriverTrip.objects.using(DB_ALIAS), id=driver_id)
    # 2) 驗證授權（用你自己的規則）
    sess_key = f"driver_auth_{driver.id}"

    # A) 尚未授權：只顯示驗證頁（或改成直接 403）
    if not request.session.get(sess_key):
        if request.method == "POST" and request.POST.get("form") == "auth":
            pwd = (request.POST.get("password") or "").strip()
            if constant_time_compare(pwd, driver.password or ""):
                request.session[sess_key] = True
                request.session.modified = True
                request.session.set_expiry(1800)
                return redirect("driver_manage", driver_id=driver.id)
            return redirect(f"/find?auth_required=true")
        # GET：顯示驗證頁（*不要*帶任何乘客資料）
        return redirect(f"/find?auth_required=true")
    authed = bool(request.session.get(sess_key))
    # 如果你在其它 view 已做密碼驗證，就會把 session 設 True
    # request.session[sess_key] = True

    # 3) 初次進頁先準備列表
    pending_qs  = PassengerRequest.objects.using(DB_ALIAS).filter(driver=driver, is_matched=False).order_by("id")
    accepted_qs = PassengerRequest.objects.using(DB_ALIAS).filter(driver=driver, is_matched=True ).order_by("id")
    candidates_qs = PassengerRequest.objects.using(DB_ALIAS).filter(
        departure=driver.departure,
        destination=driver.destination,
        date=driver.date,
        is_matched=False,
        driver__isnull=True,
    ).order_by("id")

    saved_msg = matched_msg = full_msg = ""

    if request.method == "POST":
        form_type = request.POST.get("form", "")

        # === A) 更新司機資訊 ===
        if form_type == "update_driver":
            if not authed:
                return HttpResponseForbidden("FORBIDDEN")

            # 基本欄位
            driver.driver_name = (request.POST.get("driver_name") or driver.driver_name).strip()
            driver.gender      = (request.POST.get("gender") or driver.gender or "X").strip() or "X"
            driver.email       = (request.POST.get("email") or None)
            driver.contact     = (request.POST.get("contact") or driver.contact or "").strip()
            # ✅ 司機備註（允許空 → None）
            driver.note = (request.POST.get("note") or "").strip() or None
            # 密碼：有填才更新
            pwd = (request.POST.get("password") or "").strip()
            if pwd:
                driver.password = pwd

            # 隱私：需有 email 才能 True
            hide_raw   = (request.POST.get("hide_contact") or "").lower()
            want_hide  = hide_raw in ("1", "true", "on", "yes")
            driver.hide_contact = bool(driver.email) and want_hide

            # 座位
            try:
                seats_total = int(request.POST.get("seats_total") or driver.seats_total)
            except (TypeError, ValueError):
                seats_total = driver.seats_total
            driver.seats_total = max(1, seats_total)
            if driver.seats_filled > driver.seats_total:
                driver.seats_filled = driver.seats_total

            # 酌收費用（選填）
            if hasattr(driver, "fare_note"):
                driver.fare_note = (request.POST.get("fare_note") or "").strip() or None

            # 出發/目的地（含自填）
            dep_choice = (request.POST.get("departure") or "").strip()
            dep_custom = (request.POST.get("departure_custom") or "").strip()
            if dep_choice == "自填":
                driver.departure = dep_custom
                if hasattr(driver, "departure_custom"):
                    driver.departure_custom = dep_custom
            else:
                driver.departure = dep_choice
                if hasattr(driver, "departure_custom"):
                    driver.departure_custom = ""

            des_choice = (request.POST.get("destination") or "").strip()
            des_custom = (request.POST.get("destination_custom") or "").strip()
            if des_choice == "自填":
                driver.destination = des_custom
                if hasattr(driver, "destination_custom"):
                    driver.destination_custom = des_custom
            else:
                driver.destination = des_choice
                if hasattr(driver, "destination_custom"):
                    driver.destination_custom = ""

            # 日期防呆
            date_str   = (request.POST.get("date") or "").strip()
            return_str = (request.POST.get("return_date") or "").strip() or None
            dt  = parse_date(date_str) if date_str else driver.date
            rdt = parse_date(return_str) if return_str else None

            today = _date.today()
            error_msg = None
            if not dt or dt < today:
                error_msg = "出發日期不可小於今天"
            elif rdt and rdt < today:
                error_msg = "回程日期不可小於今天（可留空）"
            elif rdt and rdt < dt:
                error_msg = "回程日期不可早於出發日期"

            if error_msg:
                # 回填目前狀態
                pending = pending_qs
                accepted = accepted_qs
                candidates = candidates_qs
                return render(request, "Find/driver_manage.html", {
                    "driver": driver,
                    "pending": pending,
                    "accepted": accepted,
                    "candidates": candidates,
                    "saved_msg": "",
                    "matched_msg": "",
                    "full_msg": "",
                    "authed": authed,
                    "error": error_msg,
                })

            driver.date        = dt
            driver.return_date = rdt

            # 其他旗標
            driver.flexible_pickup = (request.POST.get("flexible_pickup") or getattr(driver, "flexible_pickup", "MAYBE")).strip() or "MAYBE"
            driver.is_active       = (request.POST.get("is_active") == "on")
            if driver.seats_filled >= driver.seats_total:
                driver.is_active = False

            driver.save(using=DB_ALIAS)
            saved_msg = "✅ 已更新司機資料"

            # 廣播（卡片 + 管理頁）
            transaction.on_commit(lambda: (
                broadcast_driver_card(driver.id),
                broadcast_manage_panels(driver.id)
            ))

        # === B) 批次接受乘客 ===
        elif form_type == "accept_passengers":
            if not authed:
                return HttpResponseForbidden("FORBIDDEN")

            ids = request.POST.getlist("accept_ids")
            accepted_names = []

            with transaction.atomic(using=DB_ALIAS):
                d = DriverTrip.objects.using(DB_ALIAS).select_for_update().get(id=driver.id)

                for pid in ids:
                    try:
                        p = PassengerRequest.objects.using(DB_ALIAS).select_for_update().get(id=pid, is_matched=False)
                    except PassengerRequest.DoesNotExist:
                        continue

                    if p.driver_id is None:
                        p.driver = d

                    if d.seats_filled + p.seats_needed <= d.seats_total:
                        d.seats_filled += p.seats_needed
                        p.is_matched = True
                        p.save(using=DB_ALIAS)
                        accepted_names.append(p.passenger_name)

                        if d.seats_filled >= d.seats_total:
                            d.is_active = False
                            d.save(using=DB_ALIAS)
                            full_msg = f"🚗 {d.driver_name} 的行程已滿，已自動下架"
                            break

                d.save(using=DB_ALIAS)

            matched_msg = "✅ 已成功媒合：" + "、".join(accepted_names) if accepted_names else "⚠️ 沒有可媒合的乘客或座位不足"
            transaction.on_commit(lambda: (
                broadcast_driver_card(driver.id),
                broadcast_manage_panels(driver.id)
            ))

        # …(其他分支照你的需求)

        # 重新抓最新資料（避免用舊的 QuerySet）
        driver.refresh_from_db(using=DB_ALIAS)
        pending_qs  = PassengerRequest.objects.using(DB_ALIAS).filter(driver=driver, is_matched=False).order_by("id")
        accepted_qs = PassengerRequest.objects.using(DB_ALIAS).filter(driver=driver, is_matched=True ).order_by("id")
        candidates_qs = PassengerRequest.objects.using(DB_ALIAS).filter(
            departure=driver.departure,
            destination=driver.destination,
            date=driver.date,
            is_matched=False,
            driver__isnull=True,
        ).order_by("id")

    # 最後渲染
    return render(request, "Find/driver_manage.html", {
        "driver": driver,
        "pending": pending_qs,
        "accepted": accepted_qs,
        "candidates": candidates_qs,
        "saved_msg": saved_msg,
        "matched_msg": matched_msg,
        "full_msg": full_msg,
        "authed": authed,
    })
# -------------------
# 首頁
# -------------------
# === helpers ===
from django.db.models import Count

def _getlist_qs(request, key: str) -> list[str]:
    """支援 ?key=a&key=b 或 ?key[]=a&key[]=b 兩種形式。"""
    vals = request.GET.getlist(key)
    if not vals:
        vals = request.GET.getlist(f"{key}[]")
    # 清掉空字串與重複
    return [v for v in dict.fromkeys([ (v or "").strip() for v in vals ]) if v]

def _parse_int(val, default=None):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

def _extract_filters_from_request(request) -> dict:
    """把 URL 查詢參數整理成 driver_cards_qs 可用的 filters dict。"""
    return {
        "dep_in"    : _getlist_qs(request, "dep"),
        "des_in"    : _getlist_qs(request, "des"),
        "date_in"   : _getlist_qs(request, "date"),
        "ret_in"    : _getlist_qs(request, "ret"),
        "gender_in" : _getlist_qs(request, "gender"),
        "need_seats": _parse_int(request.GET.get("need_seats"), None),
        "fare_mode" : (request.GET.get("fare_mode") or "").strip(),              # 'lte' | 'gte'
        "fare_num"  : _parse_int(request.GET.get("fare_num"), None),             # 數字門檻
        "fare_q"    : (request.GET.get("fare") or "").strip(),                   # 關鍵字（如：免費、AA）
        "q"         : (request.GET.get("q") or "").strip(),  
    }

def get_active_location_choices():
    """
    只統計「上架中的司機」的出發地/目的地清單與數量。
    回傳：(DEP_CHOICES, DES_CHOICES, DEP_WITH_COUNT, DES_WITH_COUNT)
    """
    base = DriverTrip.objects.using("find_db").filter(is_active=True)

    dep_ct = (
        base.values("departure")
            .annotate(n=Count("id"))
            .order_by("departure")
    )
    des_ct = (
        base.values("destination")
            .annotate(n=Count("id"))
            .order_by("destination")
    )

    DEP_WITH_COUNT = [(row["departure"] or "", row["n"]) for row in dep_ct if row["departure"]]
    DES_WITH_COUNT = [(row["destination"] or "", row["n"]) for row in des_ct if row["destination"]]

    DEP_CHOICES = [name for name, _ in DEP_WITH_COUNT]
    DES_CHOICES = [name for name, _ in DES_WITH_COUNT]
    return DEP_CHOICES, DES_CHOICES, DEP_WITH_COUNT, DES_WITH_COUNT


# === view ===
def index(request):
    sort = request.GET.get("sort", "date_desc")
    order_by = get_order_by(sort)  # 你的既有對照：date_desc/date_asc/dep_*…

    # 組 filters
    filters = _extract_filters_from_request(request)

    # 司機卡片（已 prefetch pending_list / accepted_list）
    drivers = driver_cards_qs(
        only_active=True,
        order_by=order_by,   # 若你想讓 sort 特製生效，也可只傳 sort=sort
        sort=sort,
        filters=filters,
    )

    # 供日期多選用的選項（純字串）
    # 這裡直接從目前可見的 drivers 取，不會出現無效日期
    DATE_CHOICES = sorted({
        d.date.isoformat() for d in drivers if getattr(d, "date", None)
    })
    RET_CHOICES = sorted({
        d.return_date.isoformat() for d in drivers if getattr(d, "return_date", None)
    })

    # 乘客（左上角區塊）
    passengers = (
        PassengerRequest.objects.using("find_db")
        .filter(is_matched=False, driver__isnull=True)
        .order_by("-id")
    )

    # 出發地/目的地（多選來源）＋數量
    DEP_CHOICES, DES_CHOICES, DEP_WITH_COUNT, DES_WITH_COUNT = get_active_location_choices()

    return render(
        request,
        "Find/index.html",
        {
            "drivers": drivers,
            "passengers": passengers,
            "sort": sort,
            "filters": filters,                 # 給前端回填與 chips
            "DATE_CHOICES": DATE_CHOICES,
            "RET_CHOICES": RET_CHOICES,
            "DEP_CHOICES": DEP_CHOICES,
            "DES_CHOICES": DES_CHOICES,
            "DEP_WITH_COUNT": DEP_WITH_COUNT,   # [(name, n), ...]
            "DES_WITH_COUNT": DES_WITH_COUNT,   # [(name, n), ...]
        },
    )


# -------------------
# 找人（乘客需求）
# -------------------
def find_people(request):
    if request.method == "POST":
        password = request.POST.get("password") or "0000"
        new_passenger = PassengerRequest.objects.using("find_db").create(
            passenger_name=request.POST["name"],
            contact=request.POST["contact"],
            password=password,
            seats_needed=int(request.POST["seats_needed"]),
            departure=request.POST["departure"],
            destination=request.POST["destination"],
            date=request.POST["date"],
            note=request.POST.get("note", "")
        )

        # 找符合的司機（但不自動媒合）
        matches = DriverTrip.objects.using("find_db").filter(
            departure=new_passenger.departure,
            destination=new_passenger.destination,
            date=new_passenger.date,
            is_active=True
        )

        return render(request, "Find/match_driver.html", {
            "passenger": new_passenger,
            "drivers": matches
        })

    return render(request, "Find/find_people.html")


# -------------------
# # ✅ 司機新增出車
# -------------------
def find_car(request):
    if request.method == "POST":
        name         = (request.POST.get("name") or "").strip()
        contact      = (request.POST.get("contact") or "").strip()
        email        = (request.POST.get("email") or "").strip() or None
        password     = (request.POST.get("password") or "0000").strip()
        gender       = (request.POST.get("gender") or "X").strip()
        seats_total  = int(request.POST.get("seats_total") or 0)

        # 出發地：下拉 + 自填覆蓋
        departure           = (request.POST.get("departure") or "").strip()
        departure_custom    = (request.POST.get("departure_custom") or "").strip()
        if departure == "自填" or departure_custom:
            departure = departure_custom

        # 目的地：下拉 + 自填覆蓋（新增這段）
        destination   = (request.POST.get("destination") or "").strip()
        destination_custom = request.POST.get("destination_custom")
        if destination == "自填" or destination_custom:
            destination = destination_custom
        date          = (request.POST.get("date") or "").strip()
        return_date   = (request.POST.get("return_date") or "").strip() or None

        

        flexible_pickup = (request.POST.get("flexible_pickup") or "").strip()
        note          = (request.POST.get("note") or "").strip() or None

        # ⬇️ 新增：酌收費用
        fare_note     = (request.POST.get("fare_note") or "待定或免費").strip() or None

        d = DriverTrip.objects.using("find_db").create(
            driver_name    = name,
            contact        = contact,
            email          = email,
            password       = password,
            gender         = gender,
            seats_total    = seats_total,
            departure      = departure,
            destination    = destination,
            date           = date,
            return_date    = return_date,
            flexible_pickup= flexible_pickup,
            note           = note,
            fare_note      = fare_note,   # ⬅️ 存進資料庫
        )

        # （可選）如果你有做 Channels 的單卡片廣播，打這一行
        #from .ws import broadcast_driver_card
        from django.contrib import messages
        dt  = parse_date(date)
        rdt = parse_date(return_date) if return_date else None
        if dt and rdt and rdt < dt:
            # 轉成一般 dict，避免模板取值時拿到 list
            messages.error(request, "回程日期不可早於出發日期")
            prefill = request.POST.dict()
            return render(request, "Find/find_car.html", {
                "error": "回程日期不可早於出發日期",
                "prefill": prefill,
            })

        # …通過檢查才寫入 DB
        # DriverTrip.objects.using("find_db").create( ... )
        broadcast_driver_card(d.id)
        # 若是 AJAX 送出可回 JSON；否則回首頁
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": True, "id": d.id})
        return redirect("find_index")

        

    return render(request, "Find/find_car.html")


# -------------------
# 乘客管理
# -------------------
def edit_passenger(request, passenger_id):
    passenger = get_object_or_404(PassengerRequest.objects.using("find_db"), id=passenger_id)

    if request.method == "POST":
        password = request.POST.get("password")
        if password == passenger.password:
            return redirect("passenger_manage", passenger_id=passenger.id)
        else:
            return render(request, "Find/index.html", {
                "drivers": DriverTrip.objects.using("find_db").filter(is_active=True),
                "passengers": PassengerRequest.objects.using("find_db").filter(is_matched=False),
                "passenger_error_id": passenger.id,
                "passenger_error_msg": "密碼錯誤，請再試一次"
            })

    return redirect("find_index")


def passenger_manage(request, passenger_id):
    try:
        passenger = PassengerRequest.objects.using("find_db").get(id=passenger_id)
    except ObjectDoesNotExist:
        passenger = None

    if request.method == "POST" and passenger:
        if "update" in request.POST:
            passenger.passenger_name = request.POST["name"]
            passenger.contact = request.POST["contact"]
            passenger.seats_needed = int(request.POST["seats_needed"])
            passenger.departure = request.POST["departure"]
            passenger.destination = request.POST["destination"]
            passenger.date = request.POST["date"]
            passenger.note = request.POST.get("note", "")
            passenger.save(using="find_db")
            return redirect("find_index")

        elif "delete" in request.POST:
            passenger.delete(using="find_db")
            return redirect("find_index")

    return render(request, "Find/passenger_manage.html", {"passenger": passenger})


# -------------------
# 乘客加入司機 (從 match_driver 頁面)
# -------------------

@require_POST
def join_driver(request, driver_id: int):
    is_ajax = (request.headers.get("x-requested-with") == "XMLHttpRequest")

    # ===== 表單值 =====
    departure = (request.POST.get("departure") or
                 request.POST.get("custom_departure") or "").strip()
    raw_pay   = (request.POST.get("willing_to_pay") or "待定或免費").strip()
    try:
        seats_needed = max(1, int(request.POST.get("seats_needed", "1") or 1))
    except (TypeError, ValueError):
        seats_needed = 1

    willing_to_pay = None
    if raw_pay:
        try:
            willing_to_pay = Decimal(raw_pay)
        except Exception:
            willing_to_pay = None

    # ===== 交易 + 鎖行避免搶位 =====
    with transaction.atomic(using="find_db"):
        # 鎖住該司機（同時避免被接受時併發超載）
        d = get_object_or_404(
            DriverTrip.objects.using("find_db").select_for_update(), id=driver_id
        )

        if not d.is_active:
            msg = "此行程已下架或已滿"
            return JsonResponse({"ok": False, "error": msg}, status=400) if is_ajax else redirect("find_index")

        remaining = max(0, (d.seats_total or 0) - (d.seats_filled or 0))
        if seats_needed > remaining:
            msg = f"座位不足，剩餘 {remaining} 位"
            return JsonResponse({"ok": False, "error": msg}, status=400) if is_ajax else redirect("find_index")

        # 建立「待確認」乘客（不佔位；司機接受時才會加 seats_filled）
        PassengerRequest.objects.using("find_db").create(
            passenger_name = (request.POST.get("passenger_name", "").strip() or "匿名"),
            gender         = (request.POST.get("gender", "X")),
            email          = (request.POST.get("email") or None),
            contact        = (request.POST.get("contact", "").strip()),
            seats_needed   = seats_needed,
            willing_to_pay = willing_to_pay,
            departure      = departure,
            destination    = (request.POST.get("destination", "").strip()),
            date           = (request.POST.get("date") or d.date),
            return_date    = (request.POST.get("return_date") or None),
            note           = (request.POST.get("note", "").strip()),
            password       = (request.POST.get("password", "0000").strip() or "0000"),
            driver         = d,
            is_matched     = False,
        )

    # ===== 交易提交後再重新渲染片段並廣播（避免競態）=====
    channel_layer = get_channel_layer()

    pending_qs  = PassengerRequest.objects.using("find_db").filter(is_matched=False).order_by("-id")
    accepted_qs = PassengerRequest.objects.using("find_db").filter(is_matched=True ).order_by("-id")

    # ★ to_attr 改成 pending_list / accepted_list，與模板一致
    drivers_qs = (
        DriverTrip.objects.using("find_db")
        .filter(is_active=True)
        .prefetch_related(
            Prefetch("passengers", queryset=pending_qs,  to_attr="pending_list"),
            Prefetch("passengers", queryset=accepted_qs, to_attr="accepted_list"),
        )
    )
    # materialize（可選，但穩）
    drivers = list(drivers_qs)

    passengers = (
        PassengerRequest.objects.using("find_db")
        .filter(is_matched=False, driver__isnull=True)
        .order_by("-id")
    )

    drivers_html    = render_to_string("Find/_driver_list.html", {"drivers": drivers})
    passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})

    # 全局廣播（其他使用者）
    async_to_sync(channel_layer.group_send)(
        "find_group",
        {
            "type": "send.update",
            "drivers_html": drivers_html,
            "passengers_html": passengers_html,
        },
    )

    # 單卡片重繪（自己與他人都會收到 WS，保險再發一次）
    try:
        broadcast_driver_card(driver_id)
    except Exception:
        pass

    # AJAX 就回 {"ok":true}（若你要「自己」立刻替換，也可以把片段一起回）
    if is_ajax:
        return JsonResponse({"ok": True})
    return redirect("find_index")

def attach_passenger_lists(driver: DriverTrip):
    """
    幫單一 driver 算出 pending / accepted，並掛在 driver 上。
    會回傳 (pending, accepted) 方便需要時直接用。
    """
    plist = list(driver.passengers.all())
    driver.pending  = [p for p in plist if not p.is_matched]
    driver.accepted = [p for p in plist if p.is_matched]
    return driver.pending, driver.accepted
