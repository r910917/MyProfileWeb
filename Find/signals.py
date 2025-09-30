from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from asgiref.sync import async_to_sync, sync_to_async
from channels.layers import get_channel_layer
from django.template.loader import render_to_string
from .models import PassengerRequest, DriverTrip


@receiver([post_save, post_delete], sender=PassengerRequest)
@receiver([post_save, post_delete], sender=DriverTrip)
def broadcast_update(sender, instance, **kwargs):
    """當乘客或司機資料有變動時，廣播給所有連線的 WebSocket"""

    async def _broadcast():
        # ORM 查詢要用 sync_to_async 包裝
        passengers = await sync_to_async(
            lambda: list(PassengerRequest.objects.using("find_db").filter(is_matched=False))
        )()
        drivers = await sync_to_async(
            lambda: list(DriverTrip.objects.using("find_db").filter(is_active=True))
        )()

        passengers_html = render_to_string("Find/_passenger_list.html", {"passengers": passengers})
        drivers_html = render_to_string("Find/_driver_list.html", {"drivers": drivers})

        channel_layer = get_channel_layer()
        await channel_layer.group_send(
            "find_group",
            {
                "type": "send_update",
                "passengers_html": passengers_html,
                "drivers_html": drivers_html,
            }
        )

    async_to_sync(_broadcast)()
