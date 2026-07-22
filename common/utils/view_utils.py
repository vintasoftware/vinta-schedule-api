from __future__ import annotations

import logging
from typing import Any

from django.shortcuts import get_object_or_404

import django_virtual_models as v
from rest_framework import generics, mixins, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet, ViewSetMixin


logger = logging.getLogger(__name__)

#: Header name used to select the active organization for a request.
ACTIVE_ORG_HEADER = "X-Organization-Id"


class TenantScopedViewMixin:
    """Resolve the active organization for every DRF request.

    This mixin must be included in every base viewset so that all internal REST
    endpoints automatically pick up the ``X-Organization-Id`` header.  The resolver
    runs **after** ``super().initial()`` so that DRF authentication has already
    populated ``request.user`` — the JWT user is not available at Django-middleware
    time.

    After this mixin runs, three attributes are available on every DRF request:

    - ``request.organization_membership`` — the resolved ``OrganizationMembership``
      or ``None`` (gated / unauthenticated user).
    - ``request.organization`` — the resolved ``Organization`` or ``None``.
    - ``request.user._active_membership`` — same value as
      ``request.organization_membership``.  ``get_active_organization_membership``
      reads this stash so all ~60 existing call sites are header-aware without
      change.

    Resolution table (multi-org with no header → 400; non-member → 403):

    +-----------------------+---------------------------------+------------------------------------------+
    | Memberships (active)  | Header                          | Result                                   |
    +-----------------------+---------------------------------+------------------------------------------+
    | 0                     | any                             | gated (membership = None)                |
    | 1                     | absent                          | resolve to that membership               |
    | 1                     | present, matches                | resolve to it                            |
    | 2+                    | present, matches member         | resolve to named org                     |
    | 2+                    | absent                          | **400** (X-Organization-Id required)     |
    | any                   | present, non-member org         | **403** (PermissionDenied)               |
    | any                   | present, non-integer            | treated as absent header                 |
    |                       |                                 | (1 → resolve · 2+ → 400 · 0 → gated)     |
    +-----------------------+---------------------------------+------------------------------------------+

    The ``2+ / absent`` row raises ``rest_framework.exceptions.ValidationError``
    (rendered as **400** with body ``{"detail": "X-Organization-Id header
    required."}``) so a multi-org caller can never resolve to an ambiguous,
    implicit organization.

    **Opt-out (class-level):** a concrete view that must serve multi-org callers
    *without* the header (e.g. the org-discovery ``GET /organizations/mine/``
    endpoint and the onboarding/gated flows) sets the class attribute
    ``active_org_resolution_optional = True``.  When set, the ``2+ / absent``
    case does **not** raise a 400, and the ``non-member org`` case does **not**
    raise a 403 — the active org simply resolves to ``None`` (left gated) so the
    view can list the caller's memberships.  Defaults to ``False``.

    **Opt-out (per-action):** when only a *specific* action on an otherwise
    strict viewset must bypass the header requirement, list that action name in
    the ``active_org_optional_actions`` tuple instead.  The resolver treats a
    request as opted-out when ``active_org_resolution_optional is True`` **or**
    ``self.action in self.active_org_optional_actions``.  ``self.action`` is set
    by ``ViewSetMixin.initialize_request`` before ``initial()`` runs, so the
    check is always current.  Example: ``active_org_optional_actions = ("mine",)``
    on ``OrganizationViewSet`` waives the header for the ``mine`` action only,
    leaving ``current``, ``update``, and ``sync-rooms`` with the full 400/403
    enforcement.

    Unauthenticated requests pass through untouched (the mixin sets ``None`` on
    all three attributes so downstream code doesn't KeyError); DRF's own
    authentication / permission stack returns 401 before any business logic runs.
    """

    #: When ``True``, a multi-org caller that omits ``X-Organization-Id`` is *not*
    #: rejected with a 400, and a header naming a non-member org is *not* rejected
    #: with a 403 — the active org resolves to ``None`` instead.  Concrete views
    #: that must function without the header (org discovery, onboarding) opt in.
    #: See the class docstring's resolution table for the affected rows.
    active_org_resolution_optional: bool = False

    #: Per-action opt-out: list action names for which the header requirement is
    #: waived.  When ``self.action`` (set by ``ViewSetMixin.initialize_request``
    #: before ``initial()`` runs) is in this tuple, the resolver behaves exactly
    #: as if ``active_org_resolution_optional = True`` for that single action —
    #: the multi-org 400 and non-member 403 are suppressed, and the active org
    #: resolves to ``None`` instead.  Use this on a viewset where *most* actions
    #: require the header but a specific action (e.g. ``mine``) must not.
    #:
    #: Example::
    #:
    #:     class OrganizationViewSet(NoListVintaScheduleModelViewSet):
    #:         active_org_optional_actions = ("mine",)
    active_org_optional_actions: tuple[str, ...] = ()

    def _is_active_org_resolution_optional(self) -> bool:
        """Return ``True`` when strict org resolution should be skipped for this request.

        Resolution is optional when either the class-level
        ``active_org_resolution_optional`` flag is set, *or* the current action
        name is listed in ``active_org_optional_actions``.  The latter allows a
        single action on an otherwise strict viewset to opt out without affecting
        the other actions.
        """
        if getattr(self, "active_org_resolution_optional", False):
            return True
        action_name = getattr(self, "action", None)
        optional_actions: tuple[str, ...] = getattr(self, "active_org_optional_actions", ())
        return action_name in optional_actions if action_name is not None else False

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        """Run DRF initialisation, then resolve and stash the active org."""
        super().initial(request, *args, **kwargs)  # type: ignore[misc]
        self._resolve_active_organization(request)

    def _resolve_active_organization(self, request: Request) -> None:  # noqa: C901
        """Resolve ``X-Organization-Id`` → membership and stash on ``request`` + user.

        This method is extracted from ``initial()`` so tests can call it in isolation
        and so subclasses can override or extend it without touching ``initial()``.
        """
        # Lazily import to avoid a circular import (organizations → common → organizations).
        from organizations.models import OrganizationMembership  # noqa: PLC0415

        # Default: nothing resolved yet.
        resolved_membership: OrganizationMembership | None = None

        user = getattr(request, "user", None)
        is_authenticated = user is not None and getattr(user, "is_authenticated", False)

        if is_authenticated:
            org_id_header: str | None = request.headers.get(ACTIVE_ORG_HEADER)

            if org_id_header:
                # Validate that the header value is a valid integer before using it
                # in a DB lookup. A non-coercible value (e.g. "abc") is treated as
                # an absent header rather than raising a ValueError / 500 from the
                # ORM. We intentionally apply the *same* rules as a missing header
                # (single → resolve, multi-org → 400, gated → gated) so a garbage
                # header can never silently pick an org for a multi-org caller.
                try:
                    int(org_id_header)
                except (TypeError, ValueError):
                    logger.debug(
                        "X-Organization-Id header '%s' is not a valid integer for "
                        "user %s; treating it as an absent header.",
                        org_id_header,
                        user.pk,  # type: ignore[union-attr]
                    )
                    # Fall through to the absent-header branch below.
                    org_id_header = None

            if org_id_header:
                # Header present and is a valid integer: try to find a matching active membership.
                matching = (
                    user.organization_memberships.filter(  # type: ignore[union-attr]
                        is_active=True,
                        organization_id=org_id_header,
                    )
                    .select_related("organization")
                    .first()
                )
                if matching is not None:
                    # Happy path: header names an org the user actively belongs to.
                    resolved_membership = matching
                else:
                    # Header names an org the caller is not an active member of
                    # (either the org doesn't exist, the user has no membership in
                    # it, or the membership exists but is inactive).  Raise 403
                    # unless the concrete view opted out of strict resolution
                    # (active_org_resolution_optional = True).
                    if not self._is_active_org_resolution_optional():
                        logger.debug(
                            "X-Organization-Id header '%s' did not match any active membership for "
                            "user %s; raising PermissionDenied (403).",
                            org_id_header,
                            user.pk,  # type: ignore[union-attr]
                        )
                        raise PermissionDenied(
                            "X-Organization-Id header names an organization you are not an "
                            "active member of."
                        )
                    logger.debug(
                        "X-Organization-Id header '%s' did not match any active membership for "
                        "user %s; view opted out of the 403 — resolving to gated (None).",
                        org_id_header,
                        user.pk,  # type: ignore[union-attr]
                    )
            else:
                # Header absent: resolve to the single active membership when there
                # is exactly one. A multi-org caller who omits the header is
                # rejected with 400 (unless the view opts out via
                # ``active_org_resolution_optional``); zero memberships → gated.
                active_memberships = list(
                    user.organization_memberships.filter(  # type: ignore[union-attr]
                        is_active=True,
                    )
                    .select_related("organization")
                    .order_by("created")[:2]  # only need the first two to detect multi-org
                )
                if len(active_memberships) == 1:
                    # Single-membership happy path: identical to today's behaviour.
                    resolved_membership = active_memberships[0]
                elif len(active_memberships) > 1:
                    # Multi-org caller with no header: the active org is ambiguous.
                    # Reject with 400 so we never silently pick one — unless the
                    # concrete view opted out (org discovery / onboarding), in which
                    # case resolution falls through to gated (None).
                    if not self._is_active_org_resolution_optional():
                        raise ValidationError(
                            {"detail": "X-Organization-Id header required."},
                        )
                    logger.debug(
                        "User %s has multiple active memberships and no X-Organization-Id "
                        "header; view opted out of the 400 — resolving to gated (None).",
                        user.pk,  # type: ignore[union-attr]
                    )
                # else: zero memberships → gated; resolved_membership stays None.

        # Stash resolved values on the request and user so all downstream code
        # (permissions, serializers, get_active_organization_membership) picks them up.
        request.organization_membership = resolved_membership  # type: ignore[attr-defined]
        request.organization = (  # type: ignore[attr-defined]
            resolved_membership.organization if resolved_membership is not None else None
        )
        if is_authenticated and user is not None:
            # Set even when None so get_active_organization_membership can
            # distinguish "DRF request path resolved to gated" from
            # "not on a DRF request at all" (_UNSET sentinel).
            user._active_membership = resolved_membership  # type: ignore[union-attr]


