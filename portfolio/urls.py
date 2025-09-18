from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='home'),          # 首頁
    path('about/', views.about, name='about'),   # 關於我
    path('portfolio/', views.portfolio, name='portfolio'),  # 作品集
    path("contact/", views.contact_view, name="contact"),        # 聯絡我
    path("minecraft/", views.minecraft_view, name="minecraft"),
    path("minecraft/search/", views.minecraft_search, name="minecraft_search"),
    path("minecraft/rank/", views.minecraft_rank, name="minecraft_rank"),
]
