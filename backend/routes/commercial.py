"""s7 commercial routes: CRM and prospecting, tenders, margin rules and contract tariffs, credit, cargo insurance."""
from routes import credit, crm, insurance, pricing_rules, tenders

routers = [crm.router, *getattr(crm, "extra_routers", []), tenders.router, pricing_rules.router, credit.router,
           *getattr(credit, "extra_routers", []), insurance.router]