class RefetchReturnInstanceAfterWriteMixin:
    def get_serializer_class(self):
        """
        Return the class to use for the serializer.
        Defaults to using `self.serializer_class`.
        You may want to override this if you need to provide different
        serializations depending on the incoming request.
        (Eg. admins get full serialization, others get basic serialization)
        """
        assert (  # noqa: S101
            self.serializer_class is not None
            or getattr(self, "list_serializer_class", None) is not None
            or getattr(self, "retrieve_serializer_class", None) is not None
            or getattr(self, "read_serializer_class", None) is not None
        ), (
            f"'{self.__class__.__name__}' should either include one of `serializer_class` and "
            f"`read_serializer_class` attribute, or override one of the `get_serializer_class()`, "
            f"`get_read_serializer_class()` method."
        )

        if self.action == "list":
            return getattr(
                self,
                "list_serializer_class",
                getattr(self, "read_serializer_class", self.serializer_class),
            )

        if self.action == "retrieve":
            return getattr(
                self,
                "retrieve_serializer_class",
                getattr(self, "read_serializer_class", self.serializer_class),
            )

        return getattr(
            self,
            "retrieve_serializer_class",
            getattr(self, "read_serializer_class", self.serializer_class),
        )

    def get_list_serializer(self, *args, **kwargs):
        """
        Return the serializer instance that should be used for serializing output in list actions.
        """
        serializer_class = self.get_list_serializer_class()
        kwargs["context"] = self.get_serializer_context()
        return serializer_class(*args, **kwargs)

    def get_list_serializer_class(self):
        """
        Return the class to use for the serializer in list actions.
        Defaults to using `self.list_serializer_class`.
        You may want to override this if you need to provide different
        serializations depending on the incoming request.
        (Eg. admins get full serialization, others get basic serialization)
        """
        if getattr(self, "list_serializer_class", None) is None:
            return self.get_read_serializer_class()

        return self.list_serializer_class

    def get_retrieve_serializer(self, *args, **kwargs):
        """
        Return the serializer instance that should be used for serializing output in retrieve actions.
        """
        serializer_class = self.get_retrieve_serializer_class()
        kwargs["context"] = self.get_serializer_context()
        return serializer_class(*args, **kwargs)

    def get_retrieve_serializer_class(self):
        """
        Return the class to use for the serializer in retrieve actions.
        Defaults to using `self.retrieve_serializer_class`.
        You may want to override this if you need to provide different
        serializations depending on the incoming request.
        (Eg. admins get full serialization, others get basic serialization)
        """
        if getattr(self, "retrieve_serializer_class", None) is None:
            return self.get_read_serializer_class()

        return self.retrieve_serializer_class

    def get_create_serializer(self, *args, **kwargs):
        """
        Return the serializer instance that should be used for serializing output in create actions.
        """
        serializer_class = self.get_create_serializer_class()
        kwargs["context"] = self.get_serializer_context()
        return serializer_class(*args, **kwargs)

    def get_create_serializer_class(self):
        """
        Return the class to use for the serializer in create actions.
        Defaults to using `self.create_serializer_class`.
        You may want to override this if you need to provide different
        serializations depending on the incoming request.
        (Eg. admins can send extra fields, others cannot)
        """
        if getattr(self, "create_serializer_class", None) is None:
            return self.get_write_serializer_class()

        return self.create_serializer_class

    def get_update_serializer(self, *args, **kwargs):
        """
        Return the serializer instance that should be used for serializing output in update actions.
        """
        serializer_class = self.get_update_serializer_class()
        kwargs["context"] = self.get_serializer_context()
        return serializer_class(*args, **kwargs)

    def get_update_serializer_class(self):
        """
        Return the class to use for the serializer in update actions.
        Defaults to using `self.update_serializer_class`.
        You may want to override this if you need to provide different
        serializations depending on the incoming request.
        (Eg. admins can send extra fields, others cannot)
        """
        if getattr(self, "update_serializer_class", None) is None:
            return self.get_write_serializer_class()

        return self.update_serializer_class

    def get_read_serializer(self, *args, **kwargs):
        """
        Return the serializer instance that should be used for serializing output.
        """
        serializer_class = self.get_read_serializer_class()
        kwargs["context"] = self.get_serializer_context()
        return serializer_class(*args, **kwargs)

    def get_read_serializer_class(self):
        """
        Return the class to use for the serializer.
        Defaults to using `self.read_serializer_class`.
        You may want to override this if you need to provide different
        serializations depending on the incoming request.
        (Eg. admins get full serialization, others get basic serialization)
        """
        if getattr(self, "read_serializer_class", None) is None:
            return self.get_serializer_class()

        return self.read_serializer_class

    def get_write_serializer(self, *args, **kwargs):
        """
        Return the serializer instance that should be used for validating
        and deserializing input.
        """
        serializer_class = self.get_write_serializer_class()
        kwargs["context"] = self.get_serializer_context()
        return serializer_class(*args, **kwargs)

    def get_write_serializer_class(self):
        """
        Return the class to use for the serializer.
        Defaults to using `self.write_serializer_class`.
        You may want to override this if you need to provide different
        serializations depending on the incoming request.
        (Eg. admins can send extra fields, others cannot)
        """
        if getattr(self, "write_serializer_class", None) is None:
            return self.get_serializer_class()

        return self.write_serializer_class


