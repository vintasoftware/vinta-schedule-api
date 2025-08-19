from django.db.models import TextChoices
from django.utils.translation import gettext as _


class PaymentProviders(TextChoices):
    MERCADOPAGO = ("mercadopago", "MercadoPago")


class PaymentStatuses(TextChoices):
    PENDING_SEND = ("pending_send", _("Pending send"))
    PENDING = ("pending", _("Pending"))
    APPROVED = ("approved", _("Approved"))
    REJECTED = ("rejected", _("Rejected"))
    CANCELLED = ("cancelled", _("Cancelled"))
    PARTIALLY_REFUNDED = ("partially_refunded", _("Partially refunded"))
    REFUNDED = ("refunded", _("Refunded"))
    CHARGED_BACK = ("charged_back", _("Charged back"))
    IN_PROCESS = ("in_process", _("In process"))
    IN_MEDIATION = ("in_mediation", _("In mediation"))
    REJECTED_BY_BANK = ("rejected_by_bank", _("Rejected by bank"))
    EXPIRED = ("expired", _("Expired"))
    UNKNOWN = ("unknown", _("Unknown"))
    ERROR = ("error", _("Error"))


class RefundStatuses(TextChoices):
    PENDING_SEND = ("pending_send", _("Pending Send"))
    PENDING = ("pending", _("Pending"))
    APPROVED = ("approved", _("Approved"))
    REJECTED = ("rejected", _("Rejected"))
    FAILED = ("failed", _("Failed"))
    UNKNOWN = ("unknown", _("Unknown"))


class SubscriptionStatuses(TextChoices):
    ACTIVE = ("active", _("Active"))
    PAUSED = ("paused", _("Paused"))
    CANCELLED = ("cancelled", _("Cancelled"))
    PENDING = ("pending", _("Pending"))
    PENDING_SEND = ("pending_send", _("Pending send"))
    ERROR = ("error", _("Error"))
    UNKNOWN = ("unknown", _("Unknown"))
