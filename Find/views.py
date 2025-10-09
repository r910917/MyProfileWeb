from decimal import Decimal, InvalidOperation
from collections.abc import Sequence
from datetime import date as _date

from django.http import JsonResponse, HttpResponseForbidden, Http404, HttpResponseNotFound
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
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils.html import strip_tags
from django.core.mail import send_mail
from .utils_email_async import enqueue_join_emails_after_commit
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


def broadcast_driver_manage(driver_id, payload):
    layer = get_channel_layer()
    async_to_sync(layer.group_send)(
        f"driver_manage_{driver_id}",
        {"type": "driver.manage.update", "payload": payload}
    )


def _broadcast_after_change(driver_id: int):
    """
    任何資料異動後：同時推
    (A) 公開清單：單卡片替換
    (B) 司機管理頁：左右兩欄整段
    """
    channel_layer = get_channel_layer()

    # --- 查一次資料，兩邊共用 ---
    d = DriverTrip.objects.using(DB_ALIAS).filter(id=driver_id).first()
    if not d:
        return

    pending_qs = (PassengerRequest.objects.using(DB_ALIAS)
                  .filter(driver=d, is_matched=False)
                  .order_by("id"))
    accepted_qs = (PassengerRequest.objects.using(DB_ALIAS)
                   .filter(driver=d, is_matched=True)
                   .order_by("id"))

    # 供卡片模板使用的屬性（你的 _driver_card.html 用的是 pending_list / accepted_list）
    d.pending_list  = list(pending_qs)
    d.accepted_list = list(accepted_qs)

    # --- (A) 公開清單：單卡片 HTML（FindConsumer 那端會接 "replace_driver_card" 或你也可直接全量 send.update） ---
    card_html = render_to_string("Find/_driver_card.html", {"d": d})
    async_to_sync(channel_layer.group_send)(
        "find_group",
        {"type": "replace_driver_card", "driver_id": driver_id, "html": card_html}
    )

    # --- (B) 司機管理頁：左右兩欄（DriverManageConsumer 期待 group = driver_manage_<id>，type = manage_panels）---
    panels_html = render_to_string("Find/_driver_manage_panels.html", {
        "driver": d,
        "pending": pending_qs,
        "accepted": accepted_qs,
    })
    async_to_sync(channel_layer.group_send)(
        f"driver_manage_{driver_id}",
        {"type": "manage_panels", "html": panels_html}
    )

def broadcast_manage_panels(driver_id: int):
    """只給司機管理頁用：推送待確認／已接受兩個 UL 的整段 HTML。"""
    d = DriverTrip.objects.using(DB_ALIAS).get(pk=driver_id)

    pending_qs = (PassengerRequest.objects.using(DB_ALIAS)
                  .filter(driver=d, is_matched=False)
                  .order_by("-id"))
    accepted_qs = (PassengerRequest.objects.using(DB_ALIAS)
                   .filter(driver=d, is_matched=True)
                   .order_by("-id"))

    html = render_to_string(
        "Find/_driver_manage_panels.html",   # ←← 與 Consumer/上面保持一致
        {"driver": d, "pending": pending_qs, "accepted": accepted_qs},
    )

    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"driver_manage_{driver_id}",        # ←← 與 DriverManageConsumer.connect 的 group_name 一致
        {"type": "manage_panels", "html": html}
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
    payload = {"type": "manage_panels", "html": html}
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(f"find_driver_{driver_id}", {"type": "send.json", "data": payload})
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
        #transaction.on_commit(lambda: _broadcast_after_change(d.id))
    ))
    return JsonResponse({"ok": True, "remaining": max(0, d.seats_total - d.seats_filled)})


@require_POST
@transaction.atomic(using=DB_ALIAS)
def pax_reject(request, pax_id: int):
    USING = "find_db"  # ← 改成你的實際 alias，務必和 .using() 一致
    """司機拒絕/取消乘客：若原本已接受需釋放座位，並從司機底下移除。"""
    try:
        with transaction.atomic(using=USING):
            # 鎖乘客
            p = (PassengerRequest.objects.using(USING)
                 .select_for_update()
                 .get(id=pax_id))

            d = None
            if p.driver_id:
                d = (DriverTrip.objects.using(USING)
                     .select_for_update()
                     .filter(id=p.driver_id)
                     .first())

                # 驗證司機身分（若需要）
                if not _driver_authed(request, d):
                    return JsonResponse({"ok": False, "error": "FORBIDDEN"}, status=403)

            # 已接受 → 回沖座位
            if p.is_matched and d:
                changed = ["seats_filled"]
                d.seats_filled = max(0, (d.seats_filled or 0) - (p.seats_needed or 0))
                if d.seats_filled < (d.seats_total or 0) and d.is_active is False:
                    d.is_active = True
                    changed.append("is_active")
                d.save(using=USING, update_fields=changed)

            # 刪除乘客
            p.delete(using=USING)

        # 交易提交後再廣播
        def _after_commit():
            if p.driver_id:
                broadcast_driver_card(p.driver_id)
                broadcast_manage_panels(p.driver_id)
                #transaction.on_commit(lambda: _broadcast_after_change(d.id))
            else:
                broadcast_full_update()
        transaction.on_commit(_after_commit)

        return JsonResponse({"ok": True})

    except PassengerRequest.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Passenger not found"}, status=404)


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
        #transaction.on_commit(lambda: _broadcast_after_change(d.id))
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

    # 3) 讀取 auto_email_contact（只有 hide=True 才有意義）
    auto_email = (request.POST.get("auto_email") or "").lower() in ("1", "true", "yes", "on")
    d.auto_email_contact = auto_email if hide else False

    try:
        d.full_clean()
    except ValidationError as e:
        return JsonResponse({"ok": False, "error": "; ".join(sum(e.message_dict.values(), []))}, status=400)
    d.save(using="find_db")
    try:
        broadcast_driver_card(driver_id)
    except Exception:
        pass
    return JsonResponse({
        "ok": True,
        "hide": d.hide_contact,
        "auto_email": d.auto_email_contact,
    })

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

