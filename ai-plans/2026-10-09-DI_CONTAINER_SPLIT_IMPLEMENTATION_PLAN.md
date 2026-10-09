# DI Container Split — Implementation Plan

No SPEC sibling: the request was a one-line refactor brief, and the decisions below come from the planning interview.

## 1. Goals

1. `di_core/containers.py` stops being the file every feature edits. Each owning app declares its own providers in `<app>/containers.py`, so work in `calendar_integration` and work in `webhooks` no longer touch the same file.
2. Call sites do not change. `container.calendar_service`, `get_container().audit_service`, `patch("di_core.containers.container")` and `AppContainer` keep working exactly as today (about 300 call sites).
3. Every provider's dependencies stay explicit in the class bases of the container that owns it (layered inheritance), not hidden behind placeholders.
4. A test makes the new shape hold: a provider name may be declared in only one module, and `AppContainer` itself owns none.

Non-goals:

- No change to any service, its constructor, or which collaborators it receives. This is a move, not a redesign.
- No nested access (`container.calendar.calendar_service`).
- No change to `DICoreConfig.ready()` wiring, `INTERNAL_INSTALLED_APPS`, or `VINTA_BILLING["SERVICE_CONTAINER"]`.
- No new `di_core` package layout beyond one small base module.
- No lazy or auto-discovery of containers. The composition list stays explicit.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Access shape** | Flat. `AppContainer` inherits the domain containers, so every provider is still an attribute of `AppContainer`. Chosen over nested `providers.Container` because that would rewrite about 300 call sites and every test patch, which is itself a conflict generator. |
| **Location** | `<app>/containers.py` in each owning app. `di_core/containers.py` shrinks to `AppContainer` (composition only), `container` and `get_container()`. |
| **Grouping** | One module per owning app: `audit_integration`, `notifications`, `payments` (billing), `legal`, `webhooks`, `public_api`, `calendar_integration`, `organizations`. Billing lives in `payments/` because that app is the host's billing configuration and the DI-ownership decision in `docs/billing.md` stays with the host. |
| **Dependencies** | Layered inheritance. A container inherits the containers whose providers it consumes. Consumed providers are referenced as `Upstream.provider` (for example `AuditContainer.audit_service`), because inherited providers are not names in a subclass body. `config` is declared once in `di_core/base.py` and referenced as `BaseContainer.config.X`; a second `providers.Configuration()` would silently shadow it. |
| **Dependency layers** | Leaves: `AuditContainer`, `NotificationsContainer`. `BillingContainer(Notifications)`. `LegalContainer(Audit)`. `WebhooksContainer(Billing)`. `PublicApiContainer(Audit, Billing)`. `CalendarContainer(Audit, Notifications, Billing, Webhooks)`. `OrganizationsContainer(Audit, Billing, Webhooks, Calendar)`. These come from the current provider arguments in `di_core/containers.py`. |
| **Transition mechanism** | Phase 1 creates every domain container as an empty class with its final bases, and `AppContainer` already inherits all of them. A moving phase replaces each provider's definition in `AppContainer` with an alias line (`audit_service = AuditContainer.audit_service`) at the same position. Consumers still in `AppContainer`'s body keep using the local name, so no moving phase edits another phase's blocks. The final phase deletes the alias lines. This is why `AppContainer`'s bases line is touched once, not eight times. |
| **Silent override** | `DeclarativeContainer` inheritance lets a later declaration of a name replace an earlier one without error. The guard test in the last phase fails on a name declared in two domain modules. The alias lines are the only sanctioned duplicate and exist only during the transition. |
| **Feature flag** | None, and deliberately. Pure refactor with no reachable behavior change, provable by the existing suite and the provider parity check. No flag-removal phase. |
| **Phase granularity** | One phase per domain module, as chosen. Real code dependencies chain them (see the graph), so the plan is 7 waves but never more than 2 wide. Bundling would be faster; it was offered and declined. |
| **Rollback** | Revert the PR. No migration, no data, no external contract. |

## 3. Data Model Changes

None.

### 3.1 Type plumbing

`di_core/base.py` adds `BaseContainer(containers.DeclarativeContainer)` holding `config = providers.Configuration()`. Domain containers subclass it directly or through another domain container. mypy must stay at zero errors; `Upstream.provider` access types as the provider class, which `dependency_injector`'s stubs support.

## 4. API Design

Omitted: no API surface.

## 5. Phased Rollout

### Crew

