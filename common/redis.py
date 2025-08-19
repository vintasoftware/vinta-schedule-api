from django.conf import settings

from redis import Redis


redis_connection = Redis.from_url(settings.REDIS_URL)
