"""Owns identifier reads and replace-the-set writes for ``ExternalClientIdentifier``.

Every write path in this feature (``CalendarEventService.create_event`` /
``update_event`` today; the GraphQL / REST write paths in later phases) goes through
``replace_for_target`` rather than touching ``ExternalClientIdentifier`` directly, so
normalization, the allowlist check, and the cross-organization guard are enforced
exactly once.

**Organization safety is code-enforced, not schema-enforced.** A ``GenericForeignKey``
cannot be an ``OrganizationSafeForeignKey``, so nothing in the schema stops an
identifier row in organization A from pointing at a record in organization B. This
service is the one place that closes that gap: every write validates the target's
``organization_id`` against the organization this instance is bound to, and every read
goes through the org-scoped ``ExternalClientIdentifier.objects`` manager.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.contenttypes.models import ContentType

from calendar_integration.exceptions import (
    CalendarServiceOrganizationNotSetError,
    ExternalClientIdentifierBlankIdentifierError,
    ExternalClientIdentifierCrossOrganizationError,
    ExternalClientIdentifierDuplicateSystemError,
    ExternalClientIdentifierInvalidTargetError,
    ExternalClientIdentifierTooLongError,
)
from calendar_integration.external_client_identifiers import (
    IDENTIFIABLE_MODELS,
    normalize_system,
)
from calendar_integration.models import ExternalClientIdentifier
from calendar_integration.services.dataclasses import ExternalClientIdentifierData


if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import Model

    from organizations.models import Organization


#: Matches ``ExternalClientIdentifier.identifier``'s column (``CharField(max_length=255)``).
MAX_IDENTIFIER_LENGTH = 255


class ExternalClientIdentifierService:
    """Reads and replace-the-set writes for ``(system, identifier)`` pairs.

    Must be initialized with ``initialize(organization)`` before use, mirroring
    ``BookingPolicyService``. Not a DI-container singleton state holder: a fresh
    instance is created per resolution and bound to one organization for its
    lifetime.
    """

    organization: Organization | None

    def __init__(self) -> None:
        self.organization = None

    def initialize(self, organization: Organization) -> None:
        """Bind this service instance to a tenant organization."""
        self.organization = organization

    def _assert_initialized(self) -> None:
        if self.organization is None:
            raise CalendarServiceOrganizationNotSetError(
                "ExternalClientIdentifierService requires an organization. Call initialize()."
            )

    @staticmethod
    def _content_type_label(content_type: ContentType) -> str:
        return f"{content_type.app_label}.{content_type.model}"

    def _validate_target(self, target: Model) -> ContentType:
        """Resolve and validate ``target``'s ``ContentType``.

        Raises ``ExternalClientIdentifierInvalidTargetError`` when the target's model
        is outside ``IDENTIFIABLE_MODELS``, and
        ``ExternalClientIdentifierCrossOrganizationError`` when the target's
        organization does not match the organization this service is bound to.
        """
        content_type = ContentType.objects.get_for_model(target)
        if self._content_type_label(content_type) not in IDENTIFIABLE_MODELS:
            raise ExternalClientIdentifierInvalidTargetError(self._content_type_label(content_type))

        target_organization_id = getattr(target, "organization_id", None)
        if target_organization_id != self.organization.id:  # type: ignore[union-attr]
            raise ExternalClientIdentifierCrossOrganizationError()

        return content_type

    @staticmethod
    def _normalize_and_validate(
        identifiers: list[ExternalClientIdentifierData],
    ) -> dict[str, str]:
        """Normalize ``system`` and validate ``identifier`` for every incoming pair.

        Returns a ``{normalized_system: identifier}`` mapping. The model's
        ``extclientid_uniq_target_system`` constraint allows at most one identifier
        per system per target. So if two incoming pairs normalize to the same
        system, this raises ``ExternalClientIdentifierDuplicateSystemError`` instead
        of silently keeping the last one and dropping the first.
        """
        normalized: dict[str, str] = {}
        for item in identifiers:
            identifier_value = item.identifier
            if not identifier_value or not identifier_value.strip():
                raise ExternalClientIdentifierBlankIdentifierError()
            if len(identifier_value) > MAX_IDENTIFIER_LENGTH:
                raise ExternalClientIdentifierTooLongError()
            normalized_system = normalize_system(item.system)
            if normalized_system in normalized:
                raise ExternalClientIdentifierDuplicateSystemError()
            normalized[normalized_system] = identifier_value
        return normalized

    def replace_for_target(
        self,
        target: Model,
        identifiers: list[ExternalClientIdentifierData] | None,
    ) -> tuple[list[ExternalClientIdentifierData], list[ExternalClientIdentifierData]]:
        """Replace the full identifier set stored for ``target``.

        ``identifiers is None`` is a no-op: nothing is written, and the returned
        ``(old, new)`` tuple carries the same (unchanged) state on both sides so a
        caller can diff without special-casing ``None``. ``identifiers == []`` clears
        every stored identifier for ``target``.

        Every non-``None`` call still validates the target (allowlist + organization)
        so a caller cannot slip a cross-organization or disallowed target past a ``[]``
        write either.

        Returns ``(old, new)`` -- both sorted by ``(system, identifier)`` for stable
        diffing -- so the caller can build an audit diff and skip it entirely when
        nothing changed.
        """
        self._assert_initialized()
        content_type = self._validate_target(target)

        existing_qs = ExternalClientIdentifier.objects.filter_by_organization(
            self.organization.id  # type: ignore[union-attr]
        ).filter(content_type=content_type, identified_key=target.pk)
        existing = {row.system: row.identifier for row in existing_qs}
        old_state = self._to_sorted_data(existing)

        if identifiers is None:
            return old_state, old_state

        normalized = self._normalize_and_validate(identifiers)
        new_state = self._to_sorted_data(normalized)

        if normalized == existing:
            return old_state, new_state

        systems_to_remove = set(existing) - set(normalized)
        systems_to_upsert = {
            system: identifier
            for system, identifier in normalized.items()
            if existing.get(system) != identifier
        }
        # A changed system's old row must be deleted before the new one is created --
        # both would otherwise collide on ``extclientid_uniq_target_system``.
        systems_to_delete = systems_to_remove | (set(systems_to_upsert) & set(existing))

        if systems_to_delete:
            existing_qs.filter(system__in=systems_to_delete).delete()

        if systems_to_upsert:
            ExternalClientIdentifier.objects.bulk_create(
                [
                    ExternalClientIdentifier(
                        organization=self.organization,
                        content_type=content_type,
                        identified_key=target.pk,
                        system=system,
                        identifier=identifier,
                    )
                    for system, identifier in systems_to_upsert.items()
                ]
            )

        return old_state, new_state

    def get_for_targets(
        self, targets: Sequence[Model]
    ) -> dict[tuple[int, int], list[ExternalClientIdentifierData]]:
        """Batch-read identifiers for many targets, possibly of different models.

        Returns a mapping keyed by ``(content_type_id, target.pk)``. Every key from
        ``targets`` is present in the result -- defaulting to an empty list when a
        target has no identifiers -- so callers never need ``.get(key, [])``. Targets
        outside ``IDENTIFIABLE_MODELS`` are not rejected here (this is a read path);
        they simply resolve to an empty list, since no write path could ever have
        stored a row for them.
        """
        self._assert_initialized()

        keys: list[tuple[int, int]] = []
        content_type_ids: set[int] = set()
        target_pks: set[int] = set()
        result: dict[tuple[int, int], list[ExternalClientIdentifierData]] = {}
        for target in targets:
            content_type = ContentType.objects.get_for_model(target)
            key = (content_type.id, target.pk)
            keys.append(key)
            result[key] = []
            content_type_ids.add(content_type.id)
            target_pks.add(target.pk)

        if not keys:
            return result

        rows = ExternalClientIdentifier.objects.filter_by_organization(
            self.organization.id  # type: ignore[union-attr]
        ).filter(content_type_id__in=content_type_ids, identified_key__in=target_pks)

        for row in rows:
            key = (row.content_type_id, row.identified_key)
            if key in result:
                result[key].append(
                    ExternalClientIdentifierData(system=row.system, identifier=row.identifier)
                )

        for row_identifiers in result.values():
            row_identifiers.sort(key=lambda data: (data.system, data.identifier))

        return result

    @staticmethod
    def _to_sorted_data(mapping: dict[str, str]) -> list[ExternalClientIdentifierData]:
        return sorted(
            (
                ExternalClientIdentifierData(system=system, identifier=identifier)
                for system, identifier in mapping.items()
            ),
            key=lambda data: (data.system, data.identifier),
        )
