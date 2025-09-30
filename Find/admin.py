from django.contrib import admin
from .models import DriverTrip, PassengerRequest

@admin.register(DriverTrip)
class DriverTripAdmin(admin.ModelAdmin):
    list_display = ("driver_name", "departure", "destination", "date", "seats_filled", "seats_total", "is_active")
    search_fields = ("driver_name", "departure", "destination")

@admin.register(PassengerRequest)
class PassengerRequestAdmin(admin.ModelAdmin):
    list_display = ("passenger_name", "departure", "destination", "date", "seats_needed", "is_matched")
    search_fields = ("passenger_name", "departure", "destination")