def _is_ajax(request):
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'

def pax_get(request, pid: int):
    # 1) 只允許 AJAX
    if not _is_ajax(request):
        # 用 404 假裝不存在，避免洩漏 API 端點
        return HttpResponseNotFound()
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
        # ★ 新增這兩個，讓前端依 DB 決定開關狀態
        "hide_contact": bool(p.hide_contact),
        "auto_email_contact": bool(getattr(p, "auto_email_contact", False)),
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
        # ★ 新增
        "hide_contact": bool(p.hide_contact),
        "auto_email_contact": bool(getattr(p, "auto_email_contact", False)),
    }
    return JsonResponse({"ok": True, "data": data})

BOOL_TRUE = ("1", "true", "on", "yes")

@require_POST
@transaction.atomic(using=DB_ALIAS)
def pax_update(request, pid: int):
    """更新乘客資料（需要先通過 pax_auth）。"""
    p = get_object_or_404(PassengerRequest.objects.using(DB_ALIAS), id=pid)

    if not _pax_authorized(request, pid):
        return JsonResponse({"ok": False, "error": "未授權"}, status=403)

    # ---------- 基本欄位 ----------
    name_in = request.POST.get("passenger_name")
    if name_in is not None:
        name_in = name_in.strip()
        if name_in:
            p.passenger_name = name_in

    gender = request.POST.get("gender")
    if gender:
        p.gender = gender

    # Email（允許空 → None）
    email = (request.POST.get("email") or "").strip() or None
    p.email = email

    contact_in = request.POST.get("contact")
    if contact_in is not None:
        p.contact = (contact_in or "").strip()

    # 座位數（容錯）
    seats_in = request.POST.get("seats_needed")
    if seats_in not in (None, ""):
        try:
            p.seats_needed = max(1, int(seats_in))
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "上車人數需為正整數"}, status=400)

    # 願付金額：空字串 → None；其餘轉 Decimal
    wpay_raw = (request.POST.get("willing_to_pay") or "").strip()
    if wpay_raw == "":
        p.willing_to_pay = None
    else:
        try:
            p.willing_to_pay = Decimal(wpay_raw)
        except (InvalidOperation, ValueError):
            return JsonResponse({"ok": False, "error": "願付金額需為數字"}, status=400)

    # 地點
    dep_in = request.POST.get("departure")
    if dep_in is not None:
        p.departure = (dep_in or "").strip()

    des_in = request.POST.get("destination")
    if des_in is not None:
        p.destination = (des_in or "").strip()

    # 日期（字串 → date；空字串代表「不變」，回程允許清空）
    date_raw = request.POST.get("date")
    if date_raw is not None:
        date_raw = date_raw.strip()
        if date_raw == "":
            # 不修改出發日（若你想允許清空，改成 p.date = None）
            pass
        else:
            dt = parse_date(date_raw)
            if not dt:
                return JsonResponse({"ok": False, "error": "出發日期格式不正確"}, status=400)
            p.date = dt

    ret_raw = request.POST.get("return_date")
    if ret_raw is not None:
        ret_raw = ret_raw.strip()
        if ret_raw == "":
            p.return_date = None
        else:
            rdt = parse_date(ret_raw)
            if not rdt:
                return JsonResponse({"ok": False, "error": "回程日期格式不正確"}, status=400)
            p.return_date = rdt

    # 是否一起回程（三態）
    tr = (request.POST.get("together_return") or "").strip().lower()
    if tr == "true":
        p.together_return = True
    elif tr == "false":
        p.together_return = False
    elif tr == "":
        p.together_return = None  # 未指定
    # 其他值 → 維持原值

    # 備註
    note_in = request.POST.get("note")
    if note_in is not None:
        p.note = (note_in or "").strip()

    # ---------- 隱私 + 自動寄信 ----------
    want_hide = (request.POST.get("hide_contact", "0").lower() in BOOL_TRUE)
    want_auto = (request.POST.get("auto_email_contact", "0").lower() in BOOL_TRUE)

    # 沒有 email 時，兩者一律 False；有 email 才看使用者勾選
    p.hide_contact = bool(email) and want_hide
    p.auto_email_contact = bool(email) and p.hide_contact and want_auto

    # ---------- 驗證 & 儲存 ----------
    try:
        p.full_clean()
    except ValidationError as e:
        # 把具體原因回給前端（避免只看到 HTTP 400）
        # e.message_dict 可能是 {'field': ['msg1', 'msg2'], ...}
        msgs = []
        for _, vs in e.message_dict.items():
            msgs.extend(vs)
        return JsonResponse({"ok": False, "error": "；".join(msgs) or "資料驗證失敗"}, status=400)

    p.save(using=DB_ALIAS)

    # ---------- 廣播 ----------
    driver_id = p.driver_id
    def _after_commit(d_id=driver_id):
        if d_id:
            broadcast_driver_card(d_id)
            broadcast_manage_panels(d_id)
            #transaction.on_commit(lambda: _broadcast_after_change(d_id))
        else:
            _broadcast_lists()
    transaction.on_commit(_after_commit)

    return JsonResponse({
        "ok": True,
        "p": {
            "id": p.id,
            "passenger_name": p.passenger_name,
            "gender": p.gender,
            "email": p.email,
            "contact": p.contact,
            "seats_needed": p.seats_needed,
            "willing_to_pay": str(p.willing_to_pay) if p.willing_to_pay is not None else "",
            "departure": p.departure or "",
            "destination": p.destination or "",
            "date": p.date.isoformat() if p.date else "",
            "return_date": p.return_date.isoformat() if p.return_date else "",
            "together_return": None if p.together_return is None else bool(p.together_return),
            "note": p.note or "",
            "hide_contact": p.hide_contact,
            "auto_email_contact": p.auto_email_contact,
        }
    })

