from django.db import models
from django.core.exceptions import ValidationError
# 🚗 出車人 (司機)
class DriverTrip(models.Model):
    driver_name = models.CharField(max_length=50)
    contact = models.CharField(max_length=50)  # 手機或 LINE ID
    email = models.EmailField(blank=True, null=True)  # ✅ Email 可空
    contact = models.CharField(max_length=255, blank=True, default="")
    password = models.CharField(max_length=50, default="0000")  # 🔑 管理密碼
    # ★ 新增：是否隱藏聯絡方式（需要 email）
    hide_contact = models.BooleanField(default=False)  # 司機預設隱藏
    auto_email_contact = models.BooleanField(
        default=False,
        help_text="（僅在隱藏個資為 True 時生效）自動將你的聯絡方式以 Email 通知已報名的乘客"
    )
    def clean(self):
        super().clean()
        # 沒 email 不可隱藏；直接回退/阻擋
        if self.hide_contact and not self.email:
            raise ValidationError("要隱藏聯絡方式，必須先填寫 Email，否則無法建立乘客聯繫。")
        if not self.hide_contact:
            self.auto_email_contact = False
    # （可選）提供一個顯示字串，模板更乾淨
    @property
    def display_contact(self):
        if self.hide_contact:
            return "已隱藏（請透過系統通知）"
        return self.contact or "未提供"
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
    fare_note = models.CharField("酌收費用", max_length=100, blank=True, null=True)
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
    note = models.TextField(blank=True,null=True)
    is_active = models.BooleanField(default=True)
    pass

    def clean(self):
        super().clean()
        if self.return_date and self.date and self.return_date < self.date:
            raise ValidationError({'return_date': '回程日期不可早於出發日期'})

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
    contact = models.CharField(max_length=255, blank=True, default="")
    # ★ 新增：司機備忘（僅司機看得到）
    driver_memo = models.TextField(blank=True, null=True, default="")
    # ★ 新增：是否隱藏聯絡方式（需要 email）
    hide_contact = models.BooleanField(default=False)
    auto_email_contact = models.BooleanField(
        default=False,
        help_text="（僅在隱藏個資為 True 時生效）自動將你的聯絡方式以 Email 通知司機"
    )
    def clean(self):
        super().clean()
        # 沒 email 不可隱藏；直接回退/阻擋
        if self.hide_contact and not self.email:
            raise ValidationError("要隱藏聯絡方式，必須先填寫 Email，否則無法建立乘客聯繫。")
        if not self.hide_contact:
            self.auto_email_contact = False

    # （可選）提供一個顯示字串，模板更乾淨
    @property
    def display_contact(self):
        if self.hide_contact:
            return "已隱藏（請透過系統通知）"
        return self.contact or "未提供"
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
    def clean(self):
        super().clean()
        if self.return_date and self.date and self.return_date < self.date:
            raise ValidationError({'return_date': '回程日期不可早於出發日期'})
    # 可選：如果你之後要真的存「是否一起回程」
    together_return = models.BooleanField(null=True, blank=True)

    def __str__(self):
        return f"{self.passenger_name} - {self.departure} → {self.destination}"
