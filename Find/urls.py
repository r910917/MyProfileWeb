from django.urls import path
from django.urls import re_path
from . import views
from .consumers import DriverManageConsumer

urlpatterns = [
    path("", views.index, name="find_index"),
    path("people/", views.find_people, name="find_people"),
    path("car/", views.find_car, name="find_car"),

    # 乘客
    path("passenger/<int:passenger_id>/edit/", views.edit_passenger, name="edit_passenger"),
    path("passenger/<int:passenger_id>/manage/", views.passenger_manage, name="passenger_manage"),
    path("driver/<int:driver_id>/join/", views.join_driver, name="join_driver"),
    # Find/urls.py
    re_path(r"^pax/(?P<pid>\d+)/auth/?$",   views.pax_auth,   name="pax_auth"),
    re_path(r"^pax/(?P<pid>\d+)/get/?$",    views.pax_get,    name="pax_get"),
    re_path(r"^pax/(?P<pid>\d+)/update/?$", views.pax_update, name="pax_update"),
    re_path(r"^pax/(?P<pid>\d+)/delete/?$", views.pax_delete, name="pax_delete"),

    # 司機管理頁
    path("driver/<int:driver_id>/manage/", views.driver_manage, name="driver_manage"),
    path("driver/<int:driver_id>/delete/", views.delete_driver, name="delete_driver"),
    path("pax/<int:pax_id>/memo/",   views.pax_memo,   name="pax_memo"),
    path('pax/<int:pax_id>/accept/', views.pax_accept, name='pax_accept'),
    path('pax/<int:pax_id>/reject/', views.pax_reject, name='pax_reject'),
    # 密碼驗證（AJAX）
    path("driver/<int:driver_id>/manage/auth/", views.driver_manage_auth, name="driver_manage_auth"),
]

urlpatterns += [
    path("driver/<int:driver_id>/privacy/", views.driver_toggle_privacy, name="driver_toggle_privacy"),
    path("pax/<int:pax_id>/privacy/", views.pax_toggle_privacy, name="pax_toggle_privacy"),
    path("driver/<int:driver_id>/delete/", views.delete_driver, name="delete_driver"),
]