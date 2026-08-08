"""Payment provider slug constants, free of Django imports.

This module exists separately from `payments/constants.py` because
`vinta_schedule_api/settings/base.py` must validate `DEFAULT_PAYMENT_PROVIDER` at
settings-import time, and `payments.constants` cannot be imported at that point:
`PaymentStatuses` and friends are `TextChoices` whose members call `gettext()` at
class-body evaluation time, which touches `django.conf.settings` while settings are
still mid-import. Importing `payments.constants` from settings therefore triggers an
import cycle.

Keep this module pure stdlib (no Django imports, no imports of anything that imports
Django) so it is always safe to import from `settings/base.py`. Do not merge this back
into `payments/constants.py` -- that reintroduces the cycle above.
"""

STRIPE = "stripe"
MERCADOPAGO = "mercadopago"

PAYMENT_PROVIDER_SLUGS: tuple[str, ...] = (
    STRIPE,
    MERCADOPAGO,
)
