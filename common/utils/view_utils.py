from __future__ import annotations

import logging

from django.shortcuts import get_object_or_404

import django_virtual_models as v
from rest_framework import generics, mixins, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet, ViewSetMixin
from vinta_orgs.drf import OrganizationScopedAPIViewMixin

from common.constants import ACTIVE_ORG_HEADER


logger = logging.getLogger(__name__)


#: Body of the ``2+ memberships / no header`` refusal, rendered as
#: ``400 {"detail": ...}``. Ours, not the package's: clients match on this
#: string, so it survives the delegation to
#: ``vinta_orgs.helpers.memberships.resolve_membership_for_user`` verbatim.
AMBIGUOUS_ORGANIZATION_DETAIL = "X-Organization-Id header required."

#: Body of the ``header names an organization you do not actively belong to``
#: refusal, rendered as ``403 {"detail": ...}``. Same reasoning as above.
NON_MEMBER_ORGANIZATION_DETAIL = (
    "X-Organization-Id header names an organization you are not an active member of."
)

#: What :meth:`TenantScopedViewMixin.get_organization_slug` answers when the
#: header carries an integer that names no organization at all.
#:
#: The package resolves by *slug*, and reads ``None`` as "the caller named
#: nothing" -- which for a caller with one membership resolves to it, and for a
#: caller with several is a 400. A header naming a non-existent organization
#: must instead be refused with the same 403 as a header naming a real
#: organization the caller does not belong to: answering those two differently
#: turns the endpoint into an oracle for which ids are taken. So the lookup
#: answers with a slug that provably matches no row, rather than with ``None``.
#:
#: "Provably" for two independent reasons: it is longer than
#: ``Organization.slug``'s ``varchar(255)``, so no row can hold it whatever
#: wrote it; and it contains spaces and capitals, which
#: ``organizations.slug_validation``'s format rule forbids.
UNMATCHABLE_ORGANIZATION_SLUG = ("X-Organization-Id names no organization. " * 7).strip()


