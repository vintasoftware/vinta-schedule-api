# SMS MFA Consent (Policy Documents + User Consent) — Implementation Plan

> No sibling `..._SPEC.md`. Decisions below were fixed through a Step 0 interrogation
> (batched `AskUserQuestion` rounds) recorded in **Guiding Decisions**. If a formal
> spec is wanted later, back-fill it from this plan; the phased contract here is the
> working agreement.

## 1. Goals

1. Reactivate django-allauth's native SMS phone verification (`ACCOUNT_PHONE_VERIFICATION_ENABLED`), currently disabled in [settings/base.py:349](../vinta_schedule_api/settings/base.py#L349) pending Twilio profile approval.
2. Introduce editable, immutably-versioned **policy documents** (privacy policy, terms of use, SMS-messaging consent) stored as markdown, manageable in Django admin.
3. Capture and durably record **per-user consent** with audit-grade proof, referencing the exact policy version accepted.
4. **Refuse to send any onboarding SMS** — email/password *and* OAuth2 signup paths — unless a valid SMS-consent record exists for the user, enforced server-side.
5. Expose the policy documents through read-only REST endpoints so the frontend can render them at the consent step.

**Non-goals:**
- Per-reseller / per-organization policy documents. v1 policies are global (Vinta-owned). Per-reseller policies are a future concern; `OrganizationBranding` exists ([organizations/models.py:473](../organizations/models.py#L473)) but is out of scope here.
- Re-consent on new policy versions. Any prior SMS-consent record satisfies the gate forever (version still pinned for proof). A future "re-accept updated terms" flow is out of scope.
- Adding SMS as an `allauth.mfa` second factor. This plan reactivates **phone verification** (the OTP-at-signup flow), not a new MFA factor type; `MFA_SUPPORTED_TYPES` stays `["totp", "recovery_codes"]`.
- Multi-language / localized policy documents.
- A general feature-flag system. The allauth setting is the rollout gate (see **Guiding Decisions**).
- Migrating or backfilling consent for users who already verified a phone before this feature (there are none while phone verification is off).

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Policy storage shape** | One `PolicyDocument` model with a `document_type` enum (`PRIVACY_POLICY` / `TERMS_OF_USE` / `SMS_CONSENT`) + markdown `TextField`. One model, one admin, one read API; adding a 4th doc type later is a new enum value, not a new table. No markdown library exists in the repo today — store raw markdown in a plain `TextField`, render client-side. |
| **Versioning** | **Immutable versions.** Each publish creates a new row (`document_type` + monotonically increasing `version`); rows are never edited after publish. A consent record FKs to the exact version. Proves precisely what text the user saw. "Latest" = highest `version` for a `document_type` among published rows. |
| **Consent grain** | `UserConsent` — one row per `(user, policy_document_version)` accepted. Full history, append-only. |
| **Consent proof** | Audit-grade: `accepted_at`, FK to the exact `PolicyDocument` version, client IP, user-agent, and an `acceptance_source` enum (`SIGNUP_FORM` / `OAUTH_STEP` / `API`). |
| **SMS consent isolation** | SMS/messaging consent is its own `document_type` (`SMS_CONSENT`), tracked and gated independently of privacy/terms so the Twilio opt-in is separately provable. |
| **Re-consent policy** | Consent-once-ever. The gate is satisfied by **any** `UserConsent` row whose document is of type `SMS_CONSENT`, regardless of version. |
| **Consent scope** | Global per-user. `users.User` is a global `BaseModel`, not tenant-scoped — `PolicyDocument` and `UserConsent` are **not** `OrganizationModel` subclasses and carry no `organization` FK. |
| **Gate design** | Capture **and** enforce. Consent is collected in the signup form (email path) and a mandatory frontend step (OAuth path); the authoritative guarantee is a **server-side gate** inside the adapter's `send_verification_code_sms` and the phone-verify endpoint — no valid `SMS_CONSENT` row ⇒ SMS is refused. |
| **Rollout gate — no feature flag** | The repo has **no feature-flag system** and the entire consent surface is **purely additive** (new models, new admin, new read endpoints, a gate that is only reachable once phone verification is on). The rollout gate is the existing allauth setting `ACCOUNT_PHONE_VERIFICATION_ENABLED`: everything ships while it stays `False`, and Phase 6 flips it after Twilio approval. Therefore **no new feature flag is declared and there is no flag-removal phase** — the equivalent "flip-on" step is Phase 6. |
| **Doc exposure** | Read-only REST endpoints in v1 (see **API Design**): list-latest-per-type, retrieve-latest-by-type, retrieve-by-id, and full-history-with-optional-type-filter. |
| **Audit** | Policy-document publishes and consent grants are recorded through the existing `AuditService` (consistent with the audit-trail rollout — business writes only). |
| **App placement** | New `legal` Django app owns `PolicyDocument`, `UserConsent`, their admin, factories, serializers, viewsets, and routes. Keeps a clean bounded context out of the allauth-glue `accounts` app and the identity-only `users` app. |
| **Phasing** | Bundled granularity (Step 0 choice): closely-related flows share a phase where it stays MR-sized and single-concern. |

## 3. Data Model Changes

New app `legal/` (scaffolded in Phase 1). Neither model is tenant-scoped.

### 3.1 New `legal.PolicyDocument`

Immutable, versioned, global. Editable/creatable in Django admin; existing rows are read-only after publish (enforced in admin, not just convention).

```python
# legal/models.py
class PolicyDocumentType(models.TextChoices):
    PRIVACY_POLICY = "privacy_policy", "Privacy Policy"
    TERMS_OF_USE = "terms_of_use", "Terms of Use"
    SMS_CONSENT = "sms_consent", "SMS Messaging Consent"


class PolicyDocument(BaseModel):
    document_type = models.CharField(max_length=32, choices=PolicyDocumentType.choices)
    version = models.PositiveIntegerField()          # monotonic per document_type
    title = models.CharField(max_length=255)
    body_markdown = models.TextField()               # raw markdown, rendered client-side
    published_at = models.DateTimeField(default=timezone.now)

    objects = PolicyDocumentManager()                # .latest_for(type), .latest_per_type()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["document_type", "version"],
                name="uq_policydocument_type_version",
            ),
        ]
        indexes = [models.Index(fields=["document_type", "-version"])]
        ordering = ["document_type", "-version"]
```

- Custom `PolicyDocumentManager` / queryset with `latest_for(document_type)` and `latest_per_type()` (one latest row per type). Use the project's manager+queryset pattern (`create-model` skill).
- Export from `legal/__init__.py` / model package as the app dictates.

### 3.2 New `legal.UserConsent`

Append-only, one row per accepted document version, global per-user.

```python
# legal/models.py
class ConsentSource(models.TextChoices):
    SIGNUP_FORM = "signup_form", "Signup Form"
    OAUTH_STEP = "oauth_step", "OAuth Consent Step"
    API = "api", "API"


class UserConsent(BaseModel):
    user = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="consents")
    policy_document = models.ForeignKey(PolicyDocument, on_delete=models.PROTECT, related_name="consents")
    accepted_at = models.DateTimeField(default=timezone.now)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    source = models.CharField(max_length=16, choices=ConsentSource.choices)

    objects = UserConsentManager()                   # .has_sms_consent(user)

    class Meta:
        indexes = [models.Index(fields=["user", "policy_document"])]
```

- `on_delete=PROTECT` on `policy_document` — an accepted version must never vanish (proof integrity).
- `UserConsentManager.has_sms_consent(user) -> bool`: `exists()` of any row whose `policy_document.document_type == SMS_CONSENT`. This is the single predicate the gate calls (re-consent policy = consent-once-ever).

### 3.3 Type plumbing

- `PolicyDocumentType`, `ConsentSource` enums (above) exported for reuse by serializers, the gate, and the consent-recording service.
- A small `ConsentService` (DI-registered, [di_core/containers.py](../di_core/containers.py)) exposes `record_consent(user, document_type, *, source, ip, user_agent)` and `has_sms_consent(user)`; business logic lives here, not in views/adapters. It calls `AuditService` on write.

## 4. API Design

All endpoints read-only (`GET`), under the internal REST surface, wired via `legal/routes.py` and the project's `VintaScheduleModelViewSet` base (`create-rest-endpoint` skill). Serializer returns `id`, `document_type`, `version`, `title`, `body_markdown`, `published_at`. Auth follows the project's default internal-session posture; policy documents are non-secret (safe to expose to authenticated onboarding clients — confirm public-vs-authenticated in Open Questions).

### 4.1 Latest documents

- `GET /api/legal/policy-documents/latest/` → **list**, one latest published version per `document_type` (uses `latest_per_type()`).
- `GET /api/legal/policy-documents/latest/{document_type}/` → **retrieve**, latest published version of that single type. `404` on unknown/empty type.

### 4.2 By id

- `GET /api/legal/policy-documents/{id}/` → **retrieve** an exact version by primary key.

### 4.3 History

- `GET /api/legal/policy-documents/` → **list** full version history, newest first. Optional `?document_type=<enum>` filter (django-filter filterset). No filter ⇒ every version of every type.

> Consent **writes** (recording acceptance) are not a public CRUD surface. They happen through the signup form, the OAuth consent-step endpoint (Phase 4), and internally via `ConsentService`. A dedicated authenticated `POST` consent endpoint is added in Phase 4 for the OAuth path.

## 5. Phased Rollout

Ordered so the additive data + API surface lands first, capture and enforcement build on it, and the Twilio-dependent flip is last (slowest external dependency, but it gates nothing downstream so it sits at the end).

---

### Phase 1 — Scaffold `legal` app: `PolicyDocument` model + admin

**Goal**: A new `legal` app with the immutable, versioned `PolicyDocument` model, manageable in Django admin. Ship value: admins can author policy documents; nothing reads them yet.

**Feature flag**: none — purely additive new app/table/admin, no existing code path touched.

Changes:
1. New app `legal/` (`apps.py`, `__init__.py`, `migrations/`); register in `INSTALLED_APPS` ([settings/base.py](../vinta_schedule_api/settings/base.py)).
2. `legal/models.py`: `PolicyDocument` + `PolicyDocumentType` + `PolicyDocumentManager` (`latest_for`, `latest_per_type`).
3. `legal/admin.py`: register `PolicyDocument`; make published rows read-only (fields non-editable after creation), list filter by `document_type`, order by `-version`. Auto-suggest next `version` per type on add.
4. `legal/factories.py`: `PolicyDocumentFactory` (model_bakery, matching [users/factories.py](../users/factories.py)).
5. Migration adding the table + unique constraint + index.

Spec use-case: shared scaffolding + policy-authoring.

Tests:
- **Unit**: `legal/tests/test_policy_document_model.py` — `latest_for` / `latest_per_type` return highest version per type; unique `(type, version)` enforced.
- **Integration**: `legal/tests/test_policy_document_admin.py` — admin can create a version; published row is read-only.

**Suggested AI model**: Tier 1 for app scaffold + migration + admin; step to Tier 2 for the manager/queryset methods. IDs per tier in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml) (T1 `claude-haiku-4-5`; T2 `claude-haiku-4-5` w/ iteration).

**Reusable skills**: `add-model` (new model + manager + admin + factory), `add-migration`.

Acceptance: A superuser can create `PrivacyPolicy` / `TermsOfUse` / `SmsConsent` versions in Django admin; `PolicyDocument.objects.latest_for("sms_consent")` returns the highest-version row. `MFA_SUPPORTED_TYPES` and all signup flows are byte-for-byte unchanged.

---

### Phase 2 — Policy document read REST API

**Goal**: The four read-only endpoints (latest-list, latest-by-type, by-id, history+filter) so a client can fetch and render policy markdown.

**Feature flag**: none — new read-only endpoints at new paths; no existing route or response shape changes.

Changes:
1. `legal/serializers.py`: `PolicyDocumentSerializer` (read-only).
2. `legal/views.py`: viewset(s) on `VintaScheduleModelViewSet` with the four actions — `list` (history), `latest` (list), `latest/{document_type}` (retrieve), `retrieve` by id.
3. `legal/filters.py`: `PolicyDocumentFilterSet` with optional `document_type`.
4. `legal/routes.py`: register routes; include in the API router.
5. Regenerate `schema.yml` (drf-spectacular).

Spec use-case: policy-document exposure (all four read endpoints).

Tests:
- **Integration**: `legal/tests/test_policy_document_api.py` — latest-list returns one row per type; latest-by-type returns highest version; by-id returns exact version; history returns all, and `?document_type=` filters; unknown type ⇒ 404 on retrieve / empty on list.

**Suggested AI model**: Tier 2 — serializer + viewset + filterset against established patterns. IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: `create-rest-endpoint`, `write-tests`.

Acceptance: All four endpoints return correct payloads per the tests; `schema.yml` regenerated and committed; no existing endpoint changes.

---

### Phase 3 — `UserConsent` model + `ConsentService` + audit

**Goal**: Durable, audit-grade consent records and the service that writes/queries them. Ship value: consent can be recorded and queried internally; not yet wired to any flow.

**Feature flag**: none — new table + new service; no existing path reads or writes it yet.

Changes:
1. `legal/models.py`: `UserConsent` + `ConsentSource` + `UserConsentManager.has_sms_consent(user)`.
2. `legal/services.py`: `ConsentService.record_consent(user, document_type, *, source, ip, user_agent)` — resolves the latest `PolicyDocument` of `document_type`, creates a `UserConsent`, records via `AuditService`; `has_sms_consent(user)`.
3. Register `ConsentService` in DI ([di_core/containers.py](../di_core/containers.py)).
4. `legal/factories.py`: `UserConsentFactory`.
5. Migration for `UserConsent` (FK `PROTECT` to `PolicyDocument`, FK `CASCADE` to `users.User`, index).

Spec use-case: consent persistence + audit.

Tests:
- **Unit**: `legal/tests/test_consent_service.py` — `record_consent` pins the latest version, stores IP/UA/source, emits an audit entry; `has_sms_consent` true only when an `SMS_CONSENT` row exists (any version).
- **Integration**: `legal/tests/test_userconsent_model.py` — deleting a consented `PolicyDocument` is blocked by `PROTECT`.

**Suggested AI model**: Tier 3 — service coordinating model + audit + DI, with the has-sms-consent predicate semantics. IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml) (T3 `claude-sonnet-4-6`).

