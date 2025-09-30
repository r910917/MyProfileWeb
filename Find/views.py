from decimal import Decimal, InvalidOperation

from django.http import JsonResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.db import transaction
from django.views.decorators.http import require_POST
from django.template.loader import render_to_string

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .models import DriverTrip, PassengerRequest



def _json_or_redirect(request, ok=True, **payload):
    """
    依請求決定回傳 JSON 或 Redirect
    - 若是 AJAX（有 X-Requested-With）回 JSON
    - 否則回首頁
    """
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if is_ajax:
        return JsonResponse({"ok": ok, **payload}, status=200 if ok else 400)
    # 非 AJAX：成功回首頁；失敗也回首頁但可以帶訊息（略）
    return redirect("find_index")

def _broadcast_update():
    """重新渲染 partials，透過 Channels 廣播給所有使用者"""
    passengers = PassengerRequest.objects.using("find_db").filter(is_matched=False).order_by("-id")
    drivers    = DriverTrip.objects.using("find_db").filter(is_active=True).order_by("-id")

    passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})
    drivers_html    = render_to_string("Find/_driver_list.html", {"drivers": drivers})

    layer = get_channel_layer()
    async_to_sync(layer.group_send)(
        "find_group",
        {
            "type": "send_update",
            "passengers_html": passengers_html,
            "drivers_html": drivers_html,
        },
    )

@require_POST
def join_driver(request, driver_id: int):
    driver = get_object_or_404(DriverTrip.objects.using("find_db"), id=driver_id)

    p = PassengerRequest.objects.using("find_db").create(
        passenger_name = request.POST.get("passenger_name","").strip(),
        gender        = request.POST.get("gender","X"),
        email         = request.POST.get("email") or None,
        contact       = request.POST.get("contact","").strip(),
        seats_needed  = int(request.POST.get("seats_needed", 1)),
        willing_to_pay= request.POST.get("willing_to_pay") or None,
        departure     = request.POST.get("departure","").strip(),
        destination   = request.POST.get("destination","").strip(),
        date          = request.POST.get("date") or driver.date,    # ✅ 若沒填就用司機日期
        return_date   = request.POST.get("return_date") or None,
        note          = request.POST.get("note","").strip(),
        password      = request.POST.get("password","0000"),       # ✅ 乘客管理密碼
        is_matched    = False,                                      # ✅ 待司機確認
        driver        = driver,                                     # ✅ 掛到此司機
    )

    # 這裡你已有 signals/WS 會重繪清單，就回 JSON 即可
    return JsonResponse({"ok": True})

@require_POST
def passenger_update(request, pk: int):
    p = get_object_or_404(PassengerRequest.objects.using("find_db"), pk=pk)
    passwd = request.POST.get("password","")
    if passwd != p.password:
        return JsonResponse({"ok": False, "error": "密碼錯誤"}, status=403)

    # 可修改欄位（按你想開放的為準）
    p.passenger_name = request.POST.get("passenger_name", p.passenger_name).strip()
    p.gender         = request.POST.get("gender", p.gender)
    p.email          = request.POST.get("email") or None
    p.contact        = request.POST.get("contact", p.contact).strip()
    p.seats_needed   = int(request.POST.get("seats_needed", p.seats_needed) or p.seats_needed)
    p.willing_to_pay = request.POST.get("willing_to_pay") or None
    p.departure      = request.POST.get("departure", p.departure).strip()
    p.destination    = request.POST.get("destination", p.destination).strip()
    p.return_date    = request.POST.get("return_date") or None
    p.note           = request.POST.get("note", p.note).strip()
    p.save(using="find_db")

    return JsonResponse({"ok": True})

@require_POST
def passenger_delete(request, pk: int):
    p = get_object_or_404(PassengerRequest.objects.using("find_db"), pk=pk)
    passwd = request.POST.get("password","")
    if passwd != p.password:
        return JsonResponse({"ok": False, "error": "密碼錯誤"}, status=403)

    with transaction.atomic(using="find_db"):
        # 若已被接受，釋放座位
        if p.is_matched and p.driver_id:
            d = DriverTrip.objects.using("find_db").select_for_update().get(pk=p.driver_id)
            d.seats_filled = max(0, d.seats_filled - p.seats_needed)
            # 若座位恢復可用，可自動重新上架
            d.is_active = True
            d.save(using="find_db")

        p.delete(using="find_db")

    return JsonResponse({"ok": True})