| Agent | Tier | Takes | Why this tier |
|---|---|---|---|
| `tier2` | 2 | Phase 1, Phase 4, Phase 8, Phase 9, Phase 10 | Phase 1 verifies how `dependency_injector` treats diamond inheritance, base-body `config` references and alias lines, and every later phase copies that pattern. Phases 4, 8 and 9 move provider groups with many cross-module references, including the `providers.List` wrapper whose comment records a past silent failure. Phase 10 writes the guard test and the docs. |
| `tier1-1` | 1 | Phase 2, Phase 5, Phase 7 | Small moves with exact precedent from Phase 1: audit (3 providers), legal (1), public_api (1). |
| `tier1-2` | 1 | Phase 3, Phase 6 | Notifications (1 provider, large constructor block copied verbatim) and webhooks (3 simple factories). |

### Execution graph

Wave = how deep a phase sits in the dependency graph. Phases in the same wave have no dependency on each other and are implemented concurrently.

| Wave | Phases | Agent | Depends on |
|---|---|---|---|
| 1 | Phase 1 | `tier2` | — |
| 2 | Phase 2, Phase 3 | `tier1-1`, `tier1-2` | Phase 1 |
| 3 | Phase 4, Phase 5 | `tier2`, `tier1-1` | Phase 3 (billing); Phase 2 (legal) |
| 4 | Phase 6, Phase 7 | `tier1-2`, `tier1-1` | Phase 4 |
| 5 | Phase 8 | `tier2` | Phase 2, Phase 3, Phase 4, Phase 6 |
| 6 | Phase 9 | `tier2` | Phase 2, Phase 4, Phase 6, Phase 8 |
| 7 | Phase 10 | `tier2` | Phases 2–9 |

**File overlap:** every phase from 2 to 9 edits `@di_core/containers.py`, but only its own provider blocks (replaced in place by alias lines) and its own import lines, so the hunks are disjoint. Same-wave pairs (2/3, 4/5, 6/7) may still show an adjacent-line conflict in the import block; it resolves by keeping both deletions. The `AppContainer` bases line is not touched after Phase 1, which is what the transition mechanism is for.

**Idle:** the graph is a dependency chain with side branches, and that is the real shape: billing needs notifications, webhooks needs billing's `entitlement_service`, calendar needs webhooks, organizations needs calendar. `tier2` is idle in waves 2 and 4 and `tier1-2` in waves 3 and 5 to 7. Nothing is handed down a tier to fill them.

### Phase 1 — Composition skeleton and shared base

**Goal**: ship the structure and prove the mechanism, with no provider moved and no behavior change.

**Depends on**: nothing — starts from the base branch.

Changes:
1. `@di_core/base.py`: `BaseContainer` with the single `config = providers.Configuration()`, plus a docstring stating the convention (reference upstream providers as `Upstream.provider`, reference config as `BaseContainer.config.X`, never redeclare a provider name).
2. `@audit_integration/containers.py`, `@notifications/containers.py`, `@payments/containers.py`, `@legal/containers.py`, `@webhooks/containers.py`, `@public_api/containers.py`, `@calendar_integration/containers.py`, `@organizations/containers.py`: each an empty class with its final bases from Guiding Decisions → Dependency layers, and a one-line docstring naming what it will own. Class names: `AuditContainer`, `NotificationsContainer`, `BillingContainer`, `LegalContainer`, `WebhooksContainer`, `PublicApiContainer`, `CalendarContainer`, `OrganizationsContainer`.
3. `@di_core/containers.py`: `AppContainer` now inherits `BaseContainer` through the eight domain containers, in an order that is a valid linearization (dependents first). Its body is unchanged; the `config = providers.Configuration()` line is removed. `container` and `get_container()` untouched.
4. Only the two things the later phases rely on need proof, so the new test builds throwaway containers that mirror the real shape: a diamond of bases, a subclass-body reference `Upstream.provider`, a `BaseContainer.config.X` reference resolving against `config.from_dict`, and an alias line (`name = Upstream.name`) leaving one provider object that a body-local consumer resolves.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `@di_core/tests/test_container_composition.py` (with `@di_core/tests/__init__.py`) — the four mechanism checks above, plus `AppContainer.providers` equals the provider-name set it had before the phase (captured by the implementer from the unmodified file before editing).
- **Integration**: the full existing suite is the proof that nothing resolves differently.

**Assigned to**: `tier2` (Tier 2) — pattern-setting work whose risk is semantic rather than volume; the composition test is its safety net.

**Reusable skills**: `write-unit-test`.

