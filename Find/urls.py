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
    # Find/urls.py
    path("passenger/<int:pk>/update/", views.passenger_update, name="pax_update"),
    path("passenger/<int:pk>/delete/", views.passenger_delete, name="pax_delete"),

    # 司機管理頁
    path("driver/<int:driver_id>/manage/", views.driver_manage, name="driver_manage"),
    # 密碼驗證（AJAX）
    path("driver/<int:driver_id>/manage/auth/", views.driver_manage_auth, name="driver_manage_auth"),
]