# 產生此司機的 session key
def _driver_session_key(driver_id: int) -> str:
    return f"driver_auth_{driver_id}"

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
    """
    司機管理頁：
    - 上半部：可修改司機資訊（暱稱、性別、Email、聯絡方式、密碼、座位、起訖、出/回程、順路意願、備註、是否上架）
    - 下半部：同路線同日期、尚未媒合的乘客清單，可勾選「接受媒合」
    """
    driver = get_object_or_404(DriverTrip.objects.using("find_db"), id=driver_id)

    # 下半部：候選乘客（同一條路、同一天、尚未媒合）
    candidates = PassengerRequest.objects.using("find_db").filter(
        departure=driver.departure,
        destination=driver.destination,
        date=driver.date,
        is_matched=False
    ).order_by("id")

    saved_msg = ""
    matched_msg = ""
    full_msg = ""

    if request.method == "POST":
        form_type = request.POST.get("form", "")

        # ============ A) 更新司機資訊 ============
        if form_type == "update_driver":
            driver.driver_name   = request.POST.get("driver_name", driver.driver_name).strip()
            driver.gender        = request.POST.get("gender", driver.gender)
            driver.email         = request.POST.get("email") or None
            driver.contact       = request.POST.get("contact", driver.contact).strip()

            new_password = request.POST.get("password")  # 留空就不變更
            if new_password:
                driver.password = new_password

            # 座位
            try:
                seats_total = int(request.POST.get("seats_total", driver.seats_total))
            except (TypeError, ValueError):
                seats_total = driver.seats_total
            driver.seats_total = max(1, seats_total)
            # 若總座位小於已填座位，強制壓到相等
            if driver.seats_filled > driver.seats_total:
                driver.seats_filled = driver.seats_total

            # 起訖、日期
            driver.departure     = request.POST.get("departure", driver.departure).strip()
            driver.destination   = request.POST.get("destination", driver.destination).strip()
            driver.date          = request.POST.get("date") or driver.date
            driver.return_date   = request.POST.get("return_date") or None

            # 順路意願
            driver.flexible_pickup = request.POST.get("flexible_pickup", getattr(driver, "flexible_pickup", "MAYBE"))

            # 是否上架
            driver.is_active = (request.POST.get("is_active") == "on")
            # 若已滿，強制下架
            if driver.seats_filled >= driver.seats_total:
                driver.is_active = False

            driver.save(using="find_db")
            saved_msg = "✅ 已更新司機資料"

        # ============ B) 接受乘客 ============ 
        elif form_type == "accept_passengers":
            ids = request.POST.getlist("accept_ids")
            accepted_names = []

            with transaction.atomic(using="find_db"):
                # 重新抓 driver（避免 race）
                d = DriverTrip.objects.using("find_db").select_for_update().get(id=driver.id)

                for pid in ids:
                    try:
                        p = PassengerRequest.objects.using("find_db").select_for_update().get(id=pid, is_matched=False)
                    except PassengerRequest.DoesNotExist:
                        continue

                    # 還有座位才吃
                    if d.seats_filled + p.seats_needed <= d.seats_total:
                        d.seats_filled += p.seats_needed
                        p.is_matched = True
                        p.save(using="find_db")
                        accepted_names.append(p.passenger_name)

                        # 寄信通知（有填 email 才寄）
                        if d.email or p.email:
                            try:
                                send_mail(
                                    subject="媒合成功：車輛找到乘客",
                                    message=(
                                        f"司機 {d.driver_name} 已接受 {p.passenger_name} 的需求 "
                                        f"{d.departure} → {d.destination}（{d.date}）\n"
                                        f"司機聯絡：{d.contact}\n乘客聯絡：{p.contact}"
                                    ),
                                    from_email=None,
                                    recipient_list=list(filter(None, [d.email, p.email])),
                                    fail_silently=True,
                                )
                            except Exception:
                                pass

                        # 滿了就停止
                        if d.seats_filled >= d.seats_total:
                            d.is_active = False
                            d.save(using="find_db")
                            full_msg = f"🚗 {d.driver_name} 的行程已滿，已自動下架"
                            break

                d.save(using="find_db")

            if accepted_names:
                matched_msg = "✅ 已成功媒合：" + "、".join(accepted_names)
            else:
                matched_msg = "⚠️ 沒有可媒合的乘客或座位不足"

        else:
            return HttpResponseBadRequest("Unknown form type")

        # 重新抓候選乘客清單（避免畫面上還看到已媒合者）
        candidates = PassengerRequest.objects.using("find_db").filter(
            departure=driver.departure,
            destination=driver.destination,
            date=driver.date,
            is_matched=False
        ).order_by("id")

    return render(request, "Find/driver_manage.html", {
        "driver": driver,
        "candidates": candidates,
        "saved_msg": saved_msg,
        "matched_msg": matched_msg,
        "full_msg": full_msg,
    })