Acceptance: `AppContainer.providers` has the same names as before, and the composition test proves diamond inheritance, upstream references, shared `config` and alias lines behave as this plan assumes.

### Phase 2 — Move audit providers

**Goal**: `audit_repository`, `audit_additional_repositories`, `audit_service` live in `AuditContainer`.

**Depends on**: Phase 1 (the empty `AuditContainer` shell and `BaseContainer`).

Changes:
1. `@audit_integration/containers.py`: move the three providers verbatim, including their comments. `audit_service` references its siblings as bare names (same class body).
2. `@di_core/containers.py`: replace each of the three definitions with `name = AuditContainer.name`; drop the now-unused `OrganizationAuditRepository` / `OrganizationAuditService` imports.

Spec use-case: audit providers extraction.

Tests:
- **Unit**: `@audit_integration/tests/test_containers.py` — `AppContainer.audit_service is AuditContainer.audit_service`, and a built `AppContainer()` resolves `audit_service()` with the same repository `Singleton` instance across two resolutions.

**Assigned to**: `tier1-1` (Tier 1) — verbatim move with the pattern fixed by Phase 1.

**Reusable skills**: none.

Acceptance: provider names on `AppContainer` identical to before; `audit_integration` and `calendar_integration` suites green.

### Phase 3 — Move notification providers

**Goal**: `notification_service` lives in `NotificationsContainer`.

**Depends on**: Phase 1 (the `NotificationsContainer` shell).

Changes:
1. `@notifications/containers.py`: move the `notification_service` Singleton verbatim with its adapters and renderers, and the vintasend/notifications imports that only it used.
2. `@di_core/containers.py`: replace the definition with `notification_service = NotificationsContainer.notification_service`; drop the moved imports.

Spec use-case: notification providers extraction.

Tests:
- **Unit**: `@notifications/tests/test_containers.py` — identity with `AppContainer`, and `notification_service()` is one instance across resolutions (it is a `Singleton`).

**Assigned to**: `tier1-2` (Tier 1) — verbatim move of one provider.

**Reusable skills**: none.

Acceptance: same provider names; `notifications` suite green.

### Phase 4 — Move billing providers

**Goal**: all `vinta_billing`-facing providers live in `BillingContainer`.

**Depends on**: Phase 3 (`NotificationsContainer.notification_service`, which `dunning_service` and `usage_warning_service` take).

Changes:
1. `@payments/containers.py`: move `payment_gateway`, `subscription_gateway`, `stripe_payment_gateway`, `stripe_subscription_gateway`, both provider registries, `subscription_plan_factory`, `payment_provider_resolver`, `payment_service`, `subscription_service`, `entitlement_service`, `metering_service`, `dunning_service`, `usage_warning_service`, `cycle_close_service`, comments included. `config.X` becomes `BaseContainer.config.X`; `notification_service=` becomes `NotificationsContainer.notification_service`.
2. `@di_core/containers.py`: alias lines in place; drop moved imports.

Spec use-case: billing providers extraction.

Tests:
- **Unit**: `@payments/tests/test_containers.py` — identity with `AppContainer`; `AppContainer()` with `config.from_dict({...})` resolves `payment_service()` and `payment_provider_registry()` using the config values; `dunning_service()` receives the same `notification_service` instance as `container.notification_service()`.
- **Integration**: `@payments/tests/services/test_restricted_enforcement.py` and `@payments/tests/test_prepaid_resource_coverage.py` already build `AppContainer` and must pass unchanged.

**Assigned to**: `tier2` (Tier 2) — about fifteen providers with config and cross-module references; a missed `config` reference fails only at first resolution.

**Reusable skills**: `write-unit-test`.

Acceptance: same provider names; `payments` suite green; `VINTA_BILLING["SERVICE_CONTAINER"]` still resolves billing services.

### Phase 5 — Move legal providers

**Goal**: `consent_service` lives in `LegalContainer`.

**Depends on**: Phase 2 (`AuditContainer.audit_service`).

Changes:
1. `@legal/containers.py`: move `consent_service`, referencing `AuditContainer.audit_service`.
2. `@di_core/containers.py`: alias line; drop the `ConsentService` import.

Spec use-case: legal providers extraction.

Tests:
- **Unit**: `@legal/tests/test_containers.py` — identity, and `consent_service()` holds the same audit service singleton dependency as `audit_service`.

**Assigned to**: `tier1-1` (Tier 1) — one provider.

**Reusable skills**: none.

Acceptance: same provider names; `legal` suite green.

### Phase 6 — Move webhook providers

**Goal**: the three webhook providers live in `WebhooksContainer`.

