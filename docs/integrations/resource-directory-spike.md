# Resource directory spike

Before Phases 4 and 6 of the
[resource calendar provider sync plan](../../ai-plans/2026-10-04-RESOURCE_CALENDAR_PROVIDER_SYNC_IMPLEMENTATION_PLAN.md)
reach staging, we need to know how the Google and Microsoft room directories behave
when we write to them. The script `scripts/spikes/resource_directory_spike.py` creates,
edits and deletes throwaway rooms in a **sandbox** tenant and prints what it saw as
JSON. A person runs it, then fills in the [findings table](#findings) below.

The script is not part of the app. Django does not import it, and it needs no `.env`.

## The questions

| # | Question | Source | Subcommand |
|---|---|---|---|
| Q1 | Does deleting a room through Microsoft Graph remove the room mailbox, or only the directory entry? | Spec open question 1 | `ms-create-delete` |
| Q2 | Where does "description" live on a Microsoft room? Is any writable property free text? | Spec open question 2 | `ms-description` |
| Q8a | Does Google require `buildingId` for a `CONFERENCE_ROOM`? | Spec open question 8 | `google-create-delete` |
| Q8b | Does Google reject a second room with the same name? | Spec open question 8 | `google-create-delete` |

The same runs also check assumptions the plan relies on:

| Assumption | Plan section | Subcommand |
|---|---|---|
| A replayed Google insert with the same client-chosen `resourceId` fails with 409, so it can be treated as success. | Guiding Decisions → *Idempotent provider create* | `google-create-delete` |
| A Microsoft tag (`vinta-link-<uuid>`) round-trips, survives a PATCH, and can be found with a `$filter`. | Guiding Decisions → *Idempotent provider create*; Risks → *Duplicate provider rooms on replay* | `ms-create-delete` |
| How long a new Microsoft room takes to get an email address and a working mailbox. | Spec Risks → provisioning delay | `ms-create-delete` |
| App-only `getSchedule` works with only `Calendars.Read`. | Plan open question 4 | `ms-create-delete` |
| The token's `roles` claim lists the granted application permissions. | Guiding Decisions → *Write-enabled check* | `ms-create-delete` |

## Safety

- **Dry-run by default.** Without `--execute` the script sends nothing: no token
  request and no API call. It logs each request it *would* send and answers it with a
  canned response, so a dry run shows the full sequence of calls. The findings of a
  dry run are placeholders and mean nothing.
- **Sandbox tenants only.** The script creates and deletes rooms. Never point it at a
  customer tenant.
- **Cleanup.** Every room the script creates is deleted at the end, even when a step
  fails. If a cleanup call itself fails, the log says `delete it by hand` with the
  room's id. Search the sandbox for rooms named `Vinta spike ...` to find leftovers.
- **Logs.** Logs and the JSON summary hold only opaque ids, HTTP status codes,
  provider error *codes* (never the error message, which can echo request values),
  timings and property *names*. Room emails appear only as a short SHA-256 hash.
- Logs go to stderr and the JSON summary to stdout, so `> findings.json` captures only
  the summary.

## Running it

```bash
uv run python scripts/spikes/resource_directory_spike.py --help
uv run python scripts/spikes/resource_directory_spike.py <subcommand> --help
```

Run each subcommand once without `--execute` to see the plan, then again with it.

### `google-create-delete` (Q8a, Q8b)

Needs a service account with domain-wide delegation for the **write** scope
`https://www.googleapis.com/auth/admin.directory.resource.calendar` (the app today only
holds the `.readonly` one), and a super-admin to impersonate.

| Variable | Value |
|---|---|
| `SPIKE_GOOGLE_SERVICE_ACCOUNT_JSON` | Path to the service-account key file (JSON). |
| `SPIKE_GOOGLE_ADMIN_EMAIL` | Admin user the service account acts as. |
| `SPIKE_GOOGLE_CUSTOMER` | Optional. Defaults to `my_customer`. |

```bash
uv run python scripts/spikes/resource_directory_spike.py google-create-delete \
  [--building-id <id>] --execute > google-findings.json
```

Without `--building-id` the script uses the first building `buildings.list` returns.
The tenant needs at least one building.

Steps: list buildings → insert a room **without** a building → insert a room with
`resourceId=vinta-spike-<hex>` and a building → replay the same insert → read it back
→ PATCH capacity 4 → 8 and read back → insert a second room with the **same name** →
delete the first room → read it again (expect 404) → clean up.

### `ms-create-delete` (Q1)

Needs an Entra app registration in the sandbox tenant with **application** permissions
`Place.ReadWrite.All` and `Calendars.Read`, with admin consent and a client secret.
These are the two permissions the plan grants the production app. To read the mailbox
user after the delete, `GET /users/{email}` also needs `User.Read.All`. Without it, that
call returns 403 and the `mailbox_calendar_status_after_delete` finding answers Q1 on
its own.

| Variable | Value |
|---|---|
| `SPIKE_MS_TENANT_ID` | Directory (tenant) id. |
| `SPIKE_MS_CLIENT_ID` | Application (client) id. |
| `SPIKE_MS_CLIENT_SECRET` | Client secret value. |
| `SPIKE_MS_FLOOR_ID` | Optional, instead of `--floor-id`. |

Find a floor id with `GET https://graph.microsoft.com/v1.0/places/microsoft.graph.floor`
(Graph Explorer works). The tenant needs a building with at least one floor.

```bash
uv run python scripts/spikes/resource_directory_spike.py ms-create-delete \
  --floor-id <floor place id> --execute > ms-create-delete-findings.json
```

Options: `--places-base` (default `https://graph.microsoft.com/beta`, because Graph
documents place create and delete only on beta), `--poll-interval` (10s),
`--provision-timeout` (600s), `--post-delete-wait` (60s). If the mailbox is still there
after 60 seconds, run again with a longer `--post-delete-wait` (for example 900) to
tell "deleted later" apart from "never deleted".

Steps: POST a room under the floor with tag `vinta-link-<uuid>` → poll the place until
it has an `emailAddress` (timed) → look the room up by tag with `$filter` → poll the
room's mailbox calendar until it answers (timed) → app-only `getSchedule` → PATCH
capacity and read back (also checks that the tag survives) → DELETE → GET the place →
wait → GET the mailbox user and its calendar.

### `ms-description` (Q2)

Same credentials and options as `ms-create-delete`.

```bash
uv run python scripts/spikes/resource_directory_spike.py ms-description \
  --floor-id <floor place id> --execute > ms-description-findings.json
```

Steps: create a throwaway room → record the **names** of the properties Graph returns
→ for each candidate in `label`, `nickname` and `description`, PATCH an about 300-character,
two-line, punctuated value and read it back → delete the room. `description` is not a
documented room property. It is probed to record how Graph rejects an unknown one.

## Reading the output

| Finding key | What it answers |
|---|---|
| `insert_without_building_status` / `_error` | Q8a. 2xx = building optional; 400 plus a reason = required. |
| `duplicate_name_insert_status` / `_error` | Q8b. 2xx = duplicates allowed. |
| `replay_insert_status` / `_error` | 409 `duplicate` = idempotent insert works as Phase 4 assumes. |
| `insert_kept_client_resource_id` | Google kept our `resourceId`. |
| `readback_description_round_trip` | `resourceDescription` round-trips on Google. |
| `get_after_delete_status` | 404 = Google delete is immediate. |
| `token_roles` | The application permissions in the Microsoft token. |
| `create_status` / `create_error` | Whether `POST /places` works on `--places-base`. |
| `create_response_has_email`, `email_address_seconds` | When the room email exists. |
| `mailbox_calendar_status`, `mailbox_calendar_seconds` | When the room mailbox answers. |
| `tag_round_trip`, `tag_survives_patch` | The tag is stored and kept. |
| `tag_filter_status`, `tag_filter_found_room` | The tag lookup Phase 6 does before every POST. |
| `get_schedule_status` / `_error` | Plan open question 4. |
| `patch_capacity_status`, `patch_capacity_round_trip` | PATCH works for capacity. |
| `delete_status`, `get_place_after_delete_status` | The place is gone. |
| `mailbox_user_status_after_delete`, `mailbox_calendar_status_after_delete` | Q1. 404 = mailbox removed; 200 = mailbox left behind. |
| `room_property_names` | Q2. Every property name a room carries. |
| `candidate_<field>` | Q2. `patch_status`, `round_trip_exact`, `stored_length` against `sent_length`. |

## Findings

Fill in one row per question after a real run. Attach the JSON summaries to the PR
that records them, and note the date and the tenant (by name, not by id).

| Question | Observed | Impact on Phase 4 / Phase 6 |
|---|---|---|
| Q1: Graph delete removes the room mailbox? | _not run yet_ | If the mailbox stays, Phase 6 still marks the room archived and the Microsoft setup doc tells IT admins to remove the mailbox in Exchange. Plan default. |
| Q2: Free-text description field on Microsoft rooms? | _not run yet_ | If a candidate round-trips exactly, Phase 6 maps description to it and removes the `TODO(spike)`. If none does, description stays Vinta-only for Microsoft rooms and the API contract says so. Plan default. |
| Q8a: Google requires `buildingId` for `CONFERENCE_ROOM`? | _not run yet_ | No change either way: a location is already required for both providers. Record it so Phase 4's error mapping expects the right status. |
| Q8b: Google rejects duplicate room names? | _not run yet_ | If rejected, Phase 4 classifies the error as invalid input (sync failed at once, no retry). Plan default. |
| Google replayed insert returns 409 | _not run yet_ | If not 409, Phase 4's idempotent create needs a read-before-insert. |
| Microsoft tag round-trips and is filterable | _not run yet_ | If not, `amend-plan` switches Phase 6 to a displayName-plus-parent lookup (plan **Risks**). |
| Microsoft provisioning delay | _not run yet_ | Sets the expected delay before a new Microsoft room is bookable. Retries already absorb it. |
| App-only `getSchedule` with `Calendars.Read` | _not run yet_ | If 403, add the missing permission to the Entra app and the setup doc (Phase 5/6). |
| Places create/delete on v1.0 vs beta | _not run yet_ | Sets which Graph version Phase 6 calls. |
