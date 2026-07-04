# SMS MFA Consent — Frontend Handoff

Backend for the SMS-consent feature (plan `2026-07-03-SMS_MFA_CONSENT_IMPLEMENTATION_PLAN.md`, Phases 1–8) is merged. This document tells the frontend what to build against it: the **Privacy Policy page**, the **Terms of Use page**, and the **consent request in the signup flow** (email + OAuth2).

**Phase 8 update — consent is now phone-keyed, not just user-keyed.** Every recorded `sms_consent` row now carries the phone number it applies to. This closes a gap where the backend's anti-enumeration SMS (sent for *login-by-phone* against an unknown phone, or *signup* against a phone that already has an account) had no gate at all, because those flows have no authenticated `user` — only a submitted phone. Concretely:

- The verification-code SMS gate now checks consent **for the submitted phone**, not just "does this user have any SMS consent." A user who consented with one phone number does not automatically get SMS-verification-gate access for a *different* phone — that phone needs its own consent row.
- The two anti-enumeration SMS (unknown-account, account-already-exists) are now gated too: if the submitted phone has no recorded consent, the backend **silently sends nothing and raises no error** (this preserves allauth's anti-enumeration guarantee — the response looks identical whether or not an SMS was actually sent). There is no user-visible signal for this case; see §3c below for what that means for the frontend.
- Practically, this means: **any flow that submits a phone number the user hasn't consented for yet (login-by-phone with a new number, changing your phone number) must record consent for that specific phone via `POST /consents/` with `phone_number` before requesting a verification code.**

All paths below are relative to the API root. Policy-document endpoints are mounted at the root (e.g. `https://<api-host>/policy-documents/…`); allauth headless auth is under `/auth/…`. Policy `body_markdown` is **raw markdown** — render it client-side with your existing markdown renderer (sanitize output).

Document types (the `document_type` enum) — used everywhere below:

| value | meaning |
|---|---|
| `privacy_policy` | Privacy Policy |
| `terms_of_use` | Terms of Use |
| `sms_consent` | SMS messaging consent (the one that gates phone verification) |

---

## 1. Privacy Policy page

Fetch and render the latest published Privacy Policy.

**Request** (public — no auth required):
```
GET /policy-documents/latest/privacy_policy/
```

**Response `200`:**
```json
{
  "id": 12,
  "document_type": "privacy_policy",
  "version": 3,
  "title": "Privacy Policy",
  "body_markdown": "# Privacy Policy\n\n...markdown...",
  "published_at": "2026-07-01T12:00:00Z"
}
```

- Render `title` as the heading and `body_markdown` as markdown. Optionally show `version` / `published_at` ("Last updated …").
- **`404`** means no Privacy Policy has been published yet — show an empty/placeholder state, not an error toast.
- Public endpoint, so it works before the user has a session (needed during signup).

## 2. Terms of Use page

Identical shape, different type:

```
GET /policy-documents/latest/terms_of_use/
```

Same `200` body (with `document_type: "terms_of_use"`), same `404` handling.

### Other read endpoints (available, not required for the three surfaces)
- `GET /policy-documents/latest/` — **public**. Returns a JSON **array** with the latest version of *each* document type (one object per type). Handy to fetch all three at once.
- `GET /policy-documents/{id}/` — **authenticated**. A specific version by id.
- `GET /policy-documents/` — **authenticated**. Full version history (paginated, newest first). Optional `?document_type=privacy_policy` filter. An invalid `document_type` value returns `400`.

---

## 3. Consent request in the signup flow

The backend **will refuse to send a phone-verification SMS** to any user without a recorded `sms_consent` (see "The consent gate" below). So consent must be collected during onboarding. There are two signup paths, handled differently.

Before rendering the consent UI, fetch the policy text to link/show (endpoints in §1/§2, or the `latest/` list). Present links to the Privacy Policy and Terms of Use plus the SMS-messaging consent acknowledgement.

### 3a. Email / password signup

The headless signup form now has a **new required field `accepted_policies`** (boolean). It represents acceptance of the Privacy Policy, Terms of Use, and SMS messaging consent together.

- Add a **required** checkbox to the signup form, e.g. "I agree to the Privacy Policy, Terms of Use, and to receive SMS messages." Link the first two to the pages in §1/§2.
- Include `accepted_policies: true` in the existing headless signup POST body (alongside `email`, `password`, `phone`, `first_name`, `last_name`, and optional `organization_name`).

```
POST /auth/browser/v1/auth/signup     (or /auth/app/v1/auth/signup for the app client)
Content-Type: application/json

{
  "email": "user@example.com",
  "password": "…",
  "phone": "+15551234567",
  "first_name": "…",
  "last_name": "…",
  "accepted_policies": true
}
```

- If `accepted_policies` is missing or `false`, signup is rejected with a validation error on that field — surface it inline on the checkbox.
- On success, the backend records `sms_consent` (plus `privacy_policy` / `terms_of_use` if those are published) automatically — the frontend does **not** need a separate consent call on this path.

### 3b. OAuth2 / social signup (Google, Apple)

Social signup auto-creates the account and does **not** go through the signup form, so no consent is captured during the OAuth handshake. Collect it in a **post-signup step**, before triggering phone verification.

After the social login completes and the user has a session, show a consent step, then POST once (or per document type) to:

```
POST /consents/            (authenticated — session/JWT required)
Content-Type: application/json

{ "document_type": "sms_consent", "phone_number": "+15551234567" }
```

`phone_number` is **optional** on this endpoint but **required in practice for `sms_consent`** — it's what the phone-keyed gate checks (see the Phase 8 note above and §3c). Omit it (or send `""`) for `privacy_policy` / `terms_of_use`, which aren't phone-gated. When recording `sms_consent` for a phone the user is about to verify, always include that phone's `phone_number`.

**Response `201`:**
```json
{
  "id": 44,
  "document_type": "sms_consent",
  "policy_document": 12,
  "policy_document_version": 2,
  "source": "api",
  "accepted_at": "2026-07-04T09:30:00Z",
  "ip_address": "…",
  "user_agent": "…",
  "phone_number": "+15551234567"
}
```

- Send at least `sms_consent` (with `phone_number`). To record all three, POST once per type (`privacy_policy`, `terms_of_use`, `sms_consent`). Only `sms_consent` gates SMS; the others are recorded for completeness.
- Errors: `401` if unauthenticated; `400` if `document_type` is unknown **or** that document type has no published version yet (i.e. an admin hasn't authored it). If you get `400` for `sms_consent`, phone verification will not be possible — surface a clear message.
- The user/IP/user-agent are captured server-side from the request — do **not** send them.

### 3c. Login-by-phone and change-phone (any flow submitting a new phone)

Consent is **phone-keyed** (Phase 8): the verification-code gate checks whether *the requesting user* has a recorded `sms_consent` for *that specific phone*. This affects any flow where the user submits a phone number that wasn't captured at signup:

- **Logging in with a phone number** (`LOGIN_METHODS` includes `phone`), for a phone that has never had consent recorded.
- **Changing your phone number** to a new one after signup.
- **OAuth users adding a phone** for the first time (in addition to the OAuth consent step in §3b, which records consent with whatever phone the user has at that point — if that's blank or changes later, the new phone needs its own consent record).

Before requesting a verification code (or attempting phone-based login/signup) for a phone number that hasn't been consented yet, call:

```
POST /consents/            (authenticated)
Content-Type: application/json

{ "document_type": "sms_consent", "phone_number": "+15559876543" }
```

**If you skip this and the phone has no consent on file:**
- A **verification-code request** (`send_verification_code_sms`) for that phone returns the `403 consent_required` error described below — handle it the same way as §3a/§3b.
- The backend's **anti-enumeration SMS** (login-by-phone against an unknown phone; signup against a phone that already has an account) are consent-gated at the phone-owner level: a **login-by-phone attempt against a phone with no account never sends any SMS** (there is no owning account to have consented), and an **account-already-exists** message is sent only when the phone's *owning account* previously consented. Either way the backend **silently sends nothing** when consent is absent (no error, no distinguishable response — this preserves allauth's uniform anti-enumeration response). The frontend therefore **cannot rely on "the request succeeded" as a signal that an SMS was sent** for these enumeration paths; don't build a "check your phone" UI that assumes delivery for a phone the user hasn't verified through this app's own flows.

### The consent gate (why the above matters)

When phone verification is enabled, any attempt to send a verification SMS to a phone without a recorded `sms_consent` **for that phone** is refused server-side with:

```
HTTP 403
{ "status": 403, "errors": [ { "code": "consent_required", "message": "SMS consent is required before a verification code can be sent." } ] }
```

Detect `errors[].code === "consent_required"` on phone-verification requests and route the user back to the consent step (§3a checkbox / §3b or §3c `POST /consents/` with the relevant `phone_number`) instead of showing a generic error. This can surface from the signup phone-verification stage, a resend, a login-by-phone attempt, or an authenticated change-phone request.

> Note: phone verification itself is currently **disabled** on the backend (`ACCOUNT_PHONE_VERIFICATION_ENABLED = False`, pending Twilio approval — Phase 6). The consent capture + gate are already live and safe to build against now; the gate becomes active when the setting is flipped.

---

## Quick reference

| Surface | Method | Path | Auth | Notes |
|---|---|---|---|---|
| Privacy page | GET | `/policy-documents/latest/privacy_policy/` | public | `404` = not published |
| Terms page | GET | `/policy-documents/latest/terms_of_use/` | public | `404` = not published |
| All latest | GET | `/policy-documents/latest/` | public | array, one per type |
| History | GET | `/policy-documents/?document_type=…` | auth | paginated, newest first |
| Email signup consent | POST | `/auth/browser/v1/auth/signup` | public | add required `accepted_policies: true` |
| OAuth consent step | POST | `/consents/` | auth | `{ "document_type": "sms_consent", "phone_number": "…" }` |
| New/changed phone consent | POST | `/consents/` | auth | same as above — record **before** requesting a code for that phone |
| Gate refusal (has a user) | — | (verification-code requests) | — | `403` `code: consent_required` → route to consent step |
| Gate refusal (no user, anti-enumeration) | — | (login-by-phone / signup with existing phone) | — | silent no-op, no distinguishable error — don't assume SMS delivery for unconsented phones |