class NoReturnWriteMixin(RefetchReturnInstanceAfterWriteMixin):
    def create(self, request, *args, **kwargs):
        serializer = self.get_write_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_write_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(status=status.HTTP_200_OK)


class CreateModelMixin(RefetchReturnInstanceAfterWriteMixin, mixins.CreateModelMixin):
    def create(self, request, *args, **kwargs):
        serializer = self.get_create_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        instance = serializer.instance

        # A service may have created the user's first membership during perform_create
        # (e.g. OrganizationService.create_organization), making the stash set in
        # TenantScopedViewMixin.initial() stale. Re-resolve so the post-create re-fetch
        # honors the X-Organization-Id header (and any newly-created membership) instead
        # of silently dropping to the header-blind single-membership fallback.
        if hasattr(self, "_resolve_active_organization"):
            self._resolve_active_organization(request)

        # re-fetches the instance so we get annotations, prefetches, and selects
        if hasattr(self, "get_return_queryset"):
            annotated_instance = self.get_return_queryset().get(pk=instance.pk)
        else:
            annotated_instance = self.get_queryset().get(pk=instance.pk)
        return_serializer = self.get_retrieve_serializer(annotated_instance)
        headers = self.get_success_headers(return_serializer.data)
        return Response(return_serializer.data, status=status.HTTP_201_CREATED, headers=headers)


