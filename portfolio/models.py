from django.db import models

# Create your models here.

class Contact(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField()
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)  # 自動記錄建立時間

    def __str__(self):
        return f"{self.name} - {self.email}"