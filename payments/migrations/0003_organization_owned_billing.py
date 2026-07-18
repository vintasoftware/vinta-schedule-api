# Phase 1 of billing plans and limits: move billing ownership from the user to the
# organization, and repair the dead `Subscription.plan` seam.
#
# Destructive rebuild of `payments_billingprofile` and `payments_subscription` — both
# tables are empty in every environment (no code writes to them today), so no data
# migration is needed. Both tables (and the FK fields on `Payment` /
# `SubscriptionStatusUpdate` that point at them) are dropped and recreated rather than
# altered in place: `BillingProfile`'s primary key changes identity (from a `user` FK to
# an `organization` FK), and Postgres will not let a column backing a live FK constraint
# be dropped without first dropping the dependent constraints. The reverse path recreates
# the prior (`user`-owned) schema in the same way.
#
# The `billing_profile` FK on `payments_payment` and the `subscription` FK on
# `payments_subscriptionstatusupdate` are dropped for the same reason (they point at
# `payments_billingprofile` / `payments_subscription`) and re-added below. Both source
# tables are transitively empty (they FK to the equally-empty tables being rebuilt), so
# dropping and re-adding these columns loses no data either. The re-`AddField` calls at
# `payment.billing_profile` and `subscriptionstatusupdate.subscription` below use
# `preserve_default=False` on a non-null column: if either table were ever non-empty,
# this makes the migration fail loudly (an `IntegrityError` on the implied backfill)
# rather than silently defaulting existing rows to `NULL` and losing the association.
import django.db.models.deletion
import django.utils.timezone
import model_utils.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0014_organization_external_event_update_policy"),
        ("payments", "0002_initial"),
    ]

    operations = [
        # --- Drop the FK fields that depend on the tables being rebuilt. ---
        migrations.RemoveField(
            model_name="payment",
            name="billing_profile",
        ),
        migrations.RemoveField(
            model_name="payment",
            name="subscription",
        ),
        migrations.RemoveField(
            model_name="subscriptionstatusupdate",
            name="subscription",
        ),
        # --- Drop the tables being rebuilt. ---
        migrations.DeleteModel(
            name="Subscription",
        ),
        migrations.DeleteModel(
            name="BillingProfile",
        ),
        # --- New catalog model the rebuilt Subscription.plan resolves to. ---
        migrations.CreateModel(
            name="BillingPlan",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        db_index=True,
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        db_index=True,
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("meta", models.JSONField(blank=True, default=dict, verbose_name="meta")),
                ("slug", models.SlugField(max_length=100, unique=True)),
                ("name", models.CharField(max_length=255)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("is_default_for_new_organizations", models.BooleanField(default=False)),
                ("monthly_price", models.DecimalField(decimal_places=2, max_digits=10)),
                (
                    "annual_price",
                    models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
                ),
                ("currency", models.CharField(max_length=3)),
                ("grace_period_days", models.PositiveIntegerField(blank=True, null=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("is_default_for_new_organizations", True)),
                        fields=("is_default_for_new_organizations",),
                        name="uniq_default_billing_plan",
                    )
                ],
            },
        ),
        # --- Recreate BillingProfile, keyed on Organization instead of User. ---
        migrations.CreateModel(
            name="BillingProfile",
            fields=[
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        db_index=True,
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        db_index=True,
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("meta", models.JSONField(blank=True, default=dict, verbose_name="meta")),
                (
                    "organization",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="billing_profile",
                        serialize=False,
                        to="organizations.organization",
                    ),
                ),
                ("contact_first_name", models.CharField(max_length=255)),
                ("contact_last_name", models.CharField(blank=True, max_length=255)),
                ("contact_email", models.EmailField(max_length=254)),
                ("contact_phone", models.CharField(blank=True, max_length=50)),
                ("document_type", models.CharField(max_length=50)),
                ("document_number", models.CharField(max_length=50)),
                (
                    "billing_address",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="billing_profile",
                        to="payments.billingaddress",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        # --- Recreate Subscription, resolving to an organization + a real plan FK. ---
        migrations.CreateModel(
            name="Subscription",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        db_index=True,
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        db_index=True,
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("meta", models.JSONField(blank=True, default=dict, verbose_name="meta")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("paused", "Paused"),
                            ("cancelled", "Cancelled"),
                            ("pending", "Pending"),
                            ("pending_send", "Pending send"),
                            ("error", "Error"),
                            ("unknown", "Unknown"),
                        ],
                        default="pending_send",
                        max_length=50,
                    ),
                ),
                (
                    "billing_state",
                    models.CharField(
                        choices=[
                            ("free", "Free"),
                            ("active", "Active"),
                            ("grace", "Grace period"),
                            ("restricted", "Restricted"),
                            ("cancelled", "Cancelled"),
                        ],
                        db_index=True,
                        default="free",
                        max_length=20,
                    ),
                ),
                (
                    "billing_interval",
                    models.CharField(
                        choices=[("monthly", "Monthly"), ("annual", "Annual")],
                        default="monthly",
                        max_length=10,
                    ),
                ),
                ("current_period_start", models.DateTimeField()),
                ("current_period_end", models.DateTimeField(db_index=True)),
                (
                    "grace_period_ends_at",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                ("external_id", models.CharField(blank=True, db_index=True, max_length=255)),
                ("plan_external_id", models.CharField(blank=True, max_length=255)),
                (
                    "payment_provider",
                    models.CharField(
                        choices=[("mercadopago", "MercadoPago")], max_length=50
                    ),
                ),
                (
                    "organization",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="subscription",
                        to="organizations.organization",
                    ),
                ),
                (
                    "plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="subscriptions",
                        to="payments.billingplan",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        # --- Re-add the FK fields that were dropped above, now pointing at the
        #     rebuilt tables. ---
        migrations.AddField(
            model_name="payment",
            name="billing_profile",
            field=models.ForeignKey(
                default=None,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="payments",
                to="payments.billingprofile",
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="payment",
            name="subscription",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="payments",
                to="payments.subscription",
            ),
        ),
        migrations.AddField(
            model_name="subscriptionstatusupdate",
            name="subscription",
            field=models.ForeignKey(
                default=None,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="status_updates",
                to="payments.subscription",
            ),
            preserve_default=False,
        ),
        # --- Genuine pre-existing bug: RefundStatusUpdate.status used PaymentStatuses
        #     instead of RefundStatuses. `payments_refundstatusupdate` is empty in every
        #     environment (it FKs to `payments_refund`, which FKs to the equally-empty
        #     `payments_payment`), so there is no pre-existing row whose status value
        #     would fall outside the new (narrower) `RefundStatuses` choices. ---
        migrations.AlterField(
            model_name="refundstatusupdate",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending_send", "Pending Send"),
                    ("pending", "Pending"),
                    ("approved", "Approved"),
                    ("rejected", "Rejected"),
                    ("failed", "Failed"),
                    ("unknown", "Unknown"),
                ],
                max_length=50,
            ),
        ),
    ]
