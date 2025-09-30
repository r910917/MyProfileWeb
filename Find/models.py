from django.db import models

# 🚗 出車人 (司機)
class DriverTrip(models.Model):
    driver_name = models.CharField(max_length=50)
    contact = models.CharField(max_length=50)  # 手機或 LINE ID
    password = models.CharField(max_length=50,default="0000")  # 🔑 管理密碼
    seats_total = models.IntegerField()
    seats_filled = models.IntegerField(default=0)
    departure = models.CharField(max_length=100)
    destination = models.CharField(max_length=100)
    date = models.DateField()
    note = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    @property
    def seats_left(self):
        return self.seats_total - self.seats_filled

    def __str__(self):
        return f"{self.driver_name} - {self.departure} → {self.destination}"


class PassengerRequest(models.Model):
    passenger_name = models.CharField(max_length=50)
    contact = models.CharField(max_length=50)
    password = models.CharField(max_length=50,default="0000")  # 🔑 管理密碼
    seats_needed = models.IntegerField()
    willing_to_pay = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    departure = models.CharField(max_length=100)
    destination = models.CharField(max_length=100)
    date = models.DateField()
    flexible_pickup = models.CharField(
        max_length=20,
        choices=[("YES", "順路可載"), ("NO", "不順路也OK"), ("MAYBE", "視情況")]
    )
    note = models.TextField(blank=True)
    is_matched = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.passenger_name} - {self.departure} → {self.destination}"
