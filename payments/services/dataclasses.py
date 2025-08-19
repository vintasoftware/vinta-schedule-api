from dataclasses import dataclass
from decimal import Decimal


@dataclass
class BillingAddress:
    id: int | None  # noqa: A003
    street_name: str
    street_number: str
    neighborhood: str | None
    address_line_2: str | None
    city: str
    state: str
    country: str
    zip_code: str

    @property
    def address_line_1(self):
        return f"{self.street_name} {self.street_number}"

    def __str__(self):
        return (
            f"{self.id} {self.address_line_1}, {self.address_line_2}"
            f"{f', {self.neighborhood}' if self.neighborhood else ''} - {self.city} "
            f"- {self.state} - {self.country} - {self.zip_code}"
        )


@dataclass
class BillingProfile:
    pk: int | None  # noqa: A003
    first_name: str | None
    last_name: str | None
    email: str | None
    phone: str | None
    document_type: str | None
    document_number: str | None
    billing_address: BillingAddress

    def __str__(self):
        return f"{self.first_name} {self.last_name} - {self.email} - {self.phone}"


@dataclass
class Plan:
    id: int  # noqa: A003
    name: str
    value: Decimal
    currency: str
    billing_day: int


@dataclass
class CreatedPlan(Plan):
    external_id: str


@dataclass
class Subscription:
    id: int  # noqa: A003
    status: str
    external_id: str | None
    billing_profile: BillingProfile
    plan: CreatedPlan
    start_date: str
    end_date: str


@dataclass
class PaymentStatusUpdate:
    id: int | None  # It can be None because it may not be persisted yet  # noqa: A003
    status: str
    description: str | None
    update_external_id: str | None

    def __str__(self):
        return f"{self.id} {self.status} - {self.description}"


@dataclass
class Payment:
    id: int | None  # noqa: A003
    value: Decimal
    currency: str
    payment_provider: str
    external_id: str
    status: str
    billing_profile: BillingProfile
    payment_method: str
    description: str
    status_updates: list[PaymentStatusUpdate]


@dataclass
class Refund:
    id: int  # noqa: A003
    payment: Payment
    value: Decimal
    currency: str

    def __str__(self):
        return f"{self.id} {self.payment} - {self.value} - {self.currency}"


@dataclass
class SubscriptionPayment(Payment):
    subscription_external_id: str