**Depends on**: Phase 4 (`BillingContainer.entitlement_service`, taken by `webhook_service`).

Changes:
1. `@webhooks/containers.py`: move `webhook_service`, `webhook_calendar_side_effects_service`, `webhook_membership_side_effects_service`.
2. `@di_core/containers.py`: three alias lines; drop the moved imports.

Spec use-case: webhook providers extraction.

Tests:
- **Unit**: `@webhooks/tests/test_containers.py` — identity, and both side-effects services receive one shared `webhook_service` construction chain.

**Assigned to**: `tier1-2` (Tier 1) — three simple factories.

**Reusable skills**: none.

Acceptance: same provider names; `webhooks` suite green.

### Phase 7 — Move public API providers

**Goal**: `public_api_auth_service` lives in `PublicApiContainer`.

**Depends on**: Phase 2 (`AuditContainer.audit_service`), Phase 4 (`BillingContainer.entitlement_service`).

Changes:
1. `@public_api/containers.py`: move the provider.
2. `@di_core/containers.py`: alias line; drop the `PublicAPIAuthService` import.

Spec use-case: public API providers extraction.

Tests:
- **Unit**: `@public_api/tests/test_containers.py` — identity and dependency wiring.
- **Integration**: `@public_api/tests/test_entitlement_gates.py` and `@public_api/tests/test_invitation_groups.py` already use `AppContainer` and must pass unchanged.

**Assigned to**: `tier1-1` (Tier 1) — one provider.

**Reusable skills**: none.

Acceptance: same provider names; `public_api` suite green.

### Phase 8 — Move calendar providers

**Goal**: the calendar service family lives in `CalendarContainer`.

**Depends on**: Phase 2 (`AuditContainer.audit_service`), Phase 3 (`NotificationsContainer.notification_service`), Phase 4 (`BillingContainer.entitlement_service`), Phase 6 (`WebhooksContainer.webhook_calendar_side_effects_service`).

Changes:
1. `@calendar_integration/containers.py`: move `calendar_side_effects_service`, `calendar_permission_service`, `external_event_change_request_service`, `booking_policy_service`, `booking_policy_permission_service`, `external_client_identifier_service`, `calendar_service`, `bookable_slots_service`, `appointment_type_service`. Keep the `providers.List(...)` wrapper and its explanatory comment exactly.
2. `@di_core/containers.py`: alias lines in place; drop the moved imports.

Spec use-case: calendar providers extraction.

Tests:
- **Unit**: `@calendar_integration/tests/test_containers.py` — identity, and `calendar_side_effects_service()` holds a pipeline containing a resolved `WebhookCalendarEventSideEffectsService` instance (the past failure mode: a `Provider` object instead of a handler).

**Assigned to**: `tier2` (Tier 2) — nine providers, four upstream containers, and a known silent-failure construct.

**Reusable skills**: `write-unit-test`.

Acceptance: same provider names; `calendar_integration` suite green; the pipeline test passes.

### Phase 9 — Move organization providers

**Goal**: `organization_service` lives in `OrganizationsContainer`.

**Depends on**: Phase 2 (`AuditContainer.audit_service`), Phase 4 (`BillingContainer.subscription_service` and `entitlement_service`), Phase 6 (`WebhooksContainer.webhook_membership_side_effects_service`), Phase 8 (`CalendarContainer.calendar_service`).

Changes:
1. `@organizations/containers.py`: move the provider with its five upstream references.
2. `@di_core/containers.py`: alias line; drop the `OrganizationService` import. After this phase, every body line left in `AppContainer` is an alias.

Spec use-case: organization providers extraction.

Tests:
- **Unit**: `@organizations/tests/test_containers.py` — identity and dependency wiring.

**Assigned to**: `tier2` (Tier 2) — the widest consumer; a wrong upstream reference fails at first resolution.

**Reusable skills**: `write-unit-test`.

Acceptance: same provider names; `organizations` suite green.

### Phase 10 — Remove aliases, add guard test and docs

**Goal**: `AppContainer` is composition only, and the new shape cannot rot.

**Depends on**: Phases 2–9 (all moved providers; this phase deletes the alias lines they left in `@di_core/containers.py`).

