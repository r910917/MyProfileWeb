from django.shortcuts import render, redirect, get_object_or_404
from django.core.mail import send_mail
from django.core.exceptions import ObjectDoesNotExist
from .models import DriverTrip, PassengerRequest

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
# 司機管理
# -------------------
def edit_driver(request, driver_id):
    driver = DriverTrip.objects.using("find_db").get(id=driver_id)

    if request.method == "POST":
        password = request.POST.get("password")
        if password == driver.password:
            return redirect("driver_manage", driver_id=driver.id)
        else:
            return render(request, "Find/index.html", {
                "drivers": DriverTrip.objects.using("find_db").filter(is_active=True),
                "passengers": PassengerRequest.objects.using("find_db").filter(is_matched=False),
                "driver_error_id": driver.id,
                "driver_error_msg": "密碼錯誤，請再試一次"
            })

    return redirect("find_index")


def driver_manage(request, driver_id):
    driver = DriverTrip.objects.using("find_db").get(id=driver_id)
    passengers = PassengerRequest.objects.using("find_db").filter(
        departure=driver.departure,
        destination=driver.destination,
        date=driver.date,
        is_matched=False
    )

    if request.method == "POST":
        selected_ids = request.POST.getlist("confirm_passengers")
        for pid in selected_ids:
            try:
                passenger = PassengerRequest.objects.using("find_db").get(id=pid)
            except PassengerRequest.DoesNotExist:
                continue

            if driver.seats_filled + passenger.seats_needed <= driver.seats_total:
                driver.seats_filled += passenger.seats_needed
                passenger.is_matched = True
                passenger.save(using="find_db")

        if driver.seats_filled >= driver.seats_total:
            driver.is_active = False

        driver.save(using="find_db")
        return redirect("find_index")

    return render(request, "Find/driver_manage.html", {
        "driver": driver,
        "passengers": passengers
    })


# -------------------
# 乘客加入司機 (從 match_driver 頁面)
# -------------------
def join_driver(request, driver_id):
    driver = get_object_or_404(DriverTrip, id=driver_id)

    if request.method == "POST":
        passenger_name = request.POST.get("passenger_name")
        seats_needed = int(request.POST.get("seats_needed", 1))
        departure = request.POST.get("departure")
        destination = request.POST.get("destination")
        date = request.POST.get("date")

        PassengerRequest.objects.create(
            passenger_name=passenger_name,
            seats_needed=seats_needed,
            departure=departure,
            destination=destination,
            date=date,
            is_matched=False,
        )

        # 更新司機 trip 的座位
        driver.seats_filled += seats_needed
        driver.save()

        return redirect("find_car")  # 回到清單頁面
    
    # 如果 GET 就回傳 join form
    return render(request, "Find/join_driver.html", {"driver": driver})

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

def broadcast_update(message):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        "find_updates",
        {"type": "send_update", "message": message}
    )