class TenantScopedViewMixin(OrganizationScopedAPIViewMixin):
    """Resolve the active organization for every DRF request.

    This mixin must be included in every base viewset so that all internal REST
    endpoints automatically pick up the ``X-Organization-Id`` header.

    **The seam is the package's.**
    :class:`vinta_orgs.drf.OrganizationScopedAPIViewMixin` owns the
    ``perform_authentication`` override that puts resolution in the one place it
    can be correct -- between "``request.user`` is now real" and
    "``check_permissions`` runs" -- and the ``finally`` around ``dispatch`` that
    releases the binding on every exit path, including the ones DRF does not
    funnel through ``finalize_response``. A binding that leaked there would be
    read by the *next* request the worker thread serves. Read that class's
    docstrings before changing anything here; this subclass exists for the two
    things that are *ours*:

    1. **The header.** :meth:`get_organization_slug` reads ``X-Organization-Id``
       (an integer primary key) rather than the package's ``Organization-Slug``,
       and translates it into the slug the package's resolver matches on.
    2. **The refusal bodies.** :meth:`resolve_organization` restates the 400 and
       the 403 in the wording our clients already match on.
    After this mixin runs, two attributes are available on every DRF request:

    - ``request.organization_membership`` -- the resolved
      ``OrganizationMembership`` or ``None`` (gated / unauthenticated caller).
    - ``request.organization`` -- the resolved ``Organization`` or ``None``.

    Resolution table (multi-org with no header -> 400; non-member -> 403). It is
    ``resolve_membership_for_user``'s table, restated in terms of our header:

    +-----------------------+---------------------------------+------------------------------------------+
    | Memberships (active)  | Header                          | Result                                   |
    +-----------------------+---------------------------------+------------------------------------------+
    | 0                     | absent                          | gated (membership = None)                |
    | 1                     | absent                          | resolve to that membership               |
    | 1                     | present, matches                | resolve to it                            |
    | 2+                    | present, matches member         | resolve to named org                     |
    | 2+                    | absent                          | **400** (X-Organization-Id required)     |
    | any                   | present, non-member org         | **403** (PermissionDenied)               |
    | any                   | present, no such org            | **403** -- the same refusal, on purpose  |
    | any                   | present, non-integer            | treated as absent header                 |
    |                       |                                 | (1 -> resolve; 2+ -> 400; 0 -> gated)    |
    +-----------------------+---------------------------------+------------------------------------------+

    The ``2+ / absent`` row raises ``rest_framework.exceptions.ValidationError``
    (rendered as **400** with body ``{"detail": "X-Organization-Id header
    required."}``) so a multi-org caller can never resolve to an ambiguous,
    implicit organization.

    **Opt-out (class-level):** a concrete view that must serve multi-org callers
    *without* the header (e.g. the org-discovery ``GET /organizations/mine/``
    endpoint and the onboarding / gated flows) sets the package's
    ``organization_resolution_optional = True``. When set, the ``2+ / absent``
    case does **not** raise a 400 and the ``non-member org`` case does **not**
    raise a 403 -- the active organization simply resolves to ``None`` (left
    gated) so the view can list the caller's memberships. Defaults to ``False``.

    **Opt-out (per-action):** when only a *specific* action on an otherwise
    strict viewset must bypass the header requirement, list that action name in
    the package's ``organization_optional_actions`` tuple instead. ``self.action``
    is set by ``ViewSetMixin.initialize_request`` before ``initial()`` runs, so
    the check is always current. Example:
    ``organization_optional_actions = ("mine",)`` on ``OrganizationViewSet``
    waives the header for the ``mine`` action only, leaving ``current``,
    ``update`` and ``sync-rooms`` with the full 400 / 403 enforcement.

    Unauthenticated requests pass through untouched -- the resolver sets ``None``
    on ``request.organization`` and ``request.organization_membership`` so
    downstream code does not ``AttributeError``, and DRF's own authentication /
    permission stack answers 401 before any business logic runs. **401 stays
    ahead of 400 / 403** for two independent reasons: a bad credential raises
    out of ``super().perform_authentication`` before the resolver is reached at
    all, and ``resolve_membership_for_user`` returns ``None`` for an anonymous
    user rather than consulting the table, so no row above can fire without a
    caller.
    """

    def get_organization_slug(self, request: Request) -> str | None:
        """Translate our ``X-Organization-Id`` header into the slug the package matches on.

        This is the package's designated override point: the table, the
        refusals and the binding all stay the package's, and only "what did the
        caller name?" is ours.

        Three answers, and the difference between them is the whole contract:

        * **``None``** -- the caller named nothing. An absent, an empty *and* a
          non-integer header all land here, because a garbage header has to be
          answered by the same rules as a missing one (1 -> resolve, 2+ -> 400,
          0 -> gated) rather than silently picking an organization.
        * **A real slug** -- the caller named an organization that exists.
          Whether they may *have* it is the package's decision, taken against
          their memberships.
        * **:data:`UNMATCHABLE_ORGANIZATION_SLUG`** -- the caller named an
          integer that is not an organization, whether because no row holds it
          or because it is too wide for the primary key column and no row could.
          Answering ``None`` here would downgrade a 403 into "no header was
          sent", which for a single-membership caller quietly succeeds against an
          organization they never asked for.

        The whole body is skipped for a caller who is not authenticated. The
        package evaluates this method *eagerly*, as an argument to
        ``resolve_membership_for_user``, so it runs before that function's
        ``is_anonymous`` short-circuit -- and resolution as a whole now precedes
        ``check_throttles``. Without the guard below, an anonymous request
        carrying a header would spend an ``Organization`` query before the 401
        and before any throttle bucket was consulted. Returning ``None`` is
        behaviour-identical: the resolver answers ``None`` for an anonymous user
        whatever it is handed.
        """
        # Deferred: ``organizations`` imports ``common``, so a module-level
        # import here is a cycle.
        from organizations.models import Organization  # noqa: PLC0415

        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return None

        raw_value: str | None = request.headers.get(ACTIVE_ORG_HEADER)
        if not raw_value:
            return None

        try:
            organization_id = int(raw_value)
        except (TypeError, ValueError):
            logger.debug(
                "X-Organization-Id header '%s' is not a valid integer; "
                "treating it as an absent header.",
                raw_value,
            )
            return None

        # No range check on ``organization_id`` before the lookup, deliberately.
        # An integer too wide for the ``bigint`` primary key is adapted by
        # psycopg 3 as ``numeric``, which Postgres compares against ``bigint``
        # without error and matches nothing -- so it takes the ordinary
        # "names no organization" road below and is answered 403 like any other
        # unused id, rather than raising ``NumericValueOutOfRange`` into a 500.
        # ``int()`` above is what bounds the input: CPython refuses to parse a
        # string past ``sys.get_int_max_str_digits()``, and that ``ValueError``
        # is already handled as an absent header.
        # ``TestAHeaderTooWideForThePrimaryKey`` pins all of this.
        #
        # ``Organization`` is the tenant root, not tenant-scoped data: it is not
        # organization-scoped, so ``objects`` here is Django's stock manager and
        # this lookup neither needs nor bypasses an organization filter.
        slug: str | None = (
            Organization.objects.filter(pk=organization_id).values_list("slug", flat=True).first()
        )
        if slug is None:
            logger.debug(
                "X-Organization-Id header '%s' names no organization; refusing it as a "
                "non-member organization would be refused.",
                raw_value,
            )
            return UNMATCHABLE_ORGANIZATION_SLUG

        return slug

    def resolve_organization(self, request: Request) -> None:
        """Run the package's resolution while preserving our refusal bodies.

        The package translates ``AmbiguousOrganizationError`` into a DRF
        ``ValidationError`` and ``OrganizationAccessDeniedError`` into a DRF
        ``PermissionDenied`` -- the right status codes, in its own wording. The
        two re-raises below put our wording back: those strings predate the
        package and are a wire contract, and a client matching on them must not
        have to care that resolution moved upstream.

        """
        try:
            super().resolve_organization(request)
        except ValidationError as exc:
            raise ValidationError({"detail": AMBIGUOUS_ORGANIZATION_DETAIL}) from exc
        except PermissionDenied as exc:
            raise PermissionDenied(NON_MEMBER_ORGANIZATION_DETAIL) from exc


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
        # TenantScopedViewMixin.perform_authentication() stale. Re-resolve so the
        # post-create re-fetch honors the X-Organization-Id header (and any
        # newly-created membership) instead of silently dropping to the
        # header-blind single-membership fallback.
        # Re-bind too: the re-fetch below reads through organization-scoped default
        # managers, and leaving the context on the organization
        # ``perform_authentication()`` resolved would scope it to a different one
        # than the stash the same lines consult.
        # ``bind_organization`` releases the first binding before taking the
        # second, so ``dispatch``'s ``finally`` still restores the pre-request value.
        if hasattr(self, "resolve_organization"):
            self.resolve_organization(request)
            self.bind_organization(request.organization)

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
