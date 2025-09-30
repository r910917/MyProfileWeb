from django.apps import AppConfig

class FindConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "Find"

    def ready(self):
        # 確保啟動時會載入 signals
        from . import signals  # ✅ 檔案名稱是 signals.py，不是 Find_signals
