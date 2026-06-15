# Google Service Account Rooms Sync Fix — Implementation Plan

## 1. Goals

1. Replace the broken JWT-based credential flow in `GoogleCalendarAdapter` with `google.oauth2.service_account.Credentials` and domain-wide delegation (DWD), so the rooms sync task no longer fails with "credentials do not contain the necessary fields".
2. Rename `GoogleCalendarServiceAccount.audience` → `admin_email` so the field stores the Google Workspace admin email used as the DWD impersonation subject, and matches its actual purpose.
3. Replace `get_calendar_resources()` (which used `calendarList().list()` on the SA — always empty without manual setup) with a Google Admin SDK `resources().calendars().list()` call that discovers all resource calendars in the Workspace domain.
4. After this plan ships, triggering the `/organizations/{id}/sync-rooms/` endpoint will successfully import rooms as `CalendarType.RESOURCE` calendars.

**Non-goals:**
- Backfill script to fix existing rooms created as PERSONAL (manual retrigger per org via sync-rooms endpoint is sufficient).
- Removing `public_key` from `GoogleCalendarServiceAccount` (harmless; separate cleanup if desired).
- Changes to the MS Outlook adapter or non-SA Google OAuth path.
- Adding a feature flag (rooms sync has never worked; there is no pre-existing behavior to preserve).

---

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Auth class** | Use `google.oauth2.service_account.Credentials.from_service_account_info()` + `.with_subject(admin_email)`. The old `_generate_jwt` + OAuth `Credentials` approach generates a JWT but uses it directly as a bearer token; Google returns 401 and the client's auto-refresh fails because `refresh_token` is None. |
| **DWD subject** | `admin_email` (formerly `audience`). A Google Workspace super-admin email is required; the SA must be granted DWD in the Google Admin Console with the two scopes below. |
| **Scopes** | `https://www.googleapis.com/auth/admin.directory.resource.calendar.readonly` (Admin SDK) + `https://www.googleapis.com/auth/calendar.readonly` (Calendar API freebusy). Both granted to the SA at the domain level. |
| **Two API clients on SA adapter** | `self.client` = Calendar API (freebusy, event sync — same as today). `self.admin_client` = Admin SDK `admin/directory_v1` (resource listing — new). Both built with the same DWD credentials. |
| **Bypass `__init__`** | The new `GoogleCalendarAdapter.from_service_account()` classmethod allocates the instance via `cls.__new__(cls)` and sets `account_id`, `client`, `admin_client` directly. `__init__` is OAuth-user-only and must not be called from the SA path. |
| **Resource listing API** | `admin_client.resources().calendars().list(customer="my_customer")` discovers all resource calendars in the domain without requiring rooms to be manually added to the SA's calendar list. |
| **`audience` rename** | Clean rename via `RenameField` migration + serializer + view + TypedDict. API field changes from `audience` to `admin_email`. Frontend must update the field name it sends in PATCH. |
| **No feature flag** | The rooms sync has never produced correct output (all attempts fail with the credential error). There is no working behavior to gate off. |

---

## 3. Data Model Changes

### 3.1 `GoogleCalendarServiceAccount.audience` → `admin_email`

File: [calendar_integration/models.py](../calendar_integration/models.py)

```python
# Before
audience = models.CharField(max_length=255, blank=True)

# After
admin_email = models.EmailField(
    blank=True,
    help_text=(
        "Google Workspace super-admin email used as the DWD impersonation subject. "
        "The service account must have domain-wide delegation granted for the Admin SDK "
        "and Calendar API scopes in the Google Admin Console."
    ),
)
```

Migration: `RenameField(model_name="googlecalendarserviceaccount", old_name="audience", new_name="admin_email")`.

### 3.2 `GoogleServiceAccountCredentialsTypedDict`

File: [calendar_integration/services/calendar_adapters/google_calendar_adapter.py](../calendar_integration/services/calendar_adapters/google_calendar_adapter.py)

```python
# Before
class GoogleServiceAccountCredentialsTypedDict(TypedDict):
    account_id: str
    email: str
    audience: str      # ← broken JWT aud claim, unused after fix
    public_key: str
    private_key_id: str
    private_key: str

# After
class GoogleServiceAccountCredentialsTypedDict(TypedDict):
    account_id: str
    email: str         # service account client_email
    admin_email: str   # DWD impersonation subject
    public_key: str    # kept for API compat; not used by new auth
    private_key_id: str
    private_key: str
```

---

## 4. API Design

### 4.1 `PATCH /organizations/{id}/` — service account write serializer

File: [organizations/serializers.py](../organizations/serializers.py)

