# SMS MFA Consent — Frontend Handoff

Backend for the SMS-consent feature (plan `2026-07-03-SMS_MFA_CONSENT_IMPLEMENTATION_PLAN.md`, Phases 1–5) is merged. This document tells the frontend what to build against it: the **Privacy Policy page**, the **Terms of Use page**, and the **consent request in the signup flow** (email + OAuth2).

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

{ "document_type": "sms_consent" }
```

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
  "user_agent": "…"
}
```

- Send at least `sms_consent`. To record all three, POST once per type (`privacy_policy`, `terms_of_use`, `sms_consent`). Only `sms_consent` gates SMS; the others are recorded for completeness.
- Errors: `401` if unauthenticated; `400` if `document_type` is unknown **or** that document type has no published version yet (i.e. an admin hasn't authored it). If you get `400` for `sms_consent`, phone verification will not be possible — surface a clear message.
- The user/IP/user-agent are captured server-side from the request — do **not** send them.

### The consent gate (why the above matters)

When phone verification is enabled, any attempt to send a verification SMS without a recorded `sms_consent` is refused server-side with:

```
HTTP 403
{ "status": 403, "errors": [ { "code": "consent_required", "message": "SMS consent is required before a verification code can be sent." } ] }
```

Detect `errors[].code === "consent_required"` on phone-verification requests and route the user back to the consent step (§3a checkbox / §3b `POST /consents/`) instead of showing a generic error. This can surface from the signup phone-verification stage, a resend, or an authenticated change-phone request.

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
| OAuth consent step | POST | `/consents/` | auth | `{ "document_type": "sms_consent" }` |
| Gate refusal | — | (phone-verify requests) | — | `403` `code: consent_required` → route to consent step |
