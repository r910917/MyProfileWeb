# tasks.py
from celery import shared_task
from django.shortcuts import get_object_or_404
from django.core.mail import get_connection, send_mail
from django.conf import settings
from .models import DriverTrip, PassengerRequest
from .utils_email import build_join_emails

@shared_task
def send_join_emails_task(driver_id: int, pax_id: int):
    d = get_object_or_404(DriverTrip.objects.using("find_db"), id=driver_id)
    p = get_object_or_404(PassengerRequest.objects.using("find_db"), id=pax_id)
    messages = build_join_emails(d, p)
    with get_connection() as conn:
        for subject, body, to_list in messages:
            try:
                send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, to_list, fail_silently=True, connection=conn)
            except Exception:
                pass
