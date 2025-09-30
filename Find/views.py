from decimal import Decimal, InvalidOperation

from django.http import JsonResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.db import transaction
from django.views.decorators.http import require_POST
from django.template.loader import render_to_string
from django.db.models import Prefetch
from django.core.mail import send_mail
from django.utils.dateparse import parse_date

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .models import DriverTrip, PassengerRequest
from django.utils import timezone


def _broadcast_lists():
    """
    重新產出 drivers / passengers 的片段，廣播到 group。
    """
    channel_layer = get_channel_layer()

    pending_qs  = PassengerRequest.objects.using("find_db").filter(is_matched=False).order_by("-id")
    accepted_qs = PassengerRequest.objects.using("find_db").filter(is_matched=True ).order_by("-id")

    drivers = (
        DriverTrip.objects.using("find_db")
        .filter(is_active=True)
        .prefetch_related(
            Prefetch("passengers", queryset=pending_qs,  to_attr="pending"),
            Prefetch("passengers", queryset=accepted_qs, to_attr="accepted"),
        )
    )

    passengers = PassengerRequest.objects.using("find_db").filter(
        is_matched=False, driver__isnull=True
    ).order_by("-id")

    drivers_html    = render_to_string("Find/_driver_list.html",    {"drivers": drivers})
    passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})

    async_to_sync(channel_layer.group_send)(
        "find_group",
        {"type": "send.update", "drivers_html": drivers_html, "passengers_html": passengers_html},
    )

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
def pax_update(request, pid: int):
    """更新乘客資料（需要先通過 pax_auth）。"""
    p = get_object_or_404(PassengerRequest.objects.using("find_db"), id=pid)
    if not _pax_authorized(request, pid):
        return JsonResponse({"ok": False, "error": "未授權"}, status=403)

    # 更新欄位
    p.passenger_name = request.POST.get("passenger_name", p.passenger_name).strip() or p.passenger_name
    p.gender         = request.POST.get("gender", p.gender)
    p.email          = request.POST.get("email") or None
    p.contact        = request.POST.get("contact", p.contact).strip()
    # seats
    try:
        p.seats_needed = int(request.POST.get("seats_needed", p.seats_needed))
    except (TypeError, ValueError):
        pass
    # willing_to_pay
    wpay = request.POST.get("willing_to_pay")
    p.willing_to_pay = (wpay if wpay not in (None, "",) else None)

    p.departure    = request.POST.get("departure", p.departure).strip()
    p.destination  = request.POST.get("destination", p.destination).strip()

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

    p.note = request.POST.get("note", p.note).strip()

    p.save(using="find_db")
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
    return JsonResponse({"ok": True})



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

@require_POST
def driver_manage_auth(request, driver_id: int):
    """
    接收密碼，驗證正確就回傳管理頁 URL 讓前端跳轉
    """
    driver = get_object_or_404(DriverTrip.objects.using("find_db"), id=driver_id)
    password = request.POST.get("password", "")
    if not password:
        return JsonResponse({"ok": False, "error": "請輸入密碼"}, status=400)

    if password != driver.password:
        return JsonResponse({"ok": False, "error": "密碼錯誤"}, status=403)

    url = reverse("driver_manage", args=[driver_id])
    return JsonResponse({"ok": True, "url": url})


def driver_manage(request, driver_id: int):
    # 抓司機 + 乘客
    driver = (DriverTrip.objects.using("find_db")
              .prefetch_related("passengers")
              .get(id=driver_id))

    # 先把列表算好，頁面一進來就能顯示
    attach_passenger_lists(driver)

    # 下半部：候選乘客（同路線同一天、尚未媒合、且尚未指派 driver）
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

        # ============ A) 更新司機資訊 ============
        if form_type == "update_driver":
            driver.driver_name = request.POST.get("driver_name", driver.driver_name).strip()
            driver.gender      = request.POST.get("gender", driver.gender)
            driver.email       = request.POST.get("email") or None
            driver.contact     = request.POST.get("contact", driver.contact).strip()

            new_password = request.POST.get("password")
            if new_password:
                driver.password = new_password

            # 座位
            try:
                seats_total = int(request.POST.get("seats_total", driver.seats_total))
            except (TypeError, ValueError):
                seats_total = driver.seats_total
            driver.seats_total = max(1, seats_total)
            if driver.seats_filled > driver.seats_total:
                driver.seats_filled = driver.seats_total

            # 起訖、日期
            driver.departure   = request.POST.get("departure", driver.departure).strip()
            driver.destination = request.POST.get("destination", driver.destination).strip()
            driver.date        = request.POST.get("date") or driver.date
            driver.return_date = request.POST.get("return_date") or None

            # 順路意願 / 上架
            driver.flexible_pickup = request.POST.get("flexible_pickup", getattr(driver, "flexible_pickup", "MAYBE"))
            driver.is_active       = (request.POST.get("is_active") == "on")
            if driver.seats_filled >= driver.seats_total:
                driver.is_active = False

            driver.save(using="find_db")
            saved_msg = "✅ 已更新司機資料"

        # ============ B) 接受乘客（把 `driver` 指給乘客並設 is_matched=True） ============
        elif form_type == "accept_passengers":
            ids = request.POST.getlist("accept_ids")
            accepted_names = []

            with transaction.atomic(using="find_db"):
                d = (DriverTrip.objects.using("find_db")
                     .select_for_update()
                     .get(id=driver.id))

                for pid in ids:
                    try:
                        p = (PassengerRequest.objects.using("find_db")
                             .select_for_update()
                             .get(id=pid, is_matched=False))
                    except PassengerRequest.DoesNotExist:
                        continue

                    # 若該乘客還沒綁 driver，幫他綁上（加入這位司機的待確認或直接接受）
                    if p.driver_id is None:
                        p.driver = d

                    # 座位足夠就接受
                    if d.seats_filled + p.seats_needed <= d.seats_total:
                        d.seats_filled += p.seats_needed
                        p.is_matched = True
                        p.save(using="find_db")
                        accepted_names.append(p.passenger_name)

                        if d.seats_filled >= d.seats_total:
                            d.is_active = False
                            d.save(using="find_db")
                            full_msg = f"🚗 {d.driver_name} 的行程已滿，已自動下架"
                            break

                d.save(using="find_db")

            matched_msg = "✅ 已成功媒合：" + "、".join(accepted_names) if accepted_names else "⚠️ 沒有可媒合的乘客或座位不足"

        # 其他表單略…

        # 重新抓候選乘客與列表（提交後頁面要即時反映）
        candidates = (PassengerRequest.objects.using("find_db")
                      .filter(departure=driver.departure,
                              destination=driver.destination,
                              date=driver.date,
                              is_matched=False,
                              driver__isnull=True)
                      .order_by("id"))

        driver.refresh_from_db(using="find_db")
        driver = (DriverTrip.objects.using("find_db")
                  .prefetch_related("passengers")
                  .get(id=driver.id))
        attach_passenger_lists(driver)

    return render(request, "Find/driver_manage.html", {
        "driver": driver,
        "pending": driver.pending,    # ✅ 模板可直接用
        "accepted": driver.accepted,  # ✅ 模板可直接用
        "candidates": candidates,
        "saved_msg": saved_msg,
        "matched_msg": matched_msg,
        "full_msg": full_msg,
    })
