from django.http import HttpRequest, JsonResponse


def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness probe for the ALB target group.

    Deliberately touches nothing: no database, no cache, no broker. The load
    balancer uses this answer to decide whether to keep a task in rotation, so a
    check that reached Postgres would pull every web task out of service during a
    database blip -- turning a degraded API into an unreachable one.

    Kept out of DRF (and so out of ``schema.yml``) on purpose: it is
    infrastructure, not part of the published API.
    """
    return JsonResponse({"status": "ok"})
