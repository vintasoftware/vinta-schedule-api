from django.core.exceptions import ImproperlyConfigured


class WebhookServiceNotInjectedError(ImproperlyConfigured):
    pass
