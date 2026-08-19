"""Drop the eleven constraints and two indexes whose names ``vinta_billing``
also uses, *before* that app's ``0001_initial`` tries to create them.

Postgres puts constraint and index names in a schema-wide namespace, not a
per-table one. ``vinta_billing.0001_initial`` was generated from models ported
field-for-field from this app's, so its ``Meta.constraints`` /``Meta.indexes``
carry byte-identical names -- ``uniq_provider_webhook_event`` and the twelve
others below. With ``payments_*`` still standing, creating
``vinta_billing_providerwebhookevent`` fails outright with ``relation
"uniq_provider_webhook_event" already exists``, in *every* environment
including a fresh test database. (The implementation plan assumed the package
had picked its own names; it has not.)

``run_before`` is what makes this land in time: it inserts this migration ahead
of ``vinta_billing.0001_initial`` in the plan, wherever the rest of the graph
would otherwise have put it.

Dropping rather than renaming, and dropping rather than deferring, is
deliberate:

* These rows are read-only for the rest of the ``migrate`` run -- the very next
  host migration (``0024``) copies them into ``vinta_billing_*`` and drops the
  ``payments_*`` tables, and nothing writes billing rows in between. A uniqueness
  guarantee is only worth what a concurrent writer could violate, and there is
  no such writer inside a migration run.
* A rename would leave the state and the database disagreeing about names,
  which the ``RemoveConstraint`` operations ``0024`` is generated with would
  then fail on.

The reverse re-adds all thirteen from the migration state, which is exact.
It is also *safe*, and only because of ``run_before``: that same edge makes
``vinta_billing.0001_initial`` depend on this migration, so reversing past this
point forces Django to unapply ``vinta_billing`` first, dropping the tables
that hold the colliding names. Reversing this without that ordering would
re-collide. See ``0024``'s docstring for the full rollback path.
"""

from django.db import migrations


#: ``(model_name, constraint_name)`` -- every ``Meta.constraints`` entry this app
#: and ``vinta_billing`` name identically.
COLLIDING_CONSTRAINTS = [
    ("billingperiodresourceusage", "uniq_billing_period_resource_usage"),
    ("billingperiodsummary", "uniq_billing_period_summary"),
    ("billingplan", "uniq_default_billing_plan"),
    ("limitwarningnotification", "uniq_limit_warning_notification"),
    ("meteredoccurrence", "uniq_metered_occurrence"),
    ("paymentmethod", "uniq_payment_method"),
    ("planentitlement", "uniq_plan_entitlement_key"),
    ("planlimit", "uniq_plan_limit_resource"),
    ("providerwebhookevent", "uniq_provider_webhook_event"),
    ("subscriptionentitlement", "uniq_sub_entitlement_key"),
    ("subscriptionplanlimit", "uniq_sub_limit_resource"),
]

#: ``(model_name, index_name)`` -- same story for ``Meta.indexes``.
COLLIDING_INDEXES = [
    ("billingperiodsummary", "billing_period_org_idx"),
    ("meteredoccurrence", "metered_occ_sub_period_idx"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0022_capability_permissions"),
    ]

    run_before = [
        ("vinta_billing", "0001_initial"),
    ]

    operations = [
        *(
            migrations.RemoveConstraint(model_name=model_name, name=name)
            for model_name, name in COLLIDING_CONSTRAINTS
        ),
        *(
            migrations.RemoveIndex(model_name=model_name, name=name)
            for model_name, name in COLLIDING_INDEXES
        ),
    ]