class UpdateModelMixin(RefetchReturnInstanceAfterWriteMixin, mixins.UpdateModelMixin):
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_update_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        return_serializer = self.get_retrieve_serializer(
            self.get_return_object(serializer.instance)
        )

        return Response(return_serializer.data)

    def get_return_object(self, instance):
        if hasattr(self, "get_return_queryset"):
            queryset = self.get_return_queryset()
        else:
            queryset = self.get_queryset()
        queryset = self.filter_queryset(queryset)

        # Perform the lookup filtering.
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field

        assert lookup_url_kwarg in self.kwargs, (  # noqa: S101
            f"Expected view {self.__class__.__name__} to be called with a URL keyword argument "
            f'named "{lookup_url_kwarg}". Fix your URL conf, or set the `.lookup_field` '
            f"attribute on the view correctly."
        )

        filter_kwargs = {self.lookup_field: getattr(instance, self.lookup_field)}
        obj = get_object_or_404(queryset, **filter_kwargs)

        # May raise a permission denied
        self.check_object_permissions(self.request, obj)

        return obj


class FilterOnlyOnListMixin:
    def filter_queryset(self, queryset):
        if self.action != "list":
            return queryset
        return super().filter_queryset(queryset)


class VintaScheduleModelViewSet(
    TenantScopedViewMixin,
    CreateModelMixin,
    UpdateModelMixin,
    FilterOnlyOnListMixin,
    v.GenericVirtualModelViewMixin,
    ModelViewSet,
):
    """
    A viewset that provides default `create()`, `retrieve()`, `update()`,
    `partial_update()`, `destroy()` and `list()` actions for vinta_schedule models.
    It refetches the instance after write operations to ensure the latest data is returned.
    """

    pass


