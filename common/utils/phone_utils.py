def normalize_phone_number(phone: str) -> str:
    """Normalize a phone number to the E.164-ish form allauth ends up storing.

    Mirrors the formatting step of ``allauth.account.fields.PhoneField.clean``
    (``value.replace(" ", "").replace("-", "")``): strips incidental spaces
    and hyphens a client might submit. allauth applies this to every phone it
    hands to the adapter (``set_phone`` / ``send_verification_code_sms`` /
    etc.), but ``legal.serializers.ConsentCreateSerializer.phone_number`` is a
    plain, unvalidated ``CharField`` -- a client posting a human-formatted
    number to ``/consents/`` (e.g. ``"+1 555-555-0100"``) would otherwise be
    stored differently from the normalized value allauth later passes to
    ``ConsentService.has_sms_consent_for_phone``, permanently failing the
    exact-match lookup. Applying this same normalization on both the write
    (``ConsentService.record_consent``) and read (``UserConsentQuerySet.for_phone``)
    sides keeps them comparable.

    A blank/falsy `phone` is returned unchanged (never normalized into a
    truthy value) -- callers must not let a blank phone accidentally satisfy
    a phone-keyed consent check.
    """
    if not phone:
        return phone
    return phone.strip().replace(" ", "").replace("-", "")