# -------------------
# 首頁
# -------------------
def index(request):
    drivers = DriverTrip.objects.using("find_db").filter(is_active=True)
    passengers = PassengerRequest.objects.using("find_db").filter(is_matched=False)
    return render(request, "Find/index.html", {
        "drivers": drivers,
        "passengers": passengers
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

def join_driver(request, driver_id: int):
    if request.method != "POST":
        return HttpResponseBadRequest("Invalid method")

    driver = get_object_or_404(DriverTrip.objects.using("find_db"), id=driver_id)

    # 必填/基本欄位
    passenger_name = (request.POST.get("passenger_name") or "").strip()
    contact        = (request.POST.get("contact") or "").strip()
    if not passenger_name or not contact:
        return HttpResponseBadRequest("缺少必要欄位")

    gender   = request.POST.get("gender") or "X"
    email    = request.POST.get("email") or None
    password = request.POST.get("password") or "0000"

    # 數字欄位
    try:
        seats_needed = int(request.POST.get("seats_needed") or 1)
    except (TypeError, ValueError):
        seats_needed = 1
    seats_needed = max(1, seats_needed)

    # 願付金額（可空）
    willing_to_pay = request.POST.get("willing_to_pay")
    if willing_to_pay in (None, "", "0", "0.0"):
        willing_to_pay = None
    else:
        try:
            willing_to_pay = Decimal(willing_to_pay)
        except (InvalidOperation, TypeError, ValueError):
            willing_to_pay = None

    # 路線與日期
    departure   = (request.POST.get("departure") or "").strip()
    destination = (request.POST.get("destination") or "").strip() or driver.destination

    # **關鍵：一律使用司機的出發日**
    date        = driver.date

    # 回程日期（可空）
    return_date = request.POST.get("return_date") or None

    # 是否一起回程（可空 -> None；有值就 True/False）
    together_raw = request.POST.get("together_return")
    if together_raw in (None, "", "未指定"):
        together_return = None
    else:
        together_return = True if together_raw in ("YES", "true", "True", "1") else False

    note = (request.POST.get("note") or "").strip()

    # 建立乘客需求（先不綁 driver；等司機在管理頁勾選接受才媒合）
    PassengerRequest.objects.using("find_db").create(
        passenger_name = passenger_name,
        contact        = contact,
        email          = email,
        password       = password,
        gender         = gender,
        seats_needed   = seats_needed,
        willing_to_pay = willing_to_pay,
        departure      = departure,
        destination    = destination,
        date           = date,          # ✅ 不為空
        return_date    = return_date,
        note           = note,
        is_matched     = False,
        driver         = None,
        together_return= together_return,
    )

    # 你有 WebSocket 廣播就記得呼叫；這裡先回首頁
    return redirect("find_index")
    

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

def broadcast_update(message):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        "find_updates",
        {"type": "send_update", "message": message}
    )
