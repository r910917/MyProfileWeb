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
# 找車（司機出車）
# -------------------
def find_car(request):
    if request.method == "POST":
        password = request.POST.get("password") or "0000"
        DriverTrip.objects.using("find_db").create(
            driver_name=request.POST["name"],
            contact=request.POST["contact"],
            password=password,
            seats_total=int(request.POST["seats_total"]),
            departure=request.POST["departure"],
            destination=request.POST["destination"],
            date=request.POST["date"],
            note=request.POST.get("note", ""),
            seats_filled=0
        )
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
def join_passenger(request, passenger_id):
    passenger = PassengerRequest.objects.using("find_db").get(id=passenger_id)

    if request.method == "POST":
        driver_id = request.POST.get("driver_id")
        driver = DriverTrip.objects.using("find_db").get(id=driver_id)

        if driver.seats_filled + passenger.seats_needed <= driver.seats_total:
            driver.seats_filled += passenger.seats_needed
            if driver.seats_filled >= driver.seats_total:
                driver.is_active = False
            driver.save(using="find_db")

            passenger.is_matched = True
            passenger.save(using="find_db")

        return redirect("find_index")

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

def broadcast_update(message):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        "find_updates",
        {"type": "send_update", "message": message}
    )
