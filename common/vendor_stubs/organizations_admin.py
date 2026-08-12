"""Stand-in for the vinta-django-orgs package's own ``organizations.admin`` module.

Registered into ``sys.modules["organizations.admin"]`` from
``vinta_schedule_api/settings/base.py`` *before* Django's app registry loads,
so ``django.contrib.admin.autodiscover()`` imports this empty module instead
of the real one.

Why: the real ``organizations/admin.py`` unconditionally does
``admin.site.register(get_organization_membership_model(), OrganizationMembershipAdmin)``
at import time. With ``ORGANIZATION_MEMBERSHIP_MODEL`` pointed at
``tenancy.OrganizationMembership`` (set in the same settings block as this
stub), that model still carries its ``SafeCompositePrimaryKey`` until Phase 1c
unwinds it -- and Django's ``AdminSite.register()`` refuses a composite-PK
model unconditionally (``ImproperlyConfigured``, checked before the
"already registered" branch, so pre-registering the model ourselves does not
help). That check would otherwise crash the entire app registry on startup.

A second, independent reason this stub is correct rather than merely
expedient: ``tenancy/admin.py`` already registers ``Organization`` (and
``OrganizationBranding``) with this project's own ``ModelAdmin`` subclasses.
Once ``ORGANIZATION_MODEL`` points at ``tenancy.Organization``, the package's
own (unstubbed) admin.py would try to register the *same* model again with
its own generic ``OrganizationAdmin`` -- ``AlreadyRegistered`` either way,
whichever admin module import runs second. Stubbing the package's admin
module out entirely avoids that collision and keeps the existing admin UI
(list displays, inlines, custom forms) exactly as it was before this app was
installed.

Safe to remove once Phase 1c gives ``OrganizationMembership`` a surrogate
primary key -- at that point the real ``organizations/admin.py`` would only
double-register already-registered models, which is a much smaller, easier
problem to solve (unregister-then-register, or drop this stub and register
nothing from the package's side).
"""
