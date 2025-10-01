from __future__ import annotations

from decimal import Decimal, InvalidOperation
from datetime import date as _date
import json

from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Prefetch, Case, When, Value, IntegerField
from django.http import (
    JsonResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    HttpResponseNotAllowed,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .models import DriverTrip, PassengerRequest

# -------------------
# 常數與排序工具
# -------------------
def on_commit_finddb(func):
    # 確保廣播綁在 find_db 的 transaction 上
    transaction.on_commit(func, using="find_db")

CITY_N2S = [
    "基隆市","台北市","新北市","桃園市","新竹市","新竹縣","苗栗縣",
    "台中市","彰化縣","南投縣","雲林縣",
    "嘉義市","嘉義縣",
    "台南市","高雄市","屏東縣",
    "宜蘭縣","花蓮縣","台東縣",
    "澎湖縣","金門縣","連江縣",
]

def _dep_rank_case() -> Case:
    """依 CITY_N2S 產生出發地排序權重欄位 dep_rank。"""
    whens = [When(departure=city, then=Value(i)) for i, city in enumerate(CITY_N2S)]
    return Case(*whens, default=Value(999), output_field=IntegerField())

SORT_MAP = {
    "date_desc": ["-date", "-id"],   # 出發日期 新→舊
    "date_asc" : ["date", "id"],     # 出發日期 舊→新
    "dep_asc"  : ["departure", "date", "id"],   # 出發地 A→Z
    "dep_desc" : ["-departure", "date", "id"],  # 出發地 Z→A
    "dep_n2s"  : None,  # 特製（北→南）
    "dep_s2n"  : None,  # 特製（南→北）
}

def get_order_by(sort: str | None) -> list[str] | None:
    key = (sort or "").strip() or "date_desc"
    return SORT_MAP.get(key, SORT_MAP["date_desc"])

def get_current_sort(request) -> str:
    """統一取得目前排序（POST > GET > Cookie > 預設）"""
    return (
        request.POST.get("sort")
        or request.GET.get("sort")
        or request.COOKIES.get("find_sort")
        or "date_desc"
    )

# -------------------
# 資料查詢與預取
# -------------------

def _prefetch_lists_qs(order_by: list[str] | None, sort: str):
    """回傳 (drivers_qs, passengers_qs) 供列表渲染。"""
    pending_qs  = PassengerRequest.objects.using("find_db").filter(is_matched=False).order_by("-id")
    accepted_qs = PassengerRequest.objects.using("find_db").filter(is_matched=True).order_by("-id")

    drivers = DriverTrip.objects.using("find_db").filter(is_active=True)
    # 特製北南排序
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

    return drivers, passengers

def attach_passenger_lists(driver: DriverTrip):
    """幫單一 driver 掛上 pending/accepted（用於管理頁）。"""
    plist = list(driver.passengers.all())
    driver.pending  = [p for p in plist if not p.is_matched]
    driver.accepted = [p for p in plist if p.is_matched]
    return driver.pending, driver.accepted

# -------------------
# 廣播（Channels）
# -------------------

def driver_cards_qs(*, only_active=True):
    """
    回傳已帶好 passengers 的 DriverTrip QuerySet：
    - d.pending_list：未媒合乘客（待確認）
    - d.accepted_list：已媒合乘客（已接受）
    """
    pending_qs  = (PassengerRequest.objects.using("find_db")
                   .filter(is_matched=False)
                   .order_by("-id"))
    accepted_qs = (PassengerRequest.objects.using("find_db")
                   .filter(is_matched=True)
                   .order_by("-id"))

    base = DriverTrip.objects.using("find_db")
    if only_active:
        base = base.filter(is_active=True)

    return (base
            .prefetch_related(
                Prefetch("passengers", queryset=pending_qs,  to_attr="pending_list"),
                Prefetch("passengers", queryset=accepted_qs, to_attr="accepted_list"),
            ))

def broadcast_lists(sort: str = "date_desc") -> None:
    """整份 drivers/passengers 部分渲染並推送。"""
    channel_layer = get_channel_layer()
    order_by = get_order_by(sort)
    drivers, passengers = _prefetch_lists_qs(order_by, sort)

    drivers_html    = render_to_string("Find/_driver_list.html",    {"drivers": drivers})
    passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})

    async_to_sync(channel_layer.group_send)(
        "find_group",
        {
            "type": "send.update",
            "drivers_html": drivers_html,
            "passengers_html": passengers_html,
            "sort": sort,
        },
    )
