# Microsoft room sync setup

Vinta Schedule creates, edits and deletes meeting rooms in a customer's Microsoft 365
tenant through **Vinta's own multi-tenant Entra app**. A Global Administrator of the
customer's tenant grants that app admin consent once. After that, Vinta Schedule gets
app-only tokens for the tenant through the OAuth client-credentials flow. Nothing
acts as a Microsoft user, and the only thing Vinta stores per customer is the
directory (tenant) id.

The setup has two halves:

1. **Vinta ops** registers the Entra app once per environment and gives Vinta
   Schedule its credentials.
2. **Customer IT** grants admin consent and assigns two Exchange roles.

Everything below is behind the `resource_calendar_provider_sync` feature flag. While
the flag is off for an organization, its consent and verify endpoints answer 404.

## Part 1 — Vinta ops: register the Entra app

Do this once for each environment (staging, production) in Vinta's own Microsoft
Entra tenant.

1. In the [Entra admin center](https://entra.microsoft.com), go to **App
   registrations → New registration**.
   - **Supported account types:** *Accounts in any organizational directory
     (Any Microsoft Entra ID tenant — Multitenant)*.
   - **Redirect URI:** platform **Web**, value
     `https://<API domain>/calendar/microsoft-connection/callback/`, for example
     `https://api.staging.example.com/calendar/microsoft-connection/callback/`.
     It must match exactly, including the trailing slash. Vinta Schedule builds it
     from the host the consent-url request arrived on.
2. Under **API permissions → Add a permission → Microsoft Graph → Application
   permissions**, add:
   - `Place.ReadWrite.All` — create, edit and delete rooms, and read buildings and
     floors.
   - `Calendars.Read` — read room calendars and free/busy.

   Do not grant admin consent in Vinta's own tenant for customers; each customer
   grants it in theirs.
3. Under **Certificates & secrets → Client secrets**, create a secret. Note its
   expiry date and set a reminder to rotate it before then: an expired secret stops
   every customer's room sync at once.
4. Give Vinta Schedule the credentials:
   - `MS_CLIENT_ID` — the **Application (client) id**. It is config, not a secret:
     set the Terraform input `ms_client_id` for the environment (it becomes the
     `MS_CLIENT_ID` container variable).
   - `MS_CLIENT_SECRET` — the secret **value**. Put it in the environment's Secrets
     Manager secret under the key `MS_CLIENT_SECRET`. On an environment whose secret
     predates this key, run `infrastructure/scripts/sync-app-secret-keys.sh` (dry run,
     then `--apply`) **before** the deploy that ships it, then fill in the value.
     See `infrastructure/README.md`.

The same app is also what the Outlook calendar integration uses, so an environment
that already has `MS_CLIENT_ID` / `MS_CLIENT_SECRET` set only needs steps 1–2
checked: the redirect URI and the two application permissions.

With either value empty, the consent and verify endpoints answer 503 and Vinta
Schedule never calls Microsoft.

## Part 2 — Customer IT: connect the tenant

An org admin of the customer's organization in Vinta Schedule starts this, and a
**Global Administrator** (or Privileged Role Administrator) of their Microsoft 365
tenant finishes it.

### 1. Grant admin consent

1. In Vinta Schedule, an org admin requests the consent link
   (`POST /calendar/microsoft-connection/consent-url/`, or the button in the web app).
2. They send the link to their Global Administrator. The link works **once**, for
   **one hour**, and only for that organization. Requesting a new link cancels the
   previous one. Do not forward the link outside your company: whoever accepts it
   connects their tenant to your Vinta Schedule organization.
3. The Global Administrator opens the link, signs in to the customer's tenant, reviews
   the two permissions and selects **Accept**.
4. Microsoft sends the browser back to Vinta Schedule, which stores the tenant id and
   sends the browser on to the web app with the outcome
   (`/settings/integrations/microsoft?status=connected`, or `status=error` with
   `reason=invalid_state` or `reason=consent_denied`).

### 2. Assign the Exchange roles

Admin consent alone is not enough to write rooms. Room mailboxes live in Exchange
Online, and Exchange checks its own role-based access control (RBAC). An **Exchange
administrator** of the customer's tenant assigns these management roles to the Vinta
Schedule app's service principal:

- `TenantPlacesManagement` — manage rooms and their places metadata.
- `MailRecipient` — create and update the room mailboxes behind those rooms.

In Exchange Online PowerShell (`Connect-ExchangeOnline`):

```powershell
# The Vinta Schedule enterprise application in YOUR tenant
# (Entra admin center -> Enterprise applications -> Vinta Schedule).
$appId    = "<Application (client) id from Vinta>"
$objectId = "<Object id of the Vinta Schedule enterprise application in your tenant>"

New-ServicePrincipal -AppId $appId -ObjectId $objectId -DisplayName "Vinta Schedule"

New-ManagementRoleAssignment -App $appId -Role "TenantPlacesManagement"
New-ManagementRoleAssignment -App $appId -Role "MailRecipient"
```

Exchange can take up to an hour to apply a new role assignment.

> These commands have not yet been run against a sandbox tenant. Confirm them during
> the staging end-to-end test (see the Phase 0 spike, `resource-directory-spike.md`),
> and update this section with what worked.

### 3. Verify

An org admin runs the check (`POST /calendar/microsoft-connection/verify/`, or the
button in the web app). It:

1. gets an app-only token for the tenant;
2. checks that the token carries both `Place.ReadWrite.All` and `Calendars.Read`;
3. lists one building from Microsoft Places.

Only when all three pass does Vinta Schedule mark the connection as write-enabled.
Otherwise the response, and the connection, carry a message saying what to fix.

The check cannot see Exchange RBAC: a missing role assignment from step 2 passes
verification and shows up later as a room sync failure that names the missing role.
Run the verification again after any change to consent or permissions.

## Troubleshooting

| What you see | What to do |
|---|---|
| Consent page says the redirect URI does not match | Vinta ops: the app registration's redirect URI must equal `https://<API domain>/calendar/microsoft-connection/callback/` exactly. |
| `reason=invalid_state` after accepting | The link was used before, is older than an hour, or a newer link was requested. Request a new link. |
| `reason=consent_denied` | The administrator canceled, or was not allowed to consent. A Global Administrator must accept. |
| Verify says a permission is missing | Open a new consent link and accept again; Microsoft asks only for what is missing. |
| Verify says Vinta could not get a token | Consent was revoked, or the tenant removed the enterprise app. Grant consent again. |
| Verify passes but room writes fail | Assign the Exchange roles in step 2, wait up to an hour, then retry the sync. |
