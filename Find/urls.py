from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="find_index"),
    path("people/", views.find_people, name="find_people"),
    path("car/", views.find_car, name="find_car"),

    # 乘客
    path("passenger/<int:passenger_id>/edit/", views.edit_passenger, name="edit_passenger"),
    path("passenger/<int:passenger_id>/manage/", views.passenger_manage, name="passenger_manage"),
    path("driver/<int:driver_id>/join/", views.join_driver, name="join_driver"),

    # 司機
    path("driver/<int:driver_id>/manage/", views.driver_manage, name="driver_manage"),
    path("driver/<int:driver_id>/edit/", views.edit_driver, name="edit_driver"),
]