```python
# Before (GoogleServiceAccountWriteSerializer)
class GoogleServiceAccountWriteSerializer(serializers.Serializer):
    email = serializers.EmailField()
    audience = serializers.CharField(max_length=255, allow_blank=True)
    ...

# After
class GoogleServiceAccountWriteSerializer(serializers.Serializer):
    email = serializers.EmailField()
    admin_email = serializers.EmailField(allow_blank=True)
    ...
```

Read serializer (`GoogleServiceAccountReadSerializer`) also renames `audience` → `admin_email`.

No version bump — the change is in the SA sub-object only and the feature is non-functional today, so no clients depend on the old field name.

---

## 5. Phased Rollout

### Phase 1 — Rename `audience` → `admin_email` in model, API, and service layer

**Goal**: The `GoogleCalendarServiceAccount` model, REST serializer, view, and service-layer TypedDict all use `admin_email` consistently. No auth behaviour changes yet.

Changes:
1. [calendar_integration/models.py](../calendar_integration/models.py): rename `audience` field to `admin_email` (EmailField + help_text).
2. New migration `calendar_integration/migrations/NNNN_rename_audience_admin_email.py`: `RenameField(model_name="googlecalendarserviceaccount", old_name="audience", new_name="admin_email")`.
3. [organizations/serializers.py](../organizations/serializers.py): rename `audience` → `admin_email` in both `GoogleServiceAccountWriteSerializer` and `GoogleServiceAccountReadSerializer`. Update docstring.
4. [organizations/views.py](../organizations/views.py): `sa_data["audience"]` → `sa_data["admin_email"]` in the upsert block (line ~163).
5. [calendar_integration/services/calendar_adapters/google_calendar_adapter.py](../calendar_integration/services/calendar_adapters/google_calendar_adapter.py): rename `audience: str` → `admin_email: str` in `GoogleServiceAccountCredentialsTypedDict`.
6. [calendar_integration/services/calendar_service.py](../calendar_integration/services/calendar_service.py): update the credentials dict built from `account` in `get_calendar_adapter_for_account` — `"audience": account.audience` → `"admin_email": account.admin_email`.
7. Tests: update `_SA_PAYLOAD` fixture and all `audience` assertions in [organizations/tests/test_views.py](../organizations/tests/test_views.py) and [calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py](../calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py).

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Integration**: [organizations/tests/test_views.py](../organizations/tests/test_views.py) — `TestPhase18ServiceAccountConfig`: update `_SA_PAYLOAD["audience"]` → `_SA_PAYLOAD["admin_email"]`; verify `admin_email` in response and DB row.
- **Unit**: [calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py](../calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py) — update `service_account_credentials` fixture.

**Suggested AI model**: Tier 1 — `claude-haiku-4-5` / `gpt-5-nano` / `gemini-2.5-flash-lite`. Mechanical rename across ~6 files; every change has an exact existing pattern to follow.

**Reusable skills**: `add-migration` (for the `RenameField` migration).

Acceptance: `python manage.py migrate` runs clean; `PATCH /organizations/{id}/` with `{"google_service_account": {"admin_email": "admin@example.com", ...}}` returns 200 with `admin_email` in the response body and DB row stores the value.

---

### Phase 2 — Replace broken JWT auth with `google.oauth2.service_account.Credentials`

**Goal**: `GoogleCalendarAdapter.from_service_account()` authenticates correctly via domain-wide delegation; the rooms sync task no longer errors with the "missing fields" credential error.

Changes:
1. [calendar_integration/services/calendar_adapters/google_calendar_adapter.py](../calendar_integration/services/calendar_adapters/google_calendar_adapter.py):
   - Add import: `from google.oauth2 import service_account as google_service_account`.
   - Define constant at module level:
     ```python
     _SA_SCOPES = [
         "https://www.googleapis.com/auth/admin.directory.resource.calendar.readonly",
         "https://www.googleapis.com/auth/calendar.readonly",
     ]
     ```
   - Add classmethod `from_service_account(cls, credentials: GoogleServiceAccountCredentialsTypedDict) -> "GoogleCalendarAdapter"`:
     ```python
     @classmethod
     def from_service_account(
         cls, credentials: GoogleServiceAccountCredentialsTypedDict
     ) -> "GoogleCalendarAdapter":
         sa_creds = google_service_account.Credentials.from_service_account_info(
             {
                 "type": "service_account",
                 "private_key_id": credentials["private_key_id"],
                 "private_key": credentials["private_key"],
                 "client_email": credentials["email"],
                 "token_uri": "https://oauth2.googleapis.com/token",
             },
             scopes=_SA_SCOPES,
         ).with_subject(credentials["admin_email"])
         adapter = cls.__new__(cls)
         adapter.account_id = f"service-{credentials['account_id']}"
         adapter.client = build("calendar", "v3", credentials=sa_creds)
         adapter.admin_client = build("admin", "directory_v1", credentials=sa_creds)
         return adapter
     ```
   - Remove `_generate_jwt` classmethod.
   - Remove `from_service_account_credentials` classmethod.