**Reusable skills**: `add-model`, `add-migration`, `write-tests`.

Acceptance: `ConsentService.record_consent(user, "sms_consent", source=..., ip=..., user_agent=...)` creates a version-pinned row with an audit entry, and `has_sms_consent(user)` returns `True` thereafter; no signup flow changed yet.

---

### Phase 4 — Capture consent across signup paths (email form + OAuth step)

**Goal**: Collect SMS (and privacy/terms) consent during onboarding for both the email/password signup form and the OAuth2 signup, persisting it via `ConsentService`.

**Feature flag**: none — additive fields/endpoint. Guarded by the fact that phone verification is still off (Phase 6), so no SMS is triggered by this capture yet.

Changes:
1. `accounts/base_forms.py` `BaseVintaScheduleSignupForm`: add required consent acknowledgement field(s); in `signup(request, user)`, call `ConsentService.record_consent(..., source=SIGNUP_FORM, ip, user_agent)` for the SMS-consent (and privacy/terms) documents. See existing `signup` at [accounts/base_forms.py:47](../accounts/base_forms.py#L47).
2. `legal/views.py` (or `accounts`): authenticated `POST /api/legal/consents/` endpoint for the OAuth post-signup step — records consent with `source=OAUTH_STEP`. OAuth signups use `SOCIALACCOUNT_AUTO_SIGNUP` ([settings/base.py:322](../vinta_schedule_api/settings/base.py#L322)) and collect no phone at signup; the frontend calls this endpoint (and sets the phone) before requesting phone verification.
3. Serializer/validation for the consent-record endpoint (document type(s), captures IP + user-agent from request).

Spec use-case: consent capture (email + OAuth), bundled.

Tests:
- **Integration**: `accounts/tests/test_signup_consent.py` — email signup persists a version-pinned `SMS_CONSENT` `UserConsent` with `source=SIGNUP_FORM`; missing consent field ⇒ form invalid.
- **Integration**: `legal/tests/test_consent_endpoint.py` — authenticated `POST` records consent with `source=OAUTH_STEP`, IP + UA captured; unauthenticated ⇒ 401.

**Suggested AI model**: Tier 3 — spans allauth form hook + a new endpoint + service wiring across two flows. IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: `create-rest-endpoint` (consent endpoint), `write-tests`.

Acceptance: Completing the email signup form records an SMS-consent row; the OAuth path can record one through the endpoint; both carry audit-grade metadata. Phone verification still off ⇒ no SMS sent.

---

### Phase 5 — Enforce the SMS consent gate

**Goal**: Server-side guarantee that no verification SMS is sent to a user lacking a valid SMS-consent record — the authoritative backstop behind the capture UX.

**Feature flag**: none — the branch it adds is only reachable once Phase 6 turns phone verification on; until then `send_verification_code_sms` is not called.

Changes:
1. `accounts/account_adapters.py` `AccountAdapter.send_verification_code_sms` ([account_adapters.py:429](../accounts/account_adapters.py#L429)): before dispatching the notification, call `ConsentService.has_sms_consent(user)`; if `False`, do **not** send — log and raise/return the project's "consent required" error (new entry in [accounts/exceptions.py](../accounts/exceptions.py)) so the phone-verify request surfaces a clear, actionable error to the client.
2. Guard the phone-verify request entry point so the API returns a well-formed error (not a 500) when consent is missing.
3. Ensure the error is distinguishable by the frontend so it can route the user to the consent step.

Spec use-case: SMS consent enforcement gate.

Tests:
- **Unit**: `accounts/tests/test_sms_consent_gate.py` — `send_verification_code_sms` sends when `has_sms_consent` is `True`; refuses (no notification dispatched) and raises the consent-required error when `False`.
- **Integration**: phone-verify request without consent ⇒ deterministic client error, zero Twilio/notification calls (assert the notification adapter is not invoked).

**Suggested AI model**: Tier 3 — security-sensitive gate in the SMS dispatch path with error semantics. IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: `write-tests`.

Acceptance: With phone verification enabled in a test, a consent-less user triggers **zero** SMS sends and receives the consent-required error; a consented user receives the OTP as before.

---

### Phase 6 — Make phone verification env-driven (per-environment rollout)

**Goal**: Convert `ACCOUNT_PHONE_VERIFICATION_ENABLED` from a hardcoded `False` into an **environment variable** (`config(..., cast=bool, default=False)`) so each environment enables SMS phone verification independently — staging can turn it on for testing while production stays off until Twilio approves, with **no code change to flip**. Safe to ship immediately: the default is `False`, so behavior is unchanged everywhere until an environment opts in.

The Phase 5 anti-enumeration blocker is **resolved by Phase 8** (phone-keyed consent gates all SMS). The only remaining real-world gate is Twilio approving the messaging profile — which an operator now satisfies by setting the env var in that environment, not by shipping code.

**Feature flag**: the env var *is* the rollout control — `ACCOUNT_PHONE_VERIFICATION_ENABLED` (bool, per-environment, default `False`).

Changes:
1. `vinta_schedule_api/settings/base.py`: `ACCOUNT_PHONE_VERIFICATION_ENABLED = config("ACCOUNT_PHONE_VERIFICATION_ENABLED", cast=bool, default=False)` (reversing the hardcoded `False` from commit `62a3968` into an env-driven default-off).
2. Wire the new env var through every layer per the `add-env-var` skill: `.env.example`, `.env.docker.example`, Render `envVarGroups` in `render.yaml` (default/false; operators flip in the Render dashboard once Twilio approves), the CI workflow env, and the AGENTS.md env section.
3. Verify Twilio env/config present in all environments (`TWILIO_*`, [settings/base.py:499-506](../vinta_schedule_api/settings/base.py#L499-L506)); no code change to the send path ([vintasend_twilio](../vintasend_twilio/services/notification_adapters/twilio.py)).

Spec use-case: SMS MFA reactivation (env-driven rollout).

Tests:
- **Integration**: with the env var enabled (`@override_settings(ACCOUNT_PHONE_VERIFICATION_ENABLED=True)`), a consented user completes phone verification end-to-end (SMS stubbed); the gate still refuses a consent-less user. With it disabled (default), the phone-verify flow is inert.
- **Unit**: settings resolves the env var (truthy string → True; unset → False).

**Suggested AI model**: Tier 2 — env-var plumbing across settings + Render + CI + docs, plus a small settings/integration test. IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: `add-env-var`.

Acceptance: `ACCOUNT_PHONE_VERIFICATION_ENABLED` is read from the environment, defaults to `False` (no behavior change on deploy), and setting it truthy in an environment enables phone verification there; the var is wired through `.env.example`, `.env.docker.example`, `render.yaml`, CI, and AGENTS.md. Production stays off until an operator sets it (post-Twilio-approval).

---

### Phase 7 — Frontend handoff document

**Goal**: A single markdown handoff doc that tells the frontend team exactly what to build against the backend shipped in Phases 1–6 — the privacy-policy page, the terms-of-use page, and the consent request in the signup flow (email + OAuth2). Ship value: unblocks frontend implementation; no backend behavior change.

**Feature flag**: none — documentation only, no code.

Changes:
1. New doc `ai-plans/2026-07-04-SMS_MFA_CONSENT_FRONTEND_HANDOFF.md` covering:
   - **Privacy Policy page** — fetch + render the latest `PRIVACY_POLICY` document from the read API (endpoint from the Policy document read REST API phase, `latest/{document_type}`), render `body_markdown` as markdown, show `title` + `published_at`/`version`.
   - **Terms of Use page** — same, for `TERMS_OF_USE`.
   - **Consent request in signup** — the SMS/messaging consent acknowledgement UI for both the email/password signup form and the OAuth2 post-signup step; which document(s) to display (link to privacy + terms, dedicated `SMS_CONSENT` opt-in), how consent is submitted (signup form field for email path; the authenticated `POST` consent endpoint for the OAuth path), and that phone verification will be refused by the backend until an `SMS_CONSENT` record exists (surface the consent-required error → route the user to the consent step).
2. Include, per surface: endpoint method + path + response shape, the `document_type` enum values, request payloads for consent submission, error states (esp. the consent-required error from the gate), and empty/loading states.
3. Cross-reference the exact endpoints and payloads as actually implemented in the earlier phases (read the merged code, not just the plan — capture real field names + error codes).

Spec use-case: frontend enablement handoff.

Tests: none — documentation only.

**Suggested AI model**: Tier 2 — synthesizes the shipped API surface into a clear consumer-facing doc; must read real code from prior phases. IDs per tier in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: none.

Acceptance: `ai-plans/2026-07-04-SMS_MFA_CONSENT_FRONTEND_HANDOFF.md` exists and documents all three surfaces (privacy page, terms page, signup consent) with concrete endpoints, payloads, and error states matching the merged backend.

---

### Phase 8 — Phone-keyed consent (cover anti-enumeration SMS)

**Goal**: Close the compliance gap from Phase 5 — no SMS of ANY kind (verification, unknown-account, account-already-exists) is sent to a phone number without a recorded SMS consent for that phone. Resolves the "anti-enumeration SMS are ungated" Open Question and unblocks Phase 6.

**Feature flag**: none — extends the existing gate; still additive while phone verification is off.

Changes:
1. `legal/models.py`: add `phone_number` (`CharField`, blank default `""`, indexed) to `UserConsent`. Migration `0003` (additive column). `user` stays required — every recording site (signup, `/consents/`) has a user; the phone check filters by `phone_number` regardless of user.
2. `legal/managers.py` + `legal/services.py`: add `has_sms_consent_for_phone(phone) -> bool` (exists an `SMS_CONSENT` row with that `phone_number`). `ConsentService.record_consent(..., phone_number="")` gains the param.
3. Recording sites capture the phone:
   - `accounts/base_forms.py` signup: record SMS consent with `phone_number=user.phone_number` (the phone allauth set during signup).
   - `POST /consents/`: accept optional `phone_number` in the payload so an OAuth user can consent their phone before verification.
4. `accounts/account_adapters.py`:
   - `send_verification_code_sms`: gate on `has_sms_consent_for_phone(phone)` (raise `ConsentRequiredError` → clean 403, as today).
   - `send_unknown_account_sms` / `send_account_already_exists_sms`: if the phone has no SMS consent → **log and return (no-op, no SMS, no error)** so allauth's enumeration-prevention response stays uniform; if it does have consent (the phone's owner consented at signup), send as before. Inject `consent_service` (already injected on the adapter).
5. Update the frontend handoff doc (`ai-plans/2026-07-04-SMS_MFA_CONSENT_FRONTEND_HANDOFF.md`): login-by-phone / change-phone must record consent for the new phone (via `POST /consents/` with `phone_number`) before a code can be sent; document that a phone with no consent silently receives no anti-enumeration SMS.

Spec use-case: SMS consent enforcement — phone-scoped, all SMS types.

Tests:
- **Unit** `legal/tests/` — `has_sms_consent_for_phone` true only for a matching `SMS_CONSENT` phone row; `record_consent` stores `phone_number`.
- **Unit** `accounts/tests/` — `send_unknown_account_sms` / `send_account_already_exists_sms` dispatch **nothing** when the phone has no consent (and raise no error), and dispatch when it does; `send_verification_code_sms` gates on phone consent; signup records a phone-keyed consent row.

**Suggested AI model**: Tier 3 — model + service + adapter across two apps with the enumeration-preserving no-op semantics. IDs per tier in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: `add-migration`, `write-tests`.

Acceptance: A phone with no `SMS_CONSENT` consent receives zero SMS of any type (verification raises `consent_required`; enumeration silently sends nothing); a consented phone's flows are unchanged. The Phase 6 anti-enumeration blocker is cleared.

### Phase 9 — Separate SMS consent checkbox (Twilio compliance)

**Goal**: Twilio (and TCPA) require SMS consent to be its **own explicit, separate, unchecked** opt-in — not bundled with Terms/Privacy acceptance. Phase 4 shipped a single combined `accepted_policies` checkbox; this splits it into two required checkboxes so the SMS opt-in is distinct.

**Feature flag**: none — signup-form field change; still gated overall by phone verification being off until Phase 6's env var is set.

Changes:
1. `accounts/base_forms.py`: replace the single `accepted_policies` `BooleanField` with two required fields:
   - `accepted_terms` — "I agree to the Privacy Policy and Terms of Use." (records `PRIVACY_POLICY` + `TERMS_OF_USE`).
   - `accepted_sms_consent` — "I agree to receive SMS text messages (e.g. verification codes) at the phone number I provide. Msg & data rates may apply." (records `SMS_CONSENT`). Separate, unchecked, its own explicit label.
   `_record_signup_consents` records each document type under its corresponding field (both required, so all three still recorded — but SMS is now driven by its own explicit opt-in, and the structure supports making SMS optional later without touching terms).
2. Regenerate `schema-auth.yml` / `schema.yml` — the headless signup contract changes (`accepted_policies` → `accepted_terms` + `accepted_sms_consent`).
3. Update `ai-plans/2026-07-04-SMS_MFA_CONSENT_FRONTEND_HANDOFF.md` §3a — two separate required checkboxes with the exact consent copy; note the field rename.

Spec use-case: SMS consent capture — explicit separate opt-in.

Tests:
- `accounts/tests/test_signup_consent.py` + `test_signup_consent_headless.py`: signup requires BOTH checkboxes (missing either → invalid); checking both records `SMS_CONSENT` (from the SMS field) + privacy/terms (from the terms field); the SMS row is driven specifically by `accepted_sms_consent`. Update all existing signup fixtures/payloads from `accepted_policies` to the two new fields.

**Suggested AI model**: Tier 2 — form field split + test/fixture updates + schema regen + doc. IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: `write-tests`.

Acceptance: The headless signup form exposes two separate required booleans — `accepted_terms` and `accepted_sms_consent` — the SMS opt-in is its own explicit checkbox, and signup records `SMS_CONSENT` only when `accepted_sms_consent` is checked. Handoff doc + schema updated.

## 6. Risk & Rollout Notes

- **Rollout gate**: `ACCOUNT_PHONE_VERIFICATION_ENABLED` (global setting) — not a per-tenant flag. Phases 1–5 ship while it stays `False` (no SMS reachable, consent surface dormant-but-usable). Phase 6 flips it only after Twilio approval. **Flip-on criterion**: Twilio profile approved + Phases 1–5 merged + green on staging. **Rollback**: revert Phase 6's one-line setting change (byte-for-byte back to disabled); the consent tables/API remain harmlessly in place. No new flag ⇒ no flag-removal phase.
- **Migrations**: two additive `CREATE TABLE`s (`PolicyDocument`, `UserConsent`) on cold tables — no locks on hot paths, no rewrites, no backfill. `UserConsent.policy_document` is `PROTECT` (proof integrity); `user` is `CASCADE`.
- **No backfill**: phone verification has been off, so no already-verified users need retroactive consent.
- **Security**: the gate is the guarantee, not the UI. Enforcement lives server-side in `send_verification_code_sms` + the phone-verify entry point (Phase 5); capture UX (Phase 4) is convenience. Both must ship before Phase 6. Do not flip Phase 6 if Phase 5 is not merged.
- **Twilio compliance**: audit-grade proof (IP, user-agent, timestamp, exact version, source) is captured to answer carrier/Twilio opt-in disputes. SMS consent is its own document type, separately provable.
- **Audit**: consent grants + policy publishes flow through `AuditService` (business writes), matching the audit-trail rollout scope.

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| Are the policy read endpoints **public** (unauthenticated) or **authenticated-only**? OAuth users may need to read SMS-consent text before they have a session. | Make the `latest` read endpoints public read-only (documents are non-secret); keep history/by-id authenticated. Confirm before Phase 2. | Product / Eng |
| Should privacy + terms consent be **required** alongside SMS consent in the signup form, or is only SMS consent gate-relevant? | Capture all three at signup (form has room), but only `SMS_CONSENT` gates SMS. Privacy/terms acceptance is recorded for completeness. | Product |
| Does the OAuth consent-step endpoint also **set the phone number**, or is phone set through a separate existing allauth call? | Record consent in the new endpoint; set phone via allauth's existing phone-set path (`AccountAdapter.set_phone`, [account_adapters.py:407](../accounts/account_adapters.py#L407)) so we don't fork phone handling. | Eng |
| Admin publish UX: auto-increment `version` per type, or manual entry? | Auto-suggest `max(version)+1` per `document_type` on the add form; reject duplicates via the unique constraint. | Eng |
| **Anti-enumeration SMS are ungated** (surfaced in Phase 5 review). `send_unknown_account_sms` / `send_account_already_exists_sms` send SMS to a raw phone with no consent check and no `user`. | **RESOLVED by Phase 8** — phone-keyed consent gates all SMS; an unconsented phone silently receives no anti-enumeration SMS (enumeration-prevention preserved). | Done (Phase 8) |
| **Pre-org consent has no AuditService row** (surfaced in Phase 3). `Audit` is tenant-scoped (`OrganizationModel`, needs `organization_id`), but consent is captured during signup *before* the user has an organization (email path provisions the org on email confirmation; OAuth in the adapter). So `ConsentService` emits an `AuditService` record only when the user already has an active membership, and skips it otherwise — which is the majority signup case. The `UserConsent` row itself carries the audit-grade proof (timestamp, exact version, IP, user-agent, source), so consent proof does not depend on the org-scoped audit trail. | Accept: `UserConsent` is the source of truth for consent proof; the org-scoped `Audit` entry is a best-effort secondary log for post-org grants. If a global (non-tenant) audit stream is later wanted for pre-org events, add it then. | Product / Eng |

## 8. Touch List

**Phase 1 — scaffold + model**
- `@legal/__init__.py`, `@legal/apps.py`, `@legal/models.py`, `@legal/admin.py`, `@legal/factories.py`, `@legal/migrations/0001_initial.py` (new)
- [vinta_schedule_api/settings/base.py](../vinta_schedule_api/settings/base.py) — add `legal` to `INSTALLED_APPS`

**Phase 2 — read API**
- `@legal/serializers.py`, `@legal/views.py`, `@legal/filters.py`, `@legal/routes.py` (new)
- API router include + `schema.yml` regenerate

**Phase 3 — consent model + service**
- [legal/models.py](../legal/models.py) — add `UserConsent`, `ConsentSource`, `UserConsentManager`
- `@legal/services.py` (new)
- [di_core/containers.py](../di_core/containers.py) — register `ConsentService`
- [legal/factories.py](../legal/factories.py) — `UserConsentFactory`
- `@legal/migrations/0002_userconsent.py` (new)

**Phase 4 — capture**
- [accounts/base_forms.py](../accounts/base_forms.py) — consent field + `signup()` record call
- [legal/views.py](../legal/views.py) / `@legal/serializers.py` — authenticated consent-record endpoint

**Phase 5 — gate**
- [accounts/account_adapters.py](../accounts/account_adapters.py#L429) — consent check in `send_verification_code_sms`
- [accounts/exceptions.py](../accounts/exceptions.py) — consent-required error

**Phase 6 — reactivation**
- [vinta_schedule_api/settings/base.py:349](../vinta_schedule_api/settings/base.py#L349) — `ACCOUNT_PHONE_VERIFICATION_ENABLED = True`

**Phase 7 — frontend handoff**
- `@ai-plans/2026-07-04-SMS_MFA_CONSENT_FRONTEND_HANDOFF.md` (new) — privacy page, terms page, signup consent request

**Phase 9 — separate SMS consent checkbox**
- [accounts/base_forms.py](../accounts/base_forms.py) — split `accepted_policies` into `accepted_terms` + `accepted_sms_consent`
- [accounts/tests/](../accounts/tests/) — update signup fixtures + assertions
- [ai-plans/2026-07-04-SMS_MFA_CONSENT_FRONTEND_HANDOFF.md](2026-07-04-SMS_MFA_CONSENT_FRONTEND_HANDOFF.md) — two-checkbox update; `schema-auth.yml` regen

**Phase 8 — phone-keyed consent**
- [legal/models.py](../legal/models.py) — `UserConsent.phone_number` + migration `0003`
- [legal/managers.py](../legal/managers.py), [legal/services.py](../legal/services.py) — `has_sms_consent_for_phone`
- [accounts/base_forms.py](../accounts/base_forms.py), [legal/views.py](../legal/views.py) — record phone; `/consents/` optional `phone_number`
- [accounts/account_adapters.py](../accounts/account_adapters.py) — gate all three SMS methods on phone consent
- [ai-plans/2026-07-04-SMS_MFA_CONSENT_FRONTEND_HANDOFF.md](2026-07-04-SMS_MFA_CONSENT_FRONTEND_HANDOFF.md) — update for phone consent

**Tests (per phase)**
- `@legal/tests/` — model, admin, api, service, consent-endpoint, userconsent tests
- `@accounts/tests/test_signup_consent.py`, `@accounts/tests/test_sms_consent_gate.py` (new)
