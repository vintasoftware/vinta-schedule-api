import logging
from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING, Annotated, Any

from django.db import transaction

from dependency_injector.wiring import Provide, inject
from vinta_billing.exceptions import OverLimitError

from audit_integration.constants import AuditAction
from audit_integration.services import OrganizationAuditService
from common.utils.authentication_utils import (
    generate_long_lived_token,
    hash_long_lived_token,
    verify_long_lived_token,
)
from organizations.models import OrganizationMembership
from payments.seams.resource_keys import PUBLIC_API_SYSTEM_USERS
from payments.seams.scopes import scope_for
from public_api.exceptions import (
    OnBehalfOfMemberInvalidError,
    OnBehalfOfMemberRequiredError,
    OnBehalfOfSwitchOnScopedTokenError,
)
from public_api.models import SystemUser


if TYPE_CHECKING:
    from vinta_billing.services.entitlement_service import EntitlementService

    from organizations.models import Organization
    from users.models import User


logger = logging.getLogger(__name__)


class PublicAPIAuthService:
    @inject
    def __init__(
        self,
        # `Provide[...]` as the *default*, not inside `Annotated`: `@inject` replaces it on
        # every call, so it is never read at runtime, but it gives mypy a default so
        # `PublicAPIAuthService()` -- how the container and the tests build this -- is not a
        # missing-argument error. (`_extract_marker` prefers an `Annotated` marker and then
        # ignores the default entirely, so the two forms cannot both be stated here.)
        audit_service: OrganizationAuditService = Provide["audit_service"],
        entitlement_service: Annotated[
            "EntitlementService | None", Provide["entitlement_service"]
        ] = None,
    ) -> None:
        self.audit_service = audit_service
        self.entitlement_service = entitlement_service

    def check_system_user_token(self, system_user_id: int, token: str) -> tuple[SystemUser, bool]:
        """
        Check if the provided token matches the system user's long-lived token.
        :param system_user_id: ID of the system user.
        :param token: The long-lived token to verify.
        :return: Tuple containing the system user and a boolean indicating if the token is valid.
        :raises SystemUser.DoesNotExist: If the system user does not exist.
        :raises ValueError: If the token is invalid or does not match the user's token.
        """
        try:
            system_user_id_int = int(system_user_id)
        except (TypeError, ValueError) as e:
            raise SystemUser.DoesNotExist(f"Invalid system_user_id: {system_user_id!r}") from e
        system_user = SystemUser.original_manager.get(id=system_user_id_int)

        if not system_user.is_active or system_user.deleted_at is not None:
            return system_user, False

        return system_user, verify_long_lived_token(token, system_user.long_lived_token_hash)

    @transaction.atomic()
    def create_system_user(
        self,
        integration_name: str,
        organization: "Organization | None",
        scoped_to_membership: "OrganizationMembership | None" = None,
        bypass_limits: bool = False,
        requires_on_behalf_of: bool = False,
        acted_by: "User | SystemUser | None" = None,
    ) -> tuple[SystemUser, str]:
        """
        Create a new system user with a long-lived token.

        :param integration_name: Unique name identifying the integration.
        :param organization: The organization this system user belongs to, or ``None``
            for an org-less token with access to all organizations. ``SystemUser.
            organization`` is ``null=True``, ``SystemUserAdmin`` exposes it with no
            custom form, and that admin's ``save_model`` branches explicitly on the
            org-less case -- so ``None`` is a supported input, not a defect.
        :param scoped_to_membership: Optional OrganizationMembership to scope this token to.
            When set, the token may only read/write data belonging to calendars owned by the
            membership's user within that organization.
            When None (default), the token has org-wide access (legacy default).
        :param requires_on_behalf_of: When True, the token's mutation requests must name
            the member they act for in ``X-Public-Api-On-Behalf-Of``. Refused together with
            ``scoped_to_membership``, since a member-scoped token ignores that header.
        :param acted_by: Who created the token: the signed-in user, or the public API token
            that minted it. Recorded as the audit actor; ``None`` records the system.
        :param bypass_limits: When True, skips the ``public_api_system_users`` limit guard
            below. Only management commands and one-off repair scripts should pass this --
            never a request-handling path.
        :raises OverLimitError: When the organization is at its effective
            ``public_api_system_users`` ceiling. Nothing is created. Checked and locked
            (``SELECT ... FOR UPDATE`` on the billing root's subscription) inside this
            method's own transaction (every call site -- the REST serializer, both GraphQL
            mutations, and the admin -- routes through this single creation function), so
            two concurrent creates for the last unit of capacity serialize on that row.

            The counter this guards (``EntitlementService._count_public_api_system_users``)
            counts ``SystemUser.objects.live()`` -- ``is_active=True`` and
            ``deleted_at__isnull=True``. A freshly created row defaults to both, so it is
            unconditionally "live" and always feeds the same counter this check reads:
            there is no separate predicate to keep in sync.

            **Not checked at all when ``organization is None``.** There is no
            organization to resolve a billing root from -- ``resolve_billing_root(None)``
            has no answer -- and an org-less token is unmetered by design, which
            ``_count_public_api_system_users``'s own docstring already states (it counts
            per-organization). Guarding it would 500 a previously-working, explicitly
            supported admin path. Logged at WARNING so an unmetered credential is at
            least visible in the record.
        :return: Tuple of (system_user, plaintext_token). The plaintext token is exposed
            once and never persisted; only the hash is stored.
        """
        if requires_on_behalf_of and scoped_to_membership is not None:
            raise OnBehalfOfSwitchOnScopedTokenError()

        if organization is None:
            logger.warning(
                "Creating org-less system user %r: no organization to resolve a billing "
                "root from, so the public_api_system_users limit is not enforced for it. "
                "This credential has access to all organizations and is unmetered.",
                integration_name,
            )
        elif not bypass_limits and self.entitlement_service is not None:
            result = self.entitlement_service.check_limit(
                scope_for(organization), PUBLIC_API_SYSTEM_USERS, lock=True
            )
            if not result.allowed:
                raise OverLimitError.from_check_result(result)

        token = generate_long_lived_token()
        create_kwargs: dict[str, Any] = {
            "organization": organization,
            "integration_name": integration_name,
            "long_lived_token_hash": hash_long_lived_token(token),
            "is_active": True,
            "requires_on_behalf_of": requires_on_behalf_of,
        }
        if scoped_to_membership is not None:
            create_kwargs["scoped_to_membership_user_id"] = scoped_to_membership.user_id
        system_user = SystemUser.objects.create(**create_kwargs)

        # Audit: a system-user (API integration credential) is provisioned for the org.
        # The actor is whoever created it (system when the caller names no one); when
        # the token is membership-scoped, that membership is the affected party.
        #
        # Skipped entirely for an org-less token: `OrganizationAuditService.record` requires an
        # `organization_id` (the audit trail is org-scoped by design and has no sentinel
        # for "all organizations"), and this line previously raised `AttributeError` on
        # `None` — the org-less admin path never reached a successful return. The WARNING
        # logged above is the record for this case until the audit trail grows a
        # cross-organization scope.
        if organization is not None:
            self.audit_service.record(
                action=AuditAction.CREATE,
                actor=self.audit_service.actor_from_user_or_token(acted_by, organization.id),
                subject=self.audit_service.subject_from_instance(system_user),
                scope=self.audit_service.scope_from_organization_id(organization.id),
                affected=self.audit_service.affected_from_membership_ids(
                    organization.id,
                    ([scoped_to_membership.user_id] if scoped_to_membership is not None else []),
                ),
                # Only when the switch starts on, so every other create record keeps
                # the shape it always had.
                diff=(
                    {"requires_on_behalf_of": {"old": None, "new": True}}
                    if requires_on_behalf_of
                    else None
                ),
            )
        return system_user, token

    def set_requires_on_behalf_of(
        self,
        system_user: SystemUser,
        requires_on_behalf_of: bool,
        *,
        acted_by: "User | SystemUser | None",
    ) -> bool:
        """Turn the token's ``requires_on_behalf_of`` switch on or off, and audit the change.

        :param system_user: The token to change.
        :param requires_on_behalf_of: The new value.
        :param acted_by: Who made the change, recorded as the audit actor. A user who is
            not a member of the token's organization (staff in the admin) and ``None``
            are both recorded as the system.
        :raises OnBehalfOfSwitchOnScopedTokenError: When turning the switch on for a token
            scoped to a member. That token ignores the header, so it could never write again.
        :return: Whether the value changed. Setting the value it already has does nothing
            and writes no audit record.

        The audit record is skipped for an org-less token, for the same reason as in
        :meth:`create_system_user`: the audit trail has no scope for "every organization".
        """
        if requires_on_behalf_of and system_user.scoped_to_membership_user_id is not None:
            raise OnBehalfOfSwitchOnScopedTokenError()

        previous = system_user.requires_on_behalf_of
        if previous == requires_on_behalf_of:
            return False

        system_user.requires_on_behalf_of = requires_on_behalf_of
        system_user.save(update_fields=["requires_on_behalf_of"])

        if system_user.organization_id is not None:
            self.audit_service.record(
                action=AuditAction.UPDATE,
                actor=self.audit_service.actor_from_user_or_token(
                    acted_by, system_user.organization_id
                ),
                subject=self.audit_service.subject_from_instance(system_user),
                scope=self.audit_service.scope_from_organization_id(system_user.organization_id),
                diff={"requires_on_behalf_of": {"old": previous, "new": requires_on_behalf_of}},
            )
        return True

    def on_behalf_of_claim(
        self,
        system_user: SystemUser,
        organization: "Organization",
        header: str | None,
    ) -> AbstractContextManager[None]:
        """Check a mutation's ``X-Public-Api-On-Behalf-Of`` header and return the claim to bind.

        :param system_user: An organization-wide or org-less token making a mutation.
        :param organization: The organization the request is bound to.
        :param header: The raw header value, or ``None`` when it was not sent.
        :raises OnBehalfOfMemberRequiredError: The header is missing and the token has
            ``requires_on_behalf_of`` set.
        :raises OnBehalfOfMemberInvalidError: The header is not a positive integer, or does
            not name an active member of ``organization``.
        :return: A context manager. Inside it, every audit record the token writes carries
            the member as a claim. A no-op when no header was sent.
        """
        if header is None:
            if system_user.requires_on_behalf_of:
                raise OnBehalfOfMemberRequiredError()
            return nullcontext()

        # Only a plain positive integer is accepted: anything else (empty, signed,
        # padded, a float) is not an id a partner could have read from this API.
        value = header.strip()
        membership = (
            OrganizationMembership.objects.active()
            .filter(organization_id=organization.id, user_id=int(value))
            .select_related("user")
            .first()
            if value.isascii() and value.isdigit() and int(value) > 0
            else None
        )
        if membership is None:
            raise OnBehalfOfMemberInvalidError()
        return self.audit_service.on_behalf_of_member(system_user, membership)