@require_POST
@transaction.atomic(using=DB_ALIAS)
def pax_delete(request, pid: int):
    """
    刪除乘客紀錄（需先授權）：
    - 若該乘客已被接受 (is_matched=True)，會回沖司機 seats_filled。
    """
    try:
        p = get_object_or_404(PassengerRequest.objects.using("find_db"), id=pid)
        # 授權：乘客密碼已通過 (session) 或 司機管理已通過
        if not (_pax_authorized(request, pid) or _driver_authed_by_pax(request, p)):
            return JsonResponse({"ok": False, "error": "FORBIDDEN"}, status=403)
        driver_id = p.driver_id  # 先記下來，避免之後刪掉就拿不到

        # 已接受 → 回沖座位
        if p.is_matched and driver_id:
                try:
                    d = (DriverTrip.objects.using("find_db")
                        .select_for_update()
                        .get(id=driver_id))
                    d.seats_filled = max(0, (d.seats_filled or 0) - (p.seats_needed or 0))
                    # 可選：釋放後未滿 → 自動上架
                    if d.seats_filled < d.seats_total:
                        d.is_active = True
                    d.save(using="find_db", update_fields=["seats_filled", "is_active"])
                except DriverTrip.DoesNotExist:
                    return JsonResponse({"ok": False, "error": f"Server error: {e}"}, status=500)

        p.delete(using="find_db")
        transaction.on_commit(lambda: broadcast_driver_card(driver_id))
        if driver_id:
            broadcast_manage_panels(driver_id)
            #transaction.on_commit(lambda: _broadcast_after_change(driver_id))
        
        return JsonResponse({"ok": True})
    except Exception as e:
        # 把例外吃住，回 JSON，避免前端解析 HTML 出錯
        return JsonResponse({"ok": False, "error": f"Server error: {e}"}, status=500)

def _driver_authed_by_pax(request, p: PassengerRequest) -> bool:
    """若你已有司機管理授權的 session，就回 True；否則 False。"""
    if not p.driver_id:
        return False
    sess_key = f"driver_auth_{p.driver_id}"
    return bool(request.session.get(sess_key))

@transaction.atomic(using="find_db")
def delete_driver(request, driver_id: int):
    if request.method != "POST":
        raise Http404()

    d = (DriverTrip.objects.using("find_db")
         .select_for_update()
         .filter(pk=driver_id)
         .first())
    some_other_condition = d.is_active == "active"
    delete_event = driver_id is not None and some_other_condition
    if not d:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": "Driver not found"}, status=404)
        raise Http404("Driver not found")

    # ✅ 直接刪除該司機底下所有乘客
    PassengerRequest.objects.using("find_db").filter(driver_id=driver_id).delete()
    # 硬刪除
    d.delete(using="find_db")

    # ✅ 讓所有人同步到最新清單（這是「交易外」或「交易完成後」也 OK）
    if delete_event:
        channel_layer = get_channel_layer()  # 確保 channel_layer 被正確初始化
        async_to_sync(channel_layer.group_send)("find_group", {
            "type": "driver_partial",
            "driver_id": driver_id,
            "driver_html": "",
            "active": False,
        })
        return  # 直接返回，不再執行後續代碼

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        # 當請求是 AJAX 請求時，返回 JSON 格式的結果
        return JsonResponse({"ok": True, "deleted_id": driver_id})

    # 若非 AJAX 請求，重定向到查詢頁面
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
            hide_raw  = (request.POST.get("hide_contact") or "").lower()
            want_hide = hide_raw in ("1", "true", "on", "yes")
            driver.hide_contact = bool(driver.email) and want_hide
            # 沒有 Email 不能隱藏
            if want_hide and not driver.email:
                # 回填目前狀態並顯示錯誤（沿用你已有的 render 區塊）
                pending   = pending_qs
                accepted  = accepted_qs
                candidates= candidates_qs
                return render(request, "Find/driver_manage.html", {
                    "driver": driver,
                    "pending": pending,
                    "accepted": accepted,
                    "candidates": candidates,
                    "saved_msg": "",
                    "matched_msg": "",
                    "full_msg": "",
                    "authed": authed,
                    "error": "要隱藏聯絡方式，必須先填寫 Email",
                })
            
            # 只有隱藏時才讀 auto_email_contact；否則一律關閉
            auto_raw  = (request.POST.get("auto_email_contact") or "").lower()
            want_auto = auto_raw in ("1", "true", "on", "yes")
            driver.auto_email_contact = bool(driver.email) and driver.hide_contact and want_auto

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
                broadcast_manage_panels(driver.id),
                #_broadcast_after_change(driver.id)
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
                broadcast_manage_panels(driver.id),
                #_broadcast_after_change(driver.id)
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
# ---- imports）----
import re
from django.db import connection
from django.db.models import (
    Case, When, Value, IntegerField, ExpressionWrapper, F, Prefetch, Q, Count,
    Func, Value as V, CharField
)
from django.db.models.functions import Cast, NullIf, Trim, Replace

