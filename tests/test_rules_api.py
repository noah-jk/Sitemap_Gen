from app import auth
from app.db import Store
from tests.test_app import (PASSWORD, client, csrf_from, grid_csrf, imported, logged_in, login,  # noqa: F401
                            page_by_path)

MERGE = {"mode": "include", "groups": [[{"field": "path", "op": "matches", "value": "/admissions/*"}]],
         "actions": [{"field": "recommendation", "kind": "value", "value": "combine"},
                     {"field": "redirect_to", "kind": "sitemap_parent"}]}


def test_rule_library_belongs_to_the_person(imported, tmp_path):
    hdr = {"X-CSRF-Token": grid_csrf(imported)}
    r = imported.post("/api/rules", json={"name": "  Merge   sub-pages ", "definition": MERGE}, headers=hdr)
    assert r.status_code == 200 and r.json()["name"] == "Merge sub-pages"
    rule_id = r.json()["id"]
    second = imported.post("/api/rules", json={"name": "Owner", "definition": {
        "groups": [], "actions": [{"field": "owner", "value": "Web"}]}}, headers=hdr).json()["id"]
    listed = imported.get("/api/rules").json()
    assert [x["name"] for x in listed["rules"]] == ["Merge sub-pages", "Owner"] and "conditions" in listed["schema"]
    imported.post("/api/rules/order", json={"ids": [second, rule_id]}, headers=hdr)
    assert [x["id"] for x in imported.get("/api/rules").json()["rules"]] == [second, rule_id]
    bad = imported.put(f"/api/rules/{rule_id}", json={"name": "x", "definition": {"groups": [], "actions": []}}, headers=hdr)
    assert bad.status_code == 400 and "at least one" in bad.json()["detail"]
    assert imported.post("/api/rules", json={"name": "x", "definition": MERGE},
                         headers={"X-CSRF-Token": "forged"}).status_code == 403

    # Another person sees none of these and can't change them.
    auth.create_user(Store(str(tmp_path / "data")), "sam", PASSWORD)
    imported.post("/logout", data={"csrf": hdr["X-CSRF-Token"]})
    token = csrf_from(imported.get("/login").text)
    imported.post("/login", data={"username": "sam", "password": PASSWORD, "csrf": token})
    sam = {"X-CSRF-Token": grid_csrf(imported)}
    assert imported.get("/api/rules").json()["rules"] == []
    assert imported.put(f"/api/rules/{rule_id}", json={"name": "mine", "definition": MERGE}, headers=sam).status_code == 404
    imported.delete(f"/api/rules/{rule_id}", headers=sam)
    imported.post("/logout", data={"csrf": sam["X-CSRF-Token"]})
    login(imported)
    assert len(imported.get("/api/rules").json()["rules"]) == 2


def test_preview_then_apply_as_one_undo(imported):
    hdr = {"X-CSRF-Token": grid_csrf(imported)}
    body = {"rules": [{"name": "Merge", "definition": MERGE},
                      {"name": "Decide", "definition": {"groups": [[{"field": "recommendation", "op": "not_empty"},
                                                                     {"field": "decision", "op": "is_empty"}]],
                                                        "actions": [{"field": "decision", "kind": "field",
                                                                     "value": "recommendation"}]}}]}
    preview = imported.post("/api/p/example/rules/preview", json=body, headers=hdr).json()["rules"]
    assert preview[0]["changed"] == 2 and preview[0]["sample"][0]["changes"][1]["to"].startswith("Admissions")
    assert page_by_path(imported, "/admissions/apply")["recommendation"] is None   # nothing changed yet
    r = imported.post("/api/p/example/rules/apply", json=body, headers=hdr).json()
    apply = page_by_path(imported, "/admissions/apply")
    assert (apply["recommendation"], apply["decision"]) == ("combine", "combine") and r["changed"] >= 2
    history = imported.get("/api/p/example/history").json()
    assert history["undo"].startswith("Ran 2 rules (Merge, Decide): changed")
    detail = imported.get(f"/api/p/example/pages/{apply['id']}/detail").json()
    assert detail["rule_marks"]["decision"] == "Decide"
    assert imported.post("/api/p/example/history/undo", headers=hdr).status_code == 200
    apply = page_by_path(imported, "/admissions/apply")
    assert (apply["recommendation"], apply["decision"]) == (None, None)
    assert imported.get(f"/api/p/example/pages/{apply['id']}/detail").json()["rule_marks"] == {}
    bad = imported.post("/api/p/example/rules/apply", json={"rules": [{"name": "x", "definition": {
        "groups": [[{"field": "nope", "op": "is", "value": 1}]], "actions": [{"field": "status", "value": "done"}]}}]},
        headers=hdr)
    assert bad.status_code == 400 and "Unknown field" in bad.json()["detail"]


def test_date_sources_and_coverage(imported):
    hdr = {"X-CSRF-Token": grid_csrf(imported)}
    ctx = imported.get("/api/p/example/rules/context").json()
    assert ctx["date_sources"] == ["url_date", "url_year"] and ctx["coverage"]["dated"] >= 1
    r = imported.post("/api/p/example/dates", json={"sources": ["url_date"]}, headers=hdr)
    assert r.json()["date_sources"] == ["url_date"]
    assert imported.post("/api/p/example/dates", json={"sources": ["bogus"]}, headers=hdr).status_code == 400
    assert "rules" in imported.get("/p/example/rules").text.lower()


def test_rules_changing_the_same_field_apply_together(imported):
    """The second rule overwrites the first one's mark on the same page (an upsert), inside one
    recorded action."""
    hdr = {"X-CSRF-Token": grid_csrf(imported)}
    review = {"groups": [[{"field": "path", "op": "matches", "value": "/admissions/**"}]],
              "actions": [{"field": "recommendation", "kind": "value", "value": "review"}]}
    combine = {"groups": [[{"field": "path", "op": "matches", "value": "/admissions/*"}]],
               "actions": [{"field": "combine_into", "kind": "sitemap_parent", "value": "recommendation"}]}
    body = {"rules": [{"name": "Review", "definition": review}, {"name": "Combine", "definition": combine}]}
    r = imported.post("/api/p/example/rules/apply", json=body, headers=hdr)
    assert r.status_code == 200 and r.json()["changed"] >= 2
    apply = page_by_path(imported, "/admissions/apply")
    assert apply["recommendation"] == "combine"
    detail = imported.get(f"/api/p/example/pages/{apply['id']}/detail").json()
    assert detail["rule_marks"]["recommendation"] == "Combine"
    assert imported.post("/api/p/example/history/undo", headers=hdr).status_code == 200
    assert page_by_path(imported, "/admissions/apply")["recommendation"] is None
