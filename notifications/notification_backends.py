"""
Local notification backend fixes and overrides.

DjangoDbNotificationBackend from vintasend_django has a bug in
_get_all_in_app_unread_notifications_queryset where it passes the
NotificationTypes.IN_APP enum instance directly to the ORM filter instead of its
string value. Django's CharField.get_prep_value calls str() on the enum, producing
"NotificationTypes.IN_APP" instead of the stored "IN_APP", so the query returns no rows.

This module provides a corrected subclass used in the DI container and tests.
"""

import uuid

from django.db.models import QuerySet

from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend_django.models import Notification as NotificationModel
from vintasend_django.services.notification_backends.django_db_notification_backend import (
    DjangoDbNotificationBackend,
)


class FixedDjangoDbNotificationBackend(DjangoDbNotificationBackend):
    """
    DjangoDbNotificationBackend with the IN_APP unread query fixed.

    Overrides _get_all_in_app_unread_notifications_queryset to use
    NotificationTypes.IN_APP.value (the string "IN_APP") instead of the raw enum,
    which would otherwise serialise to "NotificationTypes.IN_APP" via str().
    """

    def _get_all_in_app_unread_notifications_queryset(
        self, user_id: int | str | uuid.UUID
    ) -> QuerySet[NotificationModel]:
        return NotificationModel.objects.filter(
            user_id=str(user_id),
            status=NotificationStatus.SENT.value,
            notification_type=NotificationTypes.IN_APP.value,
        ).order_by("created")