def broadcast_driver_card(driver_id: int, *args, **kwargs):
    """
    與舊版相同的單一卡片廣播：
    - 查 d 與 driver（相容你舊的 driver_cards_qs 使用方式）
    - 掛上 pending/accepted 與 pending_list/accepted_list 給模板
    - 廣播兩種 partial 格式（payload 包裝 + 直傳 driver_id/html），確保前端相容
    """
    channel_layer = get_channel_layer()

    # 先用你的舊工具把卡片資料取回（不只 active）
    d = DriverTrip.objects.using("find_db").filter(id=driver_id).first()
    driver = (driver_cards_qs(only_active=False).filter(id=driver_id).first())

    if not driver:
        # 若被刪除/下架，通知前端移除卡片（舊格式：payload only）
        async_to_sync(channel_layer.group_send)(
            "find_group",
            {
                "payload": {
                    "type": "driver_partial",
                    "driver_id": driver_id,
                    "driver_html": "",
                    "active": False,
                },
            },
        )
        return

    # 準備這張卡片需要的 pending / accepted（兩種命名相容你的模板）
    pending_qs  = PassengerRequest.objects.using("find_db").filter(driver_id=driver_id, is_matched=False).order_by("-id")
    accepted_qs = PassengerRequest.objects.using("find_db").filter(driver_id=driver_id, is_matched=True ).order_by("-id")

    # a) 掛在 driver（有些模板用 d.pending / d.accepted）
    driver.pending  = list(pending_qs)
    driver.accepted = list(accepted_qs)

    # b) 掛在 d（有些模板用 d.pending_list / d.accepted_list）
    if d:
        d.pending_list  = list(pending_qs)
        d.accepted_list = list(accepted_qs)

    # 用 d 來渲染（沿用你的舊約定）
    ctx_driver = d or driver
    driver_html = render_to_string("Find/_driver_card.html", {"d": ctx_driver})

    # 廣播（1）payload 版本（舊前端已使用）
    async_to_sync(channel_layer.group_send)(
        "find_group",
        {
            "type": "send.partial",
            "payload": {
                "type": "driver_partial",
                "driver_id": ctx_driver.id,
                "driver_html": driver_html,
                "active": bool(ctx_driver.is_active),
            },
        },
    )

    # 廣播（2）直傳 driver_id/html（你舊檔案的第二發，保持相容）
    html = render_to_string("Find/_driver_card.html", {"d": driver})
    async_to_sync(channel_layer.group_send)(
        "find_group",
        {
            "type": "send.partial",
            "driver_id": driver_id,
            "html": html,
        },
    )


def broadcast_full_lists():
    """
    與舊版相同：重算 drivers + passengers，渲染兩段 HTML，一次廣播。
    （不帶 sort；排序由外部決定或模板內部處理）
    """
    pending_qs  = PassengerRequest.objects.using("find_db").filter(is_matched=False)
    accepted_qs = PassengerRequest.objects.using("find_db").filter(is_matched=True)

    drivers = (
        DriverTrip.objects.using("find_db")
        .filter(is_active=True)
        .prefetch_related(
            Prefetch("passengers", queryset=pending_qs,  to_attr="pending"),
            Prefetch("passengers", queryset=accepted_qs, to_attr="accepted"),
        )
    )

    passengers = (
        PassengerRequest.objects.using("find_db")
        .filter(is_matched=False, driver__isnull=True)
        .order_by("-id")
    )

    drivers_html    = render_to_string("Find/_driver_list.html", {"drivers": drivers})
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


