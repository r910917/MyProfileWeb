# utils_email_async.py
from concurrent.futures import ThreadPoolExecutor
from django.shortcuts import get_object_or_404
from django.core.mail import get_connection, send_mail
from django.conf import settings
from .models import DriverTrip, PassengerRequest
from .utils_email import build_join_emails  # 你原本封裝判斷1~6種情境，回傳要寄的 Email 列表

# 全域執行緒池（1~3條就好）
EMAIL_EXECUTOR = ThreadPoolExecutor(max_workers=2)

def _do_send_join_emails(driver_id: int, pax_id: int):
    """在背景執行緒裡跑：用 ID 回查，避免關聯物件被序列化。"""
    d = get_object_or_404(DriverTrip.objects.using("find_db"), id=driver_id)
    p = get_object_or_404(PassengerRequest.objects.using("find_db"), id=pax_id)

    # 你原本的判斷(1~6)請集中在這支：回傳要寄的 messages 或 (subject, body, to...) 列表
    messages = build_join_emails(d, p)

    # 建議自己開關連線，避免卡住或重用錯誤連線
    with get_connection() as conn:
        for msg in messages:
            # 假設 messages 是 [(subject, body, to_list), ...]
            subject, body, to_list = msg
            try:
                send_mail(
                    subject=subject,
                    message=body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=to_list,
                    fail_silently=True,   # 不要炸出例外卡執行緒
                    connection=conn,
                )
            except Exception:
                # 這裡可加 logging，但不要把錯丟回主流程
                pass

def enqueue_join_emails_after_commit(driver_id: int, pax_id: int):
    """供 view 呼叫：在交易提交後把寄信任務丟到背景執行緒池。"""
    EMAIL_EXECUTOR.submit(_do_send_join_emails, driver_id, pax_id)
