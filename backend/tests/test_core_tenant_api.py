"""Real HTTP regression for tenant visibility and aggregate count parity."""
import httpx

from test_v4_support import API, db, make_company, make_shipment, ops


def test_operator_visibility_toggle_covers_new_tenant_records_and_counts():
    normal = {"X-Include-Test-Tenants": "false"}
    show = {"X-Include-Test-Tenants": "true"}
    c = make_company(name="E2E Testi tenant-visibility Oy")
    db.companies.update_one({"id": c["id"]}, {"$set": {"test_tenant": True}})
    s = make_shipment(c)
    # Deliberately remove the old row-level test flag: the company must own visibility.
    db.shipments.update_one({"id": s["id"]}, {"$unset": {"test": ""}})
    try:
        assert not any(x["id"] == c["id"] for x in ops().get("/ops/customers", headers=normal).json()["items"])
        assert any(x["id"] == c["id"] for x in ops().get("/ops/customers", headers=show).json()["items"])
        hidden = ops().get("/shipments", params={"company_id": c["id"]}, headers=normal).json()
        visible = ops().get("/shipments", params={"company_id": c["id"]}, headers=show).json()
        assert hidden["items"] == [] and hidden["total"] == 0 and hidden["counts"]["all"] == 0
        assert visible["total"] == 1 and visible["items"][0]["id"] == s["id"] and visible["counts"]["all"] == 1
        assert ops().get(f"/shipments/{s['id']}", headers=normal).status_code == 404
        assert ops().get(f"/shipments/{s['id']}", headers=show).status_code == 200
        assert ops().patch(f"/ops/customers/{c['id']}/test-tenant", headers=show,
                           json={"test_tenant": False}).status_code == 200
        # Legacy company-level test=True remains hidden even when the new flag is off.
        assert ops().get(f"/shipments/{s['id']}", headers=normal).status_code == 404
    finally:
        db.shipments.delete_one({"id": s["id"]})
        db.quotes.delete_one({"id": s["quote_id"]})
        db.companies.delete_one({"id": c["id"]})


def test_switches_cannot_be_enabled_by_generic_settings_patch():
    assert ops().patch("/settings", json={"autopilot_enabled": True}).status_code == 400
    for name in ("autopilot_enabled", "partner_network_enabled"):
        assert ops().post(f"/settings/autonomy/{name}", json={"enabled": True, "confirmation": "yes"}).status_code == 400
        assert ops().get("/settings").json()[name] is False