Changes:
1. `@di_core/containers.py`: delete every alias line. `AppContainer` body is empty apart from its docstring.
2. `@di_core/tests/test_container_composition.py`: add the guard — across the eight domain containers, no provider name appears in more than one (excluding `config`); `AppContainer` declares no provider of its own; every domain container is a base of `AppContainer`; each lives in `<app>.containers`.
3. `@AGENTS.md` (symlinked as `CLAUDE.md`): rewrite the Dependency Injection section for the per-app layout, the layering rule and the `Upstream.provider` / `BaseContainer.config.X` convention. Retarget the remaining prose references to `di_core/containers.py` (comments in `organizations/models.py` and the like) to the owning app's module.
4. `@docs/` — only if a page names `di_core/containers.py` as where to register services; point it at the owning app.

Spec use-case: guard and documentation.

Tests:
- **Unit**: the guard test in `@di_core/tests/test_container_composition.py`.
- **Integration**: full outer gate.

**Assigned to**: `tier2` (Tier 2) — test plus documentation tied to the pattern.

**Reusable skills**: `write-unit-test`, `deslop-comments`.

Acceptance: `grep -n "providers\\." di_core/containers.py` returns nothing, the guard test passes, the full outer gate is green.

## 6. Risk & Rollout Notes

- **Feature flag**: none; justified in Guiding Decisions.
- **Silent override**: the main risk. Mitigated by the Phase 1 mechanism tests, the per-phase provider-name parity check against `AppContainer.providers`, and the Phase 10 guard.
- **First-resolution failures**: a wrong `Upstream.provider` or `config` reference only fails when the provider is first resolved, not at import. Each phase's test resolves its own providers from a built container, and the existing suite covers the rest.
- **Import cycles**: the domain containers import service modules, which import models. They load only through `DICoreConfig.ready()` (which imports `di_core.containers`) and through wiring over `INTERNAL_INSTALLED_APPS`, which now also imports each `<app>/containers.py`. Phase 1 adds only empty classes, so a cycle would surface there before any code moves.
- **Concurrent feature work**: anyone adding a service during the split should add it to the owning app's container. While the transition lasts, a new provider added to `AppContainer` still works; Phase 10's guard fails until it is moved. Announce the convention when Phase 1 merges.
- **Rollback**: revert the offending phase's PR; later phases that depend on it revert with it.

## 7. Open Questions

- **Does an alias line (`name = Upstream.name` in a subclass body) behave as Phase 1 assumes?** Recommended default: yes, and the Phase 1 test decides. If it does not, the fallback is for each moving phase to rewrite the remaining local consumers to `Upstream.name`; that adds overlap between same-wave phases, so Phases 2/3, 4/5 and 6/7 would need serializing edges.
- **Should billing's container live in `payments/` or `billing_integration/`?** Recommended default: `payments/`, the host's billing-configuration app. Owner: project lead.

## 8. Touch List

**Phase 1**
- @di_core/base.py
- @di_core/tests/__init__.py
- @di_core/tests/test_container_composition.py
- @audit_integration/containers.py
- @notifications/containers.py
- @payments/containers.py
- @legal/containers.py
- @webhooks/containers.py
- @public_api/containers.py
- @calendar_integration/containers.py
- @organizations/containers.py
- [di_core/containers.py](../di_core/containers.py)

**Phase 2**: [di_core/containers.py](../di_core/containers.py), [audit_integration/containers.py](../audit_integration/containers.py), @audit_integration/tests/test_containers.py

**Phase 3**: [di_core/containers.py](../di_core/containers.py), [notifications/containers.py](../notifications/containers.py), @notifications/tests/test_containers.py

**Phase 4**: [di_core/containers.py](../di_core/containers.py), [payments/containers.py](../payments/containers.py), @payments/tests/test_containers.py

**Phase 5**: [di_core/containers.py](../di_core/containers.py), [legal/containers.py](../legal/containers.py), @legal/tests/test_containers.py

**Phase 6**: [di_core/containers.py](../di_core/containers.py), [webhooks/containers.py](../webhooks/containers.py), @webhooks/tests/test_containers.py

**Phase 7**: [di_core/containers.py](../di_core/containers.py), [public_api/containers.py](../public_api/containers.py), @public_api/tests/test_containers.py

**Phase 8**: [di_core/containers.py](../di_core/containers.py), [calendar_integration/containers.py](../calendar_integration/containers.py), @calendar_integration/tests/test_containers.py

**Phase 9**: [di_core/containers.py](../di_core/containers.py), [organizations/containers.py](../organizations/containers.py), @organizations/tests/test_containers.py

**Phase 10**: [di_core/containers.py](../di_core/containers.py), [di_core/tests/test_container_composition.py](../di_core/tests/test_container_composition.py), [AGENTS.md](../AGENTS.md), [organizations/models.py](../organizations/models.py)