# 若有 Postgres，可用正規式替換；沒有就保持 None
try:
    from django.contrib.postgres.search import SearchVector  # 不是必須，只是避免匯入錯
    from django.contrib.postgres.functions import RegexpReplace as PGRegexpReplace
except Exception:
    PGRegexpReplace = None
CITY_N2S = [
    "需清淤地區","光復鄉糖廠","花蓮縣光復車站以外火車站","花蓮縣光復鄉","基隆市","台北市","新北市","桃園市","新竹市","新竹縣","苗栗縣",
    "台中市","彰化縣","南投縣","雲林縣",
    "嘉義市","嘉義縣",
    "台南市","高雄市","屏東縣",
    "宜蘭縣","花蓮縣","台東縣",
    "澎湖縣","金門縣","連江縣",
]

def fare_text_to_int(field_name: str = "fare_note"):
    vendor = connection.vendor  # 'postgresql' | 'mysql' | 'sqlite' | 'oracle'...

    if vendor == "postgresql" and PGRegexpReplace is not None:
        cleaned = PGRegexpReplace(F(field_name), r"[^0-9]+", Value(""))
    elif vendor == "mysql":
        cleaned = Func(F(field_name), Value(r"[^0-9]+"), Value(""), function="REGEXP_REPLACE")
    else:
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
    numeric_or_empty = NullIf(cleaned, Value(""))
    as_int = Cast(numeric_or_empty, IntegerField())
    return as_int

def _city_rank_case(field: str = "departure") -> Case:
    # 包含 icontains 規則，較彈性（地名是句子的一部分也吃得到）
    whens = [When(**{f"{field}__icontains": name}, then=Value(idx+1))
             for idx, name in enumerate(CITY_N2S)]
    return Case(*whens, default=Value(999), output_field=IntegerField())

def get_order_by(sort: str | None) -> list[str] | None:
    """
    傳回基本 order_by。遇到 seats_*/fare_* 或 dep_n2s/dep_s2n 這類需要 annotate 的，
    這裡回傳 None 讓 driver_cards_qs 內部自己處理。
    """
    s = (sort or "").strip()
    # 需要 annotate 的排序：交給 driver_cards_qs
    if sort in ("seats_asc", "seats_desc", "fare_asc", "fare_desc"):
        return None
    if sort == "date_asc":
        return ["date", "id"]
    if sort == "dep_n2s":
        return None  # 地理排序一樣在 driver_cards_qs 做 annotate 後排序
    if sort == "dep_s2n":
        return None
    # 預設（日期新→舊）
    return ["-date", "-id"]




# 兼容舊名稱（如果其他地方有用到）
def _dep_rank_case():
    return _city_rank_case("departure")

def _getlist_qs(request, key: str) -> list[str]:
    """GET 支援單值或多選（?key=a&key=b 或 ?key[]=a&key[]=b）。"""
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