2. [calendar_integration/services/calendar_service.py](../calendar_integration/services/calendar_service.py): in `get_calendar_adapter_for_account`, update the `isinstance(account, GoogleCalendarServiceAccount)` branch to call `GoogleCalendarAdapter.from_service_account({...})` instead of `GoogleCalendarAdapter.from_service_account_credentials({...})`.

3. [calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py](../calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py):
   - Remove `test_generate_jwt` and `test_from_service_account_credentials` tests.
   - Add `test_from_service_account_builds_both_clients` that mocks `google_service_account.Credentials.from_service_account_info`, asserts `adapter.client` and `adapter.admin_client` are both built, and `adapter.account_id` starts with `"service-"`.
   - Add `test_from_service_account_calls_with_subject` that asserts `with_subject` is called with `credentials["admin_email"]`.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `test_from_service_account_builds_both_clients`, `test_from_service_account_calls_with_subject` in [calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py](../calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py).
- **Integration**: [calendar_integration/tests/services/test_calendar_service.py](../calendar_integration/tests/services/test_calendar_service.py) — update `test_execute_organization_calendar_resources_import_calls_adapter` to mock `GoogleCalendarAdapter.from_service_account` instead of the old classmethod.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6`. Multi-file orchestration; new classmethod must correctly wire `with_subject`, bypass `__init__`, and build two API clients. Removal of the old JWT tests + writing new unit tests for the credential building logic needs care.

**Reusable skills**: none — pure adapter code.

Acceptance: running `pytest calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py -k from_service_account` passes. `CalendarService.get_calendar_adapter_for_account` with a `GoogleCalendarServiceAccount` instance returns a `GoogleCalendarAdapter` with both `client` and `admin_client` set.

---

### Phase 3 — Use Admin SDK to list Workspace resource calendars

**Goal**: `get_calendar_resources()` returns all Google Workspace resource calendars via the Admin SDK directory API instead of the SA's (always-empty) personal calendar list.

Changes:
1. [calendar_integration/services/calendar_adapters/google_calendar_adapter.py](../calendar_integration/services/calendar_adapters/google_calendar_adapter.py):
   - Rewrite `get_calendar_resources()`:
     ```python
     def get_calendar_resources(self) -> Iterable[CalendarResourceData]:
         if not hasattr(self, "admin_client"):
             raise NotImplementedError(
                 "get_calendar_resources requires a service-account adapter with admin_client."
             )
         read_quote_limiter.try_acquire(f"google_calendar_read_{self.account_id}")
         result = (
             self.admin_client.resources()
             .calendars()
             .list(customer="my_customer", maxResults=500)
             .execute()
         )
         for resource in result.get("items", []):
             yield CalendarResourceData(
                 external_id=resource["resourceId"],
                 name=resource["resourceName"],
                 description=resource.get("resourceDescription", ""),
                 email=resource.get("resourceEmail", ""),
                 capacity=resource.get("capacity", 0),
                 original_payload=resource,
                 provider=self.provider,
             )
     ```
   - Rewrite `get_calendar_resource(resource_id)` to use `self.admin_client.resources().calendars().get(customer="my_customer", calendarResourceId=resource_id)`.
   - Handle pagination: Admin SDK returns `nextPageToken`; add a `while` loop in `get_calendar_resources()` to exhaust pages.

2. Tests: [calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py](../calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py):
   - Add `test_get_calendar_resources_uses_admin_sdk` — mock `adapter.admin_client`, assert `resources().calendars().list()` called with `customer="my_customer"`, verify returned `CalendarResourceData` fields map correctly from Admin SDK field names (`resourceId`, `resourceName`, `resourceEmail`, etc.).
   - Add `test_get_calendar_resources_paginates` — mock two pages via `nextPageToken`, assert all items returned.
   - Add `test_get_calendar_resources_raises_without_admin_client` — assert `NotImplementedError` when `admin_client` absent (non-SA adapters).

Spec use-case: rooms sync produces correct resource calendars.

Tests:
- **Unit**: three tests above in [calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py](../calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py).
- **Integration**: [calendar_integration/tests/services/test_calendar_service.py](../calendar_integration/tests/services/test_calendar_service.py) — update `test_execute_organization_calendar_resources_import_calls_adapter` to mock `adapter.get_available_calendar_resources` returning Admin-SDK-shaped resources.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6`. Admin SDK response shape differs from `calendarList` (field names `resourceId`/`resourceName`/`resourceEmail` vs `id`/`summary`/`email`); pagination loop; guard for non-SA adapters.