# -------------------
# 首頁
# -------------------
def index(request):
    drivers = build_driver_cards()
    passengers = (PassengerRequest.objects.using("find_db")
                  .filter(is_matched=False, driver__isnull=True))  # ✅ 只顯示「尚未指定司機」的找人需求
    return render(request, "Find/index.html", {
        "drivers": drivers,
        "passengers": passengers,
    })



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
        name = request.POST.get("name")
        contact = request.POST.get("contact")
        email = request.POST.get("email")
        password = request.POST.get("password")
        gender = request.POST.get("gender")
        seats_total = int(request.POST.get("seats_total"))
        
        # 出發地處理：先選單，若有輸入自訂的，覆蓋掉
        departure = request.POST.get("departure")
        departure_custom = request.POST.get("departure_custom")
        if departure_custom:
            departure = departure_custom

        destination = request.POST.get("destination")
        date = request.POST.get("date")
        return_date = request.POST.get("return_date") or None
        flexible_pickup = request.POST.get("flexible_pickup")
        note = request.POST.get("note")

        DriverTrip.objects.using("find_db").create(
            driver_name=name,
            contact=contact,
            email=email,
            password=password,
            gender=gender,
            seats_total=seats_total,
            departure=departure,
            destination=destination,
            date=date,
            return_date=return_date,
            flexible_pickup=flexible_pickup,
            note=note,
        )
        return redirect("find_index")  # ✅ 送出後回首頁

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
    d = get_object_or_404(DriverTrip.objects.using("find_db"), id=driver_id)

    # 先拿 departure（隱藏欄位），若沒填再回退 custom_departure
    departure = (request.POST.get("departure") or
                 request.POST.get("custom_departure") or "").strip()

    # 願付金額處理為 Decimal 或 None
    raw_pay = (request.POST.get("willing_to_pay") or "").strip()
    willing_to_pay = None
    if raw_pay:
        try:
            willing_to_pay = Decimal(raw_pay)
        except Exception:
            willing_to_pay = None

    p = PassengerRequest.objects.using("find_db").create(
        passenger_name = request.POST.get("passenger_name", "").strip() or "匿名",
        gender         = request.POST.get("gender", "X"),
        email          = request.POST.get("email") or None,
        contact        = request.POST.get("contact", "").strip(),
        seats_needed   = int(request.POST.get("seats_needed", "1") or 1),
        willing_to_pay = willing_to_pay,
        departure      = departure,
        destination    = request.POST.get("destination", "").strip(),
        date           = request.POST.get("date") or d.date,
        return_date    = request.POST.get("return_date") or None,
        note           = request.POST.get("note", "").strip(),
        password       = request.POST.get("password", "0000").strip() or "0000",
        driver         = d,
        is_matched     = False,
    )

    # ✅ 即時更新（透過 Channels 廣播整個清單片段）
    channel_layer = get_channel_layer()

    # 重新計算 drivers / passengers 給片段
    pending_qs  = PassengerRequest.objects.using("find_db").filter(is_matched=False).order_by("-id")
    accepted_qs = PassengerRequest.objects.using("find_db").filter(is_matched=True).order_by("-id")
    drivers = (
        DriverTrip.objects.using("find_db")
        .filter(is_active=True)
        .prefetch_related(
            Prefetch("passengers", queryset=pending_qs,  to_attr="pending"),
            Prefetch("passengers", queryset=accepted_qs, to_attr="accepted"),
        )
    )
    passengers = PassengerRequest.objects.using("find_db").filter(
        is_matched=False, driver__isnull=True
    ).order_by("-id")

    drivers_html = render_to_string("Find/_driver_list.html", {"drivers": drivers})
    passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})

    async_to_sync(channel_layer.group_send)(
        "find_group",
        {
            "type": "send.update",
            "drivers_html": drivers_html,
            "passengers_html": passengers_html,
        },
    )

    # 可回到首頁或回傳 JSON 讓前端 toast 與關閉 modal
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
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


def broadcast_update(message):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        "find_updates",
        {"type": "send_update", "message": message}
    )