FREE_WORDS = ["免費", "免", "待定", "未定", "面議", "不收", "不收費", "free", "Free", "FREE", "0"]
def _free_note_q(field="fare_note"):
    """fare_note 為空/NULL 或包含免費/待定等關鍵字"""
    q = Q(**{f"{field}__isnull": True}) | Q(**{f"{field}": ""})
    for w in FREE_WORDS:
        q |= Q(**{f"{field}__icontains": w})
    return q



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

    filters 支援：
      dep_in, des_in, date_in, ret_in, gender_in (list)
      need_seats (int)
      fare_num (int), fare_mode ('lte'|'gte'), fare_q (str)
      q (整卡片關鍵字)
    """
    DB_ALIAS = "find_db"

    # 1) 起手式
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
    fare_mode  = (f.get("fare_mode") or "").strip()   # 'lte'|'gte'
    fare_q     = (f.get("fare_q") or "").strip()
    qkw        = (f.get("q") or "").strip()

    if dep_in:    qs = qs.filter(departure__in=dep_in)
    if des_in:    qs = qs.filter(destination__in=des_in)
    if date_in:   qs = qs.filter(date__in=date_in)
    if ret_in:    qs = qs.filter(return_date__in=ret_in)
    if gender_in: qs = qs.filter(gender__in=gender_in)

    # 可用座位（剩餘座位）
    want_seat_sort = sort in ("seats_desc", "seats_asc") or (need_seats is not None)
    if want_seat_sort:
        qs = qs.annotate(
            remaining=ExpressionWrapper(
                F("seats_total") - F("seats_filled"),
                output_field=IntegerField()
            )
        )
    if need_seats is not None:
        qs = qs.filter(remaining__gte=int(need_seats) if want_seat_sort else
                       (F("seats_total") - F("seats_filled") >= int(need_seats)))

    # available 已在上面條件式可能被 annotate；若未 annotate 就補上以供排序
    if sort in ("seats_asc", "seats_desc") and "available" not in qs.query.annotations:
        qs = qs.annotate(
            available=ExpressionWrapper(F("seats_total") - F("seats_filled"), output_field=IntegerField())
        )
    # 酌收費用（若你有 fare_amount 直接用；否則退回文字排序）
    has_fare_amount = hasattr(DriverTrip, "fare_amount")
    has_fare_amount_field = hasattr(DriverTrip, "fare_amount")
    need_fare_sort = sort in ("fare_desc", "fare_asc")
    if need_fare_sort:
        if has_fare_amount_field:
            fare_value_field = F("fare_amount")
        else:
            # 你已有的輔助：把文字金額轉 int，沒有就 None
            qs = qs.annotate(fare_num_annot=fare_text_to_int("fare_note"))
            fare_value_field = F("fare_num_annot")

        # 定義「免費/待定/待議/AA/未定」的判斷
        free_q = (
            Q(fare_note__iregex=r"(免費|待定|待議|AA|未定)")
            | Q(fare_note__isnull=True)
            | Q(fare_note__exact="")
            | Q(fare_amount__isnull=True) if has_fare_amount_field else Q(fare_num_annot__isnull=True)
        )

        # 依方向給 rank：asc 要把免費放最前；desc 放最後
        if sort == "fare_asc":
            # 免費=0，數字=1
            qs = qs.annotate(
                fare_rank=Case(When(free_q, then=Value(0)), default=Value(1), output_field=IntegerField())
            )
            # 免費群排最前，再依數字由低到高
            qs = qs.order_by("fare_rank", fare_value_field.asc(nulls_last=True), "id")
        else:
            # 數字=0，免費=1（排最後）
            qs = qs.annotate(
                fare_rank=Case(When(free_q, then=Value(1)), default=Value(0), output_field=IntegerField())
            )
            # 先數字群由高到低，再免費群
            qs = qs.order_by("fare_rank", fare_value_field.desc(nulls_last=True), "id")
    # ------------ 決定排序（其餘項目）------------
    elif order_by:
        qs = qs.order_by(*order_by)

    else:
        if sort == "date_asc":
            qs = qs.order_by("date", "id")
        elif sort in (None, "", "date_desc"):
            qs = qs.order_by("-date", "-id")

        elif sort == "dep_n2s":
            qs = qs.annotate(dep_rank=_city_rank_case("departure")).order_by("dep_rank", "date", "id")
        elif sort == "dep_s2n":
            qs = qs.annotate(dep_rank=_city_rank_case("departure")).order_by("-dep_rank", "date", "id")

        elif sort == "seats_asc":
            # 少→多：available 由小到大
            qs = qs.order_by("available", "id")
        elif sort == "seats_desc":
            # 多→少：available 由大到小
            qs = qs.order_by("-available", "id")

        else:
            qs = qs.order_by("-date", "-id")


    # 有數字欄位：直接用數字，同時做一個「是否免費/待定」旗標，排序/篩選會用到
    if has_fare_amount:
        qs = qs.annotate(
            fare_num  = Coalesce(F("fare_amount"), Value(0)),
            fare_free = Case(
                When(_free_note_q("fare_note"), then=Value(1)),  # 1=免費/待定
                default=Value(0),
                output_field=IntegerField(),
            ),
        )
    else:
        # 沒數字欄位：用文字排序備援，另外一樣標 free 旗標
        qs = qs.annotate(
            fare_text = Coalesce(F("fare_note"), Value("")),
            fare_free = Case(
                When(_free_note_q("fare_note"), then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            ),
        )

    # ---------- 金額門檻過濾 ----------
    # 要求：當 mode == 'lte' (小於等於) 時，免費/待定也要被包含
    if fare_num is not None and (fare_mode in ("lte", "gte")):
        if has_fare_amount:
            # 數字比較
            base = Q(**{f"fare_num__{fare_mode}": int(fare_num)})
            # <= 門檻時把免費/待定也算進來
            if fare_mode == "lte":
                base |= Q(fare_free=1)
            qs = qs.filter(base)
        else:
            # 沒有 fare_amount 無法做數字比較：
            # 只在 <= 時把免費/待定納入（>= 對純文字沒意義，忽略）
            if fare_mode == "lte":
                qs = qs.filter(_free_note_q("fare_note"))

    # 關鍵字（例如「免費」「AA」）
    if fare_q and hasattr(DriverTrip, "fare_note"):
        qs = qs.filter(fare_note__icontains=fare_q)

    # 整卡片關鍵字搜尋（多詞 AND）
    if qkw:
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
        # 若是 YYYY-MM-DD 也比對日期等於
        if len(qkw) <= 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", qkw):
            qs = qs.filter(Q(date=qkw) | Q(return_date=qkw)) | qs
        qs = qs.distinct()

    # 4) 預抓乘客：pending_list / accepted_list
    pending_qs  = PassengerRequest.objects.using(DB_ALIAS).filter(is_matched=False).order_by("-id")
    accepted_qs = PassengerRequest.objects.using(DB_ALIAS).filter(is_matched=True ).order_by("-id")

    return qs.prefetch_related(
        Prefetch("passengers", queryset=pending_qs,  to_attr="pending_list"),
        Prefetch("passengers", queryset=accepted_qs, to_attr="accepted_list"),
    )

def _extract_filters_from_request(request):
    # 你現成的那支即可；這裡保留常見 keys
    q = request.GET.get("q", "").strip()
    dep_in = request.GET.getlist("dep")
    des_in = request.GET.getlist("des")
    date_in = request.GET.getlist("date")
    ret_in = request.GET.getlist("ret")
    gender_in = request.GET.getlist("gender")
    need_seats = request.GET.get("need_seats")
    fare_mode = (request.GET.get("fare_mode") or "").strip()
    fare_num = request.GET.get("fare_num")
    fare_q   = request.GET.get("fare", "")
    return {
        "q": q or None,
        "dep_in": dep_in or None,
        "des_in": des_in or None,
        "date_in": date_in or None,
        "ret_in": ret_in or None,
        "gender_in": gender_in or None,
        "need_seats": int(need_seats) if (need_seats and need_seats.isdigit()) else None,
        "fare_mode": fare_mode if fare_mode in ("lte", "gte") else None,
        "fare_num": int(fare_num) if (fare_num and fare_num.isdigit()) else None,
        "fare_q": fare_q or None,
    }


def get_active_location_choices():
    """
    只統計「上架中的司機」的出發地/目的地清單與數量。
    以地理順序（_city_rank_case）排序，再以字母作次序。
    回傳：(DEP_CHOICES, DES_CHOICES, DEP_WITH_COUNT, DES_WITH_COUNT)
    """
    base = DriverTrip.objects.using("find_db").filter(is_active=True)

    dep_ct = (
        base.exclude(departure__isnull=True)
            .exclude(departure__exact="")
            .values("departure")
            .annotate(n=Count("id"))
            .annotate(rank=_city_rank_case("departure"))
            .order_by("rank", "departure")
    )
    des_ct = (
        base.exclude(destination__isnull=True)
            .exclude(destination__exact="")
            .values("destination")
            .annotate(n=Count("id"))
            .annotate(rank=_city_rank_case("destination"))
            .order_by("rank", "destination")
    )

    DEP_WITH_COUNT = [(row["departure"], row["n"])   for row in dep_ct]
    DES_WITH_COUNT = [(row["destination"], row["n"]) for row in des_ct]

    DEP_CHOICES = [name for name, _ in DEP_WITH_COUNT]
    DES_CHOICES = [name for name, _ in DES_WITH_COUNT]
    return DEP_CHOICES, DES_CHOICES, DEP_WITH_COUNT, DES_WITH_COUNT



# === view ===
def index(request):
    sort = request.GET.get("sort", "date_desc")
    order_by = get_order_by(sort)

    # 組 filters
    filters = _extract_filters_from_request(request)

    # 司機卡片（已 prefetch pending_list / accepted_list）
    drivers = driver_cards_qs(
        only_active=True,
        order_by=order_by,
        sort=sort,
        filters=filters,
    )

    # 供日期多選用的選項（純字串）
    DATE_CHOICES = sorted({ d.date.isoformat() for d in drivers if getattr(d, "date", None) })
    RET_CHOICES  = sorted({ d.return_date.isoformat() for d in drivers if getattr(d, "return_date", None) })

    # 乘客（左上角區塊）
    passengers = (
        PassengerRequest.objects.using("find_db")
        .filter(is_matched=False, driver__isnull=True)
        .order_by("-id")
    )

    # 出發地/目的地（多選來源）＋數量（用地理排序）
    base = DriverTrip.objects.using("find_db").filter(is_active=True)

    dep_ct = (
        base.exclude(departure__isnull=True).exclude(departure__exact="")
            .values("departure")
            .annotate(n=Count("id"))
            .annotate(rank=_city_rank_case("departure"))
            .order_by("rank", "departure")
    )
    des_ct = (
        base.exclude(destination__isnull=True).exclude(destination__exact="")
            .values("destination")
            .annotate(n=Count("id"))
            .annotate(rank=_city_rank_case("destination"))
            .order_by("rank", "destination")
    )

    DEP_WITH_COUNT = [(row["departure"], row["n"])   for row in dep_ct]
    DES_WITH_COUNT = [(row["destination"], row["n"]) for row in des_ct]
    DEP_CHOICES = [name for name, _ in DEP_WITH_COUNT]
    DES_CHOICES = [name for name, _ in DES_WITH_COUNT]

    # ▶▶ 如果是部分請求（AJAX / _partial=1），只回傳司機清單的 HTML
    is_partial = request.GET.get("_partial") == "1" or request.headers.get("x-requested-with") == "XMLHttpRequest"
    if is_partial:
        drivers_html = render_to_string("Find/_driver_list.html", {"drivers": drivers}, request)
        return JsonResponse({
            "ok": True,
            "drivers_html": drivers_html,
            # 若你想一起回傳篩選來源（例如「動態數量」），也可以加在這裡
            # "dep_with_count": DEP_WITH_COUNT,
            # "des_with_count": DES_WITH_COUNT,
        })

    # ---------- AJAX：回傳 partial（不刷新整頁） ----------
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        drivers_html = render_to_string("Find/_driver_list.html", {"drivers": drivers})
        passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})
        return JsonResponse({
            "type": "send.update",
            "drivers_html": drivers_html,
            "passengers_html": passengers_html,
            "sort": sort,
        })

    # ---------- 首次載入：整頁 render ----------
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


BOOL_TRUE = ("1", "true", "on", "yes")

@require_POST
def join_driver(request, driver_id: int):
    is_ajax = (request.headers.get("x-requested-with") == "XMLHttpRequest")

    # ===== 表單值 =====
    passenger_name = (request.POST.get("passenger_name", "").strip() or "匿名")
    gender         = (request.POST.get("gender", "X"))
    email          = (request.POST.get("email") or "").strip() or None
    contact        = (request.POST.get("contact", "").strip())

    # 使用者表單勾選
    want_hide = (request.POST.get("hide_contact") or "0").lower() in BOOL_TRUE
    want_auto = (request.POST.get("auto_email_contact") or "0").lower() in BOOL_TRUE

    departure = (request.POST.get("departure") or
                 request.POST.get("custom_departure") or "").strip()

    raw_pay = (request.POST.get("willing_to_pay") or "待定或免費").strip()
    try:
        seats_needed = max(1, int(request.POST.get("seats_needed", "1") or 1))
    except (TypeError, ValueError):
        seats_needed = 1

    willing_to_pay = None
    if raw_pay:
        try:
            from decimal import Decimal
            willing_to_pay = Decimal(raw_pay)
        except Exception:
            willing_to_pay = None

    # 三態一起回程
    tr_raw = (request.POST.get("together_return") or "").strip().lower()
    if   tr_raw == "true":  together_return = True
    elif tr_raw == "false": together_return = False
    else:                   together_return = None

    # ===== 交易 + 鎖行避免搶位 =====
    with transaction.atomic(using="find_db"):
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

        # --- 新規則：若司機關閉自動發信，乘客端必須強制開啟 ---
        driver_auto = bool(getattr(d, "auto_email_contact", False))

        if not driver_auto:
            # 司機關閉 → 乘客必須提供 Email，且強制開啟 隱藏+自動寄信
            if not email:
                msg = "司機未啟用自動寄信，乘客需提供 Email 並啟用自動寄信。"
                return JsonResponse({"ok": False, "error": msg}, status=400) if is_ajax else redirect("find_index")
            hide_contact_final       = True
            auto_email_contact_final = True
        else:
            # 司機有自動寄信 → 依照乘客自己的選擇與原有規則
            if want_hide and not email:
                msg = "要隱藏個資，請先填 Email"
                return JsonResponse({"ok": False, "error": msg}, status=400) if is_ajax else redirect("find_index")
            if want_auto and not (email and want_hide):
                msg = "要啟用自動寄信，需先填 Email 並勾選隱藏個資"
                return JsonResponse({"ok": False, "error": msg}, status=400) if is_ajax else redirect("find_index")

            hide_contact_final       = bool(email) and want_hide
            auto_email_contact_final = bool(email) and hide_contact_final and want_auto

        # 建立「待確認」乘客
        p = PassengerRequest.objects.using("find_db").create(
            passenger_name   = passenger_name,
            gender           = gender,
            email            = email,
            contact          = contact,
            seats_needed     = seats_needed,
            willing_to_pay   = willing_to_pay,
            departure        = departure,
            destination      = (request.POST.get("destination", "").strip()),
            date             = (request.POST.get("date") or d.date),
            return_date      = (request.POST.get("return_date") or None),
            together_return  = together_return,
            note             = (request.POST.get("note", "").strip()),
            password         = (request.POST.get("password", "0000").strip() or "0000"),
            driver           = d,
            is_matched       = False,
            hide_contact       = hide_contact_final,        # ← 入庫
            auto_email_contact = auto_email_contact_final,  # ← 入庫
        )

    # ===== 提交後廣播 =====
    channel_layer = get_channel_layer()

    pending_qs  = PassengerRequest.objects.using("find_db").filter(is_matched=False).order_by("-id")
    accepted_qs = PassengerRequest.objects.using("find_db").filter(is_matched=True ).order_by("-id")

    drivers_qs = (
        DriverTrip.objects.using("find_db")
        .filter(is_active=True)
        .prefetch_related(
            Prefetch("passengers", queryset=pending_qs,  to_attr="pending_list"),
            Prefetch("passengers", queryset=accepted_qs, to_attr="accepted_list"),
        )
    )
    drivers = list(drivers_qs)

    passengers = (
        PassengerRequest.objects.using("find_db")
        .filter(is_matched=False, driver__isnull=True)
        .order_by("-id")
    )

    drivers_html    = render_to_string("Find/_driver_list.html", {"drivers": drivers})
    passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})

    async_to_sync(channel_layer.group_send)(
        "find_group",
        {"type": "send.update", "drivers_html": drivers_html, "passengers_html": passengers_html},
    )
    # ✅ 交易提交後再寄信（綁定 *單筆* 乘客物件 p）
    #transaction.on_commit(lambda d_id=d.id, p_id=p.id: _notify_join_by_ids(d_id, p_id))
    transaction.on_commit(lambda d_id=d.id, p_id=p.id: enqueue_join_emails_after_commit(d_id, p_id))
    try:
        broadcast_driver_card(driver_id)
        broadcast_manage_panels(driver_id)
        #transaction.on_commit(lambda: _broadcast_after_change(driver_id))
    except Exception:
        pass

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


def _fmt_driver_contact(d):
    lines = [
        f"司機暱稱：{d.driver_name}",
        f"行程：{d.departure} → {d.destination}",
        f"出發日期：{getattr(d, 'date', '') or '未定'}",
    ]
    if getattr(d, "return_date", None):
        lines.append(f"回程日期：{d.return_date}")
    if getattr(d, "contact", ""):
        lines.append(f"司機聯絡方式：{d.contact}")
    return "\n".join(lines)

def _fmt_passenger_info(p):
    lines = [
        f"乘客暱稱：{p.passenger_name}",
        f"性別：{p.get_gender_display() if hasattr(p, 'get_gender_display') else p.gender}",
        f"上車地點：{p.departure or '未填'}",
        f"目的地：{p.destination or '未填'}",
        f"人數：{p.seats_needed}",
        f"出發日期：{p.date or '未填'}",
    ]
    if getattr(p, "return_date", None):
        lines.append(f"回程日期：{p.return_date}")
    if getattr(p, "willing_to_pay", None):
        lines.append(f"願付金額：NT$ {p.willing_to_pay}")
    # 乘客聯絡方式（只有在要寄出的幾種情境才會包含；這裡先備好）
    if getattr(p, "contact", ""):
        lines.append(f"乘客聯絡方式：{p.contact}")
    return "\n".join(lines)

def _send_mail(subject, body_text, to, reply_to=None):
    """
    輕量寄信工具：同時送純文字與簡單 HTML（防止信件過白）。
    `to` 可放單一 email 字串或 list。
    """
    if not to:
        return
    to_list = [to] if isinstance(to, str) else list(to)
    html_body = "<pre style='font-family:system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; white-space:pre-wrap; line-height:1.5'>" + body_text + "</pre>"
    msg = EmailMultiAlternatives(
        subject=subject,
        body=strip_tags(html_body),  # 純文字
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        to=to_list,
        reply_to=[reply_to] if reply_to else None,
    )
    msg.attach_alternative(html_body, "text/html")
    msg.send(fail_silently=True)

def notify_on_passenger_join(driver, passenger):
    """
    乘客報名完成後的通知邏輯（對司機、對乘客）
    覆蓋六種情境；需要模型欄位：
      - driver.email / driver.hide_contact / driver.auto_email_contact
      - passenger.email / passenger.hide_contact / passenger.auto_email_contact
      - passenger.contact（乘客的聯絡方式）
    """
    # 資料存在與否
    drv_email = bool(getattr(driver, "email", None))
    pax_email = bool(getattr(passenger, "email", None))

    drv_hide  = bool(getattr(driver, "hide_contact", False))
    drv_auto  = bool(getattr(driver, "auto_email_contact", False))  # 你有加這欄位

    pax_hide  = bool(getattr(passenger, "hide_contact", False))
    pax_auto  = bool(getattr(passenger, "auto_email_contact", False))

    # ---------- 通知司機（來自乘客報名） ----------
    # 規則 1：乘客隱藏 + 自動寄信 + 司機有 Email → 寄「乘客完整報名資料（含乘客聯絡方式）」給司機
    if pax_hide and pax_auto and drv_email:
        subject = f"【新報名】{passenger.passenger_name} 報名了你的行程（含乘客聯絡方式）"
        body = "\n".join([
            "乘客已選擇隱藏個資，且開啟自動寄信功能。",
            "以下為乘客報名資料：",
            "",
            _fmt_passenger_info(passenger),
            "",
            "（本信含乘客聯絡方式，請勿轉傳給第三方）",
        ])
        _send_mail(subject, body, to=driver.email, reply_to=passenger.email if pax_email else None)

    # 規則 2：乘客隱藏 + 關閉自動寄信 → 只寄「報名通知（不含乘客聯絡方式）」給司機（需司機有 Email）
    elif pax_hide and not pax_auto and drv_email:
        subject = f"【新報名】{passenger.passenger_name} 報名了你的行程"
        # 不放乘客聯絡方式
        base = _fmt_passenger_info(passenger).splitlines()
        base = [line for line in base if not line.startswith("乘客聯絡方式：")]
        body = "\n".join([
            "乘客已選擇隱藏個資，且關閉自動寄信。",
            "目前僅通知你有人報名；乘客聯絡方式將在你接受後由系統再行通知（或請在系統內聯絡）。",
            "",
            "\n".join(base),
        ])
        _send_mail(subject, body, to=driver.email)

    # 規則 3：乘客未隱藏 → 直接把「乘客完整報名資料」寄給司機（需司機有 Email）
    elif not pax_hide and drv_email:
        subject = f"【新報名】{passenger.passenger_name} 報名了你的行程（含聯絡方式）"
        body = "\n".join([
            "以下為乘客報名資料：",
            "",
            _fmt_passenger_info(passenger),
        ])
        _send_mail(subject, body, to=driver.email, reply_to=passenger.email if pax_email else None)

    # 沒司機 Email → 無法寄給司機，直接略過（或你要 log 也可）

    # ---------- 通知乘客（來自司機設定） ----------
    # 規則 4：司機隱藏 + 開啟自動寄信 + 乘客有 Email → 寄「司機聯絡方式」給乘客
    if drv_hide and drv_auto and pax_email:
        subject = f"【聯絡方式】你報名的司機（{driver.driver_name}）聯絡資料"
        body = "\n".join([
            "司機目前隱藏聯絡方式，已啟用自動寄信功能。",
            "以下為司機聯絡資訊：",
            "",
            _fmt_driver_contact(driver),
            "",
            "（請妥善保存，不要公開轉傳）",
        ])
        _send_mail(subject, body, to=passenger.email, reply_to=driver.email if drv_email else None)

    # 規則 5：司機隱藏 + 關閉自動寄信 → 不動作
    # 規則 6：司機未隱藏 → 不動作（聯絡方式已公開在卡片