**Reusable skills**: none.

Acceptance: `pytest calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py -k get_calendar_resources` passes. In a real Workspace environment, `get_available_calendar_resources()` returns at least one `CalendarResourceData` entry whose `email` ends in `@resource.calendar.google.com`.

---

## 6. Risk & Rollout Notes

- **No migration lock risk**: `RenameField` on `googlecalendarserviceaccount` is a low-traffic table (one row per org). Postgres renames a column in a single catalog update — no table rewrite, no lock contention.
- **DWD must be configured before sync works**: after Phase 2 ships, admins must ensure the SA has DWD granted in Google Admin Console (`admin.googleapis.com` → Security → API Controls → Domain-wide delegation) with both scopes. Without DWD the SDK call will return a 403 (`insufficient authentication scopes`).
- **Admin SDK requires `googleapiclient`**: already a dep (`google-api-python-client`); `build("admin", "directory_v1", ...)` works out of the box.
- **`my_customer` resolves correctly**: when the SA impersonates a Workspace admin, `"my_customer"` resolves to that admin's customer ID. No hardcoded customer ID needed.
- **`public_key` field**: still accepted by the write serializer and stored. It is never read by the new auth path. Can be removed in a future cleanup PR.
- **Rollback**: all three phases are independently reversible. Phase 1 renames a column on a dead feature — safe to revert. Phase 2 + 3 change adapter internals — rolling back to the old code restores the previous (broken) state.
- **Existing PERSONAL rooms**: admins must manually trigger `/organizations/{id}/sync-rooms/` after Phase 3 ships; the `update_or_create` fix (already merged) will correct `calendar_type` for pre-existing calendars on re-sync.

---

## 7. Open Questions

| Question | Recommended default |
|---|---|
| Does the Workspace have DWD configured for the existing SA? | Unknown. Ops/admin must verify and grant scopes after Phase 2 ships. |
| Should `admin_email` be required (non-blank) in the write serializer? | Keep `allow_blank=True` for now so existing stored SAs don't fail validation on unrelated PATCHes; add required validation in a follow-up if desired. |
| Pagination max results per page? | 500 (Admin SDK cap); `maxResults=500` + pagination loop covers all edge cases. |

---

## 8. Touch List

### Phase 1
- [calendar_integration/models.py](../calendar_integration/models.py) — rename `audience` → `admin_email`
- `calendar_integration/migrations/NNNN_rename_audience_admin_email.py` — new file
- [organizations/serializers.py](../organizations/serializers.py) — rename field in both serializers
- [organizations/views.py](../organizations/views.py) — `sa_data["audience"]` → `sa_data["admin_email"]`
- [calendar_integration/services/calendar_adapters/google_calendar_adapter.py](../calendar_integration/services/calendar_adapters/google_calendar_adapter.py) — rename in `GoogleServiceAccountCredentialsTypedDict`
- [calendar_integration/services/calendar_service.py](../calendar_integration/services/calendar_service.py) — update credentials dict key
- [organizations/tests/test_views.py](../organizations/tests/test_views.py) — update `_SA_PAYLOAD` + assertions
- [calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py](../calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py) — update fixture

### Phase 2
- [calendar_integration/services/calendar_adapters/google_calendar_adapter.py](../calendar_integration/services/calendar_adapters/google_calendar_adapter.py) — add `from_service_account`, remove `_generate_jwt` + `from_service_account_credentials`, add `_SA_SCOPES`
- [calendar_integration/services/calendar_service.py](../calendar_integration/services/calendar_service.py) — update `get_calendar_adapter_for_account` SA branch
- [calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py](../calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py) — remove old SA tests, add new
- [calendar_integration/tests/services/test_calendar_service.py](../calendar_integration/tests/services/test_calendar_service.py) — update import task test

### Phase 3
- [calendar_integration/services/calendar_adapters/google_calendar_adapter.py](../calendar_integration/services/calendar_adapters/google_calendar_adapter.py) — rewrite `get_calendar_resources()` + `get_calendar_resource()`
- [calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py](../calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py) — add 3 Admin SDK tests
- [calendar_integration/tests/services/test_calendar_service.py](../calendar_integration/tests/services/test_calendar_service.py) — update integration test
