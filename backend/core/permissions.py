"""Roles and permissions. The matrix is data: ops admins see it in Team & roles.

Org types: internal (Huolitsija staff), customer (shipper / consignee companies),
partner (carriers and agents working assigned legs).
"""

INTERNAL_ROLES = ["ops_admin", "ops_agent", "customs_broker", "finance", "sales"]
CUSTOMER_ROLES = ["customer_admin", "customer_user"]
PARTNER_ROLES = ["partner"]
ALL_ROLES = INTERNAL_ROLES + CUSTOMER_ROLES + PARTNER_ROLES

PERMISSIONS: dict[str, str] = {
    "shipments.read_all": "See every customer's shipments",
    "shipments.write": "Change shipment status, legs and milestones",
    "quotes.write": "Create and edit quotes for any customer",
    "quotes.approve": "Approve quotes above the approval threshold",
    "rates.write": "Manage carriers and rate cards",
    "customs.review": "Review and edit customs declarations",
    "customs.file": "Approve and file customs declarations with Tulli",
    "billing.write": "Create, send, void and credit invoices",
    "customers.write": "Edit customer companies, credit limits and terms",
    "kyc.approve": "Approve or reject KYC and sanctions screening",
    "claims.approve": "Decide claims",
    "messages.ops": "Answer customer messages and approve AI replies",
    "exceptions.write": "Handle shipment exceptions",
    "users.manage": "Invite internal users and change roles",
    "settings.write": "Change company settings, thresholds and integrations",
    "ai.view": "See AI agent runs",
    # customer side
    "company.manage": "Edit own company profile, KYC and team",
    "company.book": "Request quotes and book shipments",
    "company.pay": "Pay invoices",
    # partner side
    "partner.update": "Update milestones on assigned legs",
}

ROLE_PERMISSIONS: dict[str, list[str]] = {
    "ops_admin": [p for p in PERMISSIONS if not p.startswith(("company.", "partner."))],
    "ops_agent": [
        "shipments.read_all", "shipments.write", "quotes.write", "rates.write", "customs.review",
        "messages.ops", "exceptions.write", "ai.view",
    ],
    "customs_broker": ["shipments.read_all", "customs.review", "customs.file", "messages.ops", "ai.view"],
    "finance": ["shipments.read_all", "billing.write", "customers.write", "claims.approve", "ai.view"],
    "sales": ["shipments.read_all", "quotes.write", "quotes.approve", "rates.write", "customers.write", "messages.ops"],
    "customer_admin": ["company.manage", "company.book", "company.pay"],
    "customer_user": ["company.book"],
    "partner": ["partner.update"],
}

ROLE_LABELS = {
    "ops_admin": "Ops admin",
    "ops_agent": "Operations",
    "customs_broker": "Customs broker",
    "finance": "Finance",
    "sales": "Sales & pricing",
    "customer_admin": "Admin",
    "customer_user": "Member",
    "partner": "Partner",
}


def org_type_for_role(role: str) -> str:
    if role in INTERNAL_ROLES:
        return "internal"
    if role in PARTNER_ROLES:
        return "partner"
    return "customer"


def permissions_for(user: dict) -> set[str]:
    perms = set(ROLE_PERMISSIONS.get(user.get("role", ""), []))
    perms.update(user.get("extra_permissions") or [])
    return perms
