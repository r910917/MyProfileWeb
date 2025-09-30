from django.db import models

# 🚗 出車人 (司機)
class DriverTrip(models.Model):
    driver_name = models.CharField(max_length=50)
    contact = models.CharField(max_length=50)  # 手機或 LINE ID
    email = models.EmailField(blank=True, null=True)  # ✅ Email 可空
    password = models.CharField(max_length=50, default="0000")  # 🔑 管理密碼
    gender = models.CharField(
        max_length=10,
        choices=[
            ("M", "男生"),
            ("F", "女生"),
            ("X", "不方便透漏"),
        ],
        default="X"
    )
    seats_total = models.IntegerField()
    seats_filled = models.IntegerField(default=0)
    departure = models.CharField(max_length=100)
    destination = models.CharField(max_length=100)
    date = models.DateField()
    return_date = models.DateField(blank=True, null=True)  # ✅ 回程日期，可空
    flexible_pickup = models.CharField(   # ✅ 改到 DriverTrip
        max_length=20,
        choices=[
            ("YES", "順路可載"),
            ("NO", "不順路也OK"),
            ("MAYBE", "視情況")
        ],
        default="YES"
    )
    note = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    pass

    @property
    def seats_left(self):
        return self.seats_total - self.seats_filled

    def __str__(self):
        return f"{self.driver_name} - {self.departure} → {self.destination}"


# 🧍 乘客
class PassengerRequest(models.Model):
    passenger_name = models.CharField(max_length=50)
    contact = models.CharField(max_length=50)
    email = models.EmailField(blank=True, null=True)  # ✅ Email 可空
    password = models.CharField(max_length=50, default="0000")  # 🔑 管理密碼
    gender = models.CharField(
        max_length=10,
        choices=[
            ("M", "男生"),
            ("F", "女生"),
            ("X", "不方便透漏"),
        ],
        default="X"
    )
    seats_needed = models.IntegerField()
    willing_to_pay = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    departure = models.CharField(max_length=100)
    destination = models.CharField(max_length=100)
    date = models.DateField()
    return_date = models.DateField(blank=True, null=True)  # ✅ 回程日期，可空
    note = models.TextField(blank=True)
    is_matched = models.BooleanField(default=False)
    driver = models.ForeignKey(
        DriverTrip,
        related_name="passengers",
        null=True, blank=True,
        on_delete=models.SET_NULL
    )
    # 可選：如果你之後要真的存「是否一起回程」
    together_return = models.BooleanField(null=True, blank=True)

    def __str__(self):
        return f"{self.passenger_name} - {self.departure} → {self.destination}"