class ReadOnlyVintaScheduleModelViewSet(
    TenantScopedViewMixin,
    ViewSetMixin,
    RefetchReturnInstanceAfterWriteMixin,
    FilterOnlyOnListMixin,
    v.GenericVirtualModelViewMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    generics.GenericAPIView,
):
    """
    A viewset that provides read-only access to vinta_schedule models.
    It does not allow creation, update, or deletion of instances.
    """

    pass


class NoCreateVintaScheduleModelViewSet(
    TenantScopedViewMixin,
    ViewSetMixin,
    FilterOnlyOnListMixin,
    v.GenericVirtualModelViewMixin,
    mixins.RetrieveModelMixin,
    UpdateModelMixin,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    generics.GenericAPIView,
):
    """
    A viewset that does not allow creation of new instances.
    It only allows read and update operations.
    """

    pass


class NoUpdateVintaScheduleModelViewSet(
    TenantScopedViewMixin,
    ViewSetMixin,
    FilterOnlyOnListMixin,
    v.GenericVirtualModelViewMixin,
    mixins.RetrieveModelMixin,
    CreateModelMixin,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    generics.GenericAPIView,
):
    """
    A viewset that does not allow update of instances.
    It only allows read and create/destroy operations.
    """

    pass


class CreateAndReadVintaScheduleModelViewSet(
    TenantScopedViewMixin,
    ViewSetMixin,
    FilterOnlyOnListMixin,
    v.GenericVirtualModelViewMixin,
    mixins.RetrieveModelMixin,
    CreateModelMixin,
    mixins.ListModelMixin,
    generics.GenericAPIView,
):
    """
    A viewset that does not allow update of instances.
    It only allows read and create operations.
    """

    pass


class NoListVintaScheduleModelViewSet(
    TenantScopedViewMixin,
    ViewSetMixin,
    FilterOnlyOnListMixin,
    v.GenericVirtualModelViewMixin,
    mixins.RetrieveModelMixin,
    UpdateModelMixin,
    CreateModelMixin,
    mixins.DestroyModelMixin,
    generics.GenericAPIView,
):
    """
    A viewset that does not allow update of instances.
    It only allows read and create operations.
    """

    pass


class WriteOnlyVintaScheduleModelViewSet(
    TenantScopedViewMixin,
    ViewSetMixin,
    FilterOnlyOnListMixin,
    v.GenericVirtualModelViewMixin,
    UpdateModelMixin,
    CreateModelMixin,
    mixins.DestroyModelMixin,
    generics.GenericAPIView,
):
    """
    A viewset that does not allow update of instances.
    It only allows read and create operations.
    """

    pass


class NoDetailsVintaScheduleModelViewSet(
    TenantScopedViewMixin,
    ViewSetMixin,
    FilterOnlyOnListMixin,
    v.GenericVirtualModelViewMixin,
    UpdateModelMixin,
    CreateModelMixin,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    generics.GenericAPIView,
):
    """
    A viewset that does not allow details of instances.
    It only allows list and create/update/destroy operations.
    """

    pass