# -------------------
# 首頁
# -------------------

def index(request):
    sort = get_current_sort(request)
    order_by = get_order_by(sort)
    drivers, passengers = _prefetch_lists_qs(order_by, sort)
    return render(
        request,
        "Find/index.html",
        {"drivers": drivers, "passengers": passengers, "sort": sort},
    )

# -------------------
# 司機建立（簡化版）
# -------------------

def create_driver(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    # 你的表單驗證略…
    with transaction.atomic(using="find_db"):
        d = DriverTrip.objects.using("find_db").create(
            # TODO: 依你的表單欄位填入
            driver_name = request.POST.get("driver_name", "匿名司機").strip() or "匿名司機",
            contact     = (request.POST.get("contact") or "").strip(),
            email       = (request.POST.get("email") or "").strip() or None,
            password    = (request.POST.get("password") or "0000").strip() or "0000",
            gender      = (request.POST.get("gender") or "X").strip() or "X",
            seats_total = int(request.POST.get("seats_total") or 1),
            seats_filled= 0,
            departure   = (request.POST.get("departure") or "").strip(),
            destination = (request.POST.get("destination") or "").strip(),
            date        = request.POST.get("date"),
            return_date = request.POST.get("return_date") or None,
            flexible_pickup = (request.POST.get("flexible_pickup") or "MAYBE").strip(),
            note        = (request.POST.get("note") or "").strip() or None,
            fare_note   = (request.POST.get("fare_note") or "").strip() or None,
            is_active   = True,
        )
    sort = get_current_sort(request)
    on_commit_finddb(lambda: broadcast_driver_card(d.id))
    return redirect(f"{reverse('find_index')}?sort={sort}")

# -------------------
# 找人（乘客需求）
# -------------------

def find_people(request):
    if request.method == "POST":
        password = (request.POST.get("password") or "0000").strip() or "0000"
        with transaction.atomic(using="find_db"):
            new_passenger = PassengerRequest.objects.using("find_db").create(
                passenger_name=(request.POST.get("name") or "匿名").strip() or "匿名",
                contact=(request.POST.get("contact") or "").strip(),
                password=password,
                seats_needed=int(request.POST.get("seats_needed") or 1),
                departure=(request.POST.get("departure") or "").strip(),
                destination=(request.POST.get("destination") or "").strip(),
                date=request.POST.get("date"),
                note=(request.POST.get("note") or "").strip(),
                is_matched=False,
            )

        matches = DriverTrip.objects.using("find_db").filter(
            departure=new_passenger.departure,
            destination=new_passenger.destination,
            date=new_passenger.date,
            is_active=True,
        )
        return render(request, "Find/match_driver.html", {
            "passenger": new_passenger,
            "drivers": matches
        })

    return render(request, "Find/find_people.html")

# -------------------
# 司機新增出車（完整表單）
# -------------------

def find_car(request):
    if request.method == "POST":
        name         = (request.POST.get("name") or "").strip()
        contact      = (request.POST.get("contact") or "").strip()
        email        = (request.POST.get("email") or "").strip() or None
        password     = (request.POST.get("password") or "").strip() or "0000"
        gender       = (request.POST.get("gender") or "X").strip()
        seats_total  = int(request.POST.get("seats_total") or 1)

        # 出發地：下拉 + 自填
        departure        = (request.POST.get("departure") or "").strip()
        departure_custom = (request.POST.get("departure_custom") or "").strip()
        if departure == "自填" or departure_custom:
            departure = departure_custom

        # 目的地：下拉 + 自填
        destination        = (request.POST.get("destination") or "").strip()
        destination_custom = (request.POST.get("destination_custom") or "").strip()
        if destination == "自填" or destination_custom:
            destination = destination_custom

        date        = (request.POST.get("date") or "").strip()
        return_date = (request.POST.get("return_date") or "").strip() or None

        dt  = parse_date(date) if date else None
        rdt = parse_date(return_date) if return_date else None
        if dt and rdt and rdt < dt:
            messages.error(request, "回程日期不可早於出發日期")
            prefill = request.POST.dict()
            return render(request, "Find/find_car.html", {"error": "回程日期不可早於出發日期","prefill": prefill})

        flexible_pickup = (request.POST.get("flexible_pickup") or "MAYBE").strip()
        note       = (request.POST.get("note") or "").strip() or None
        fare_note  = (request.POST.get("fare_note") or "").strip() or None

        with transaction.atomic(using="find_db"):
            d = DriverTrip.objects.using("find_db").create(
                driver_name=name or "匿名司機",
                contact=contact,
                email=email,
                password=password,
                gender=gender or "X",
                seats_total=seats_total,
                seats_filled=0,
                departure=departure,
                destination=destination,
                date=date,
                return_date=return_date,
                flexible_pickup=flexible_pickup,
                note=note,
                fare_note=fare_note,
                is_active=True,
            )

        sort = get_current_sort(request)
        on_commit_finddb(lambda: broadcast_driver_card(d.id))

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": True, "id": d.id})
        return redirect(f"{reverse('find_index')}?sort={sort}")

    return render(request, "Find/find_car.html")

# -------------------
# 乘客管理（驗證→取得→更新/刪除）
# -------------------

SESSION_PAX = "pax_auth_{}"

def _pax_authorized(request, pid: int) -> bool:
    return request.session.get(SESSION_PAX.format(pid)) is True

@require_POST
def pax_auth(request, pid: int):
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
def pax_update(request, pid: int):
    p = get_object_or_404(PassengerRequest.objects.using("find_db"), id=pid)
    if not _pax_authorized(request, pid):
        return JsonResponse({"ok": False, "error": "未授權"}, status=403)

    p.passenger_name = (request.POST.get("passenger_name") or p.passenger_name).strip() or p.passenger_name
    p.gender         = (request.POST.get("gender") or p.gender)
    p.email          = request.POST.get("email") or None
    p.contact        = (request.POST.get("contact") or p.contact).strip()

    try:
        p.seats_needed = int(request.POST.get("seats_needed", p.seats_needed))
    except (TypeError, ValueError):
        pass

    wpay = request.POST.get("willing_to_pay")
    if wpay in (None, ""):
        p.willing_to_pay = None
    else:
        try:
            p.willing_to_pay = Decimal(str(wpay))
        except (InvalidOperation, TypeError):
            p.willing_to_pay = None

    p.departure   = (request.POST.get("departure") or p.departure).strip()
    p.destination = (request.POST.get("destination") or p.destination).strip()

    date_val = request.POST.get("date")
    p.date = date_val or p.date

    ret_val = request.POST.get("return_date")
    p.return_date = ret_val or None

    tr = request.POST.get("together_return")
    if tr == "true":
        p.together_return = True
    elif tr == "false":
        p.together_return = False
    else:
        p.together_return = None

    p.note = (request.POST.get("note") or p.note).strip()

    p.save(using="find_db")

    driver_id = p.driver_id
    sort = get_current_sort(request)
    on_commit_finddb(lambda: broadcast_driver_card(driver_id) if driver_id else broadcast_full_lists())
    return JsonResponse({"ok": True})

@require_POST
def pax_delete(request, pid: int):
    p = get_object_or_404(PassengerRequest.objects.using("find_db"), id=pid)
    if not _pax_authorized(request, pid):
        return JsonResponse({"ok": False, "error": "未授權"}, status=403)

    sort = get_current_sort(request)
    driver_id = p.driver_id

    with transaction.atomic(using="find_db"):
        if p.is_matched and p.driver_id:
            try:
                d = DriverTrip.objects.using("find_db").select_for_update().get(id=p.driver_id)
                d.seats_filled = max(0, d.seats_filled - (p.seats_needed or 0))
                if d.seats_filled < d.seats_total:
                    d.is_active = True
                d.save(using="find_db")
            except DriverTrip.DoesNotExist:
                pass

        p.delete(using="find_db")

    transaction.on_commit(
        lambda: broadcast_driver_card(driver_id, sort) if driver_id else broadcast_lists(sort)
    )
    return JsonResponse({"ok": True})

# -------------------
# 乘客加入司機（從 match_driver 或卡片）
# -------------------

@require_POST
def join_driver(request, driver_id: int):
    d = get_object_or_404(DriverTrip.objects.using("find_db"), id=driver_id)

    departure = (request.POST.get("departure") or request.POST.get("custom_departure") or "").strip()

    raw_pay = (request.POST.get("willing_to_pay") or "").strip()
    willing_to_pay = None
    if raw_pay:
        try:
            willing_to_pay = Decimal(raw_pay)
        except (InvalidOperation, TypeError):
            willing_to_pay = None

    with transaction.atomic(using="find_db"):
        PassengerRequest.objects.using("find_db").create(
            passenger_name = (request.POST.get("passenger_name") or "").strip() or "匿名",
            gender         = (request.POST.get("gender") or "X"),
            email          = request.POST.get("email") or None,
            contact        = (request.POST.get("contact") or "").strip(),
            seats_needed   = int(request.POST.get("seats_needed") or 1),
            willing_to_pay = willing_to_pay,
            departure      = departure,
            destination    = (request.POST.get("destination") or "").strip(),
            date           = request.POST.get("date") or d.date,
            return_date    = request.POST.get("return_date") or None,
            note           = (request.POST.get("note") or "").strip(),
            password       = (request.POST.get("password") or "0000").strip() or "0000",
            driver         = d,
            is_matched     = False,
        )

    sort = get_current_sort(request)
    transaction.on_commit(lambda: (
        broadcast_lists(sort),          # 列表重算
        broadcast_driver_card(d.id, sort)  # 單卡片也更新
    ))

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": True})

    return redirect(f"{reverse('find_index')}?sort={sort}")

# -------------------
# 司機管理（含更新與接受乘客）
# -------------------

def driver_manage_auth(request, driver_id: int):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    driver = get_object_or_404(DriverTrip.objects.using("find_db"), id=driver_id)
    password = request.POST.get("password", "")
    if not password:
        return JsonResponse({"ok": False, "error": "請輸入密碼"}, status=400)
    if password != driver.password:
        return JsonResponse({"ok": False, "error": "密碼錯誤"}, status=403)

    url = reverse("driver_manage", args=[driver_id])
    return JsonResponse({"ok": True, "url": url})

def driver_manage(request, driver_id: int):
    driver = get_object_or_404(DriverTrip.objects.using("find_db").prefetch_related("passengers"), id=driver_id)
    attach_passenger_lists(driver)

    candidates = (PassengerRequest.objects.using("find_db")
                  .filter(departure=driver.departure,
                          destination=driver.destination,
                          date=driver.date,
                          is_matched=False,
                          driver__isnull=True)
                  .order_by("id"))

    saved_msg = matched_msg = full_msg = ""

    if request.method == "POST":
        form_type = request.POST.get("form", "")

        if form_type == "update_driver":
            driver.driver_name = (request.POST.get("driver_name") or driver.driver_name).strip() or driver.driver_name
            driver.gender      = (request.POST.get("gender") or driver.gender).strip() or "X"
            driver.email       = request.POST.get("email") or None
            driver.contact     = (request.POST.get("contact") or driver.contact).strip()

            pwd = (request.POST.get("password") or "").strip()
            if pwd:
                driver.password = pwd

            try:
                seats_total = int(request.POST.get("seats_total") or driver.seats_total)
            except (TypeError, ValueError):
                seats_total = driver.seats_total
            driver.seats_total = max(1, seats_total)
            if driver.seats_filled > driver.seats_total:
                driver.seats_filled = driver.seats_total

            if hasattr(driver, "fare_note"):
                driver.fare_note = (request.POST.get("fare_note") or "").strip() or None

            dep_choice = (request.POST.get("departure") or "").strip()
            dep_custom = (request.POST.get("departure_custom") or "").strip()
            driver.departure = dep_custom if (dep_choice == "自填" or dep_custom) else dep_choice

            des_choice = (request.POST.get("destination") or "").strip()
            des_custom = (request.POST.get("destination_custom") or "").strip()
            driver.destination = des_custom if (des_choice == "自填" or des_custom) else des_choice

            date_str   = (request.POST.get("date") or "").strip()
            return_str = (request.POST.get("return_date") or "").strip() or None
            dt  = parse_date(date_str) if date_str else None
            rdt = parse_date(return_str) if return_str else None

            today = _date.today()
            error_msg = None
            if not dt or dt < today:
                error_msg = "出發日期不可小於今天"
            elif rdt and rdt < today:
                error_msg = "回程日期不可小於今天（可留空）"
            elif rdt and dt and rdt < dt:
                error_msg = "回程日期不可早於出發日期"

            if error_msg:
                attach_passenger_lists(driver)
                return render(request, "Find/driver_manage.html", {
                    "driver": driver,
                    "pending": driver.pending,
                    "accepted": driver.accepted,
                    "candidates": candidates,
                    "saved_msg": "",
                    "matched_msg": "",
                    "full_msg": "",
                    "error": error_msg,
                })

            driver.date        = dt
            driver.return_date = rdt

            driver.flexible_pickup = (request.POST.get("flexible_pickup") or getattr(driver, "flexible_pickup", "MAYBE")).strip() or "MAYBE"
            driver.is_active       = (request.POST.get("is_active") == "on")
            if driver.seats_filled >= driver.seats_total:
                driver.is_active = False

            driver.save(using="find_db")
            saved_msg = "✅ 已更新司機資料"
            on_commit_finddb(lambda: broadcast_driver_card(d.id))

        elif form_type == "accept_passengers":
            ids = request.POST.getlist("accept_ids")
            accepted_names = []

            with transaction.atomic(using="find_db"):
                d = DriverTrip.objects.using("find_db").select_for_update().get(id=driver.id)

                for pid in ids:
                    try:
                        p = PassengerRequest.objects.using("find_db").select_for_update().get(id=pid, is_matched=False)
                    except PassengerRequest.DoesNotExist:
                        continue

                    if p.driver_id is None:
                        p.driver = d

                    if d.seats_filled + (p.seats_needed or 0) <= d.seats_total:
                        d.seats_filled += (p.seats_needed or 0)
                        p.is_matched = True
                        p.save(using="find_db")
                        accepted_names.append(p.passenger_name)

                        if d.seats_filled >= d.seats_total:
                            d.is_active = False
                            d.save(using="find_db")
                            full_msg = f"🚗 {d.driver_name} 的行程已滿，已自動下架"
                            break

                d.save(using="find_db")
                on_commit_finddb(lambda: broadcast_driver_card(d.id))

            matched_msg = "✅ 已成功媒合：" + "、".join(accepted_names) if accepted_names else "⚠️ 沒有可媒合的乘客或座位不足"

        # 重新整理畫面資料
        driver.refresh_from_db(using="find_db")
        attach_passenger_lists(driver)
        candidates = (PassengerRequest.objects.using("find_db")
                      .filter(departure=driver.departure,
                              destination=driver.destination,
                              date=driver.date,
                              is_matched=False,
                              driver__isnull=True)
                      .order_by("id"))

    return render(request, "Find/driver_manage.html", {
        "driver": driver,
        "pending": driver.pending,
        "accepted": driver.accepted,
        "candidates": candidates,
        "saved_msg": saved_msg,
        "matched_msg": matched_msg,
        "full_msg": full_msg,
    })

# -------------------
# 乘客編輯入口（輸入密碼→導向管理頁）
# -------------------

def edit_passenger(request, passenger_id: int):
    passenger = get_object_or_404(PassengerRequest.objects.using("find_db"), id=passenger_id)

    if request.method == "POST":
        password = request.POST.get("password")
        if password == passenger.password:
            return redirect("passenger_manage", passenger_id=passenger.id)
        else:
            sort = get_current_sort(request)
            drivers, passengers = _prefetch_lists_qs(get_order_by(sort), sort)
            return render(request, "Find/index.html", {
                "drivers": drivers,
                "passengers": passengers,
                "passenger_error_id": passenger.id,
                "passenger_error_msg": "密碼錯誤，請再試一次",
                "sort": sort,
            })

    return redirect("find_index")

def passenger_manage(request, passenger_id: int):
    try:
        passenger = PassengerRequest.objects.using("find_db").get(id=passenger_id)
    except ObjectDoesNotExist:
        passenger = None

    if request.method == "POST" and passenger:
        if "update" in request.POST:
            passenger.passenger_name = (request.POST.get("name") or passenger.passenger_name).strip() or passenger.passenger_name
            passenger.contact = (request.POST.get("contact") or passenger.contact).strip()
            passenger.seats_needed = int(request.POST.get("seats_needed") or passenger.seats_needed)
            passenger.departure = (request.POST.get("departure") or passenger.departure).strip()
            passenger.destination = (request.POST.get("destination") or passenger.destination).strip()
            passenger.date = request.POST.get("date") or passenger.date
            passenger.note = (request.POST.get("note") or passenger.note).strip()
            passenger.save(using="find_db")
            transaction.on_commit(lambda: broadcast_lists(get_current_sort(request)))
            return redirect(f"{reverse('find_index')}?sort={get_current_sort(request)}")

        elif "delete" in request.POST:
            pid = passenger.id
            passenger.delete(using="find_db")
            transaction.on_commit(lambda: broadcast_lists(get_current_sort(request)))
            return redirect(f"{reverse('find_index')}?sort={get_current_sort(request)}")

    return render(request, "Find/passenger_manage.html", {"passenger": passenger})

# -------------------
# 刪除司機
# -------------------

@require_POST
def delete_driver(request, driver_id: int):
    sort = get_current_sort(request)
    with transaction.atomic(using="find_db"):
        d = get_object_or_404(DriverTrip.objects.using("find_db"), id=driver_id)

        PassengerRequest.objects.using("find_db").filter(driver_id=d.id).update(
            driver=None,
            is_matched=False,
        )

        d.delete(using="find_db")

        def _after():
            channel_layer = get_channel_layer()
            # A) 單卡片移除
            async_to_sync(channel_layer.group_send)(
                "find_group",
                {
                    "type": "send.partial",
                    "payload": {
                        "type": "driver_partial",
                        "driver_id": driver_id,
                        "driver_html": "",
                        "active": False,
                        "sort": sort,
                    },
                },
            )
            # B) 乘客清單重繪
            passengers = PassengerRequest.objects.using("find_db")\
                .filter(is_matched=False, driver__isnull=True).order_by("-id")
            passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})

            async_to_sync(channel_layer.group_send)(
                "find_group",
                {
                    "type": "send.update",
                    "drivers_html": None,
                    "passengers_html": passengers_html,
                    "sort": sort,
                },
            )

        transaction.on_commit(_after)

    messages.success(request, "🗑️ 已取消出車並解除關聯乘客")
    return redirect(f"{reverse('find_index')}?sort={sort}")
