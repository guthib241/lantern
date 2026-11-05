"""S5 City Permit Portal: every page is served, the whole application goes through over HTTP and in a real
browser, hard-stop fields sit on the payment page, and layout v2 really is different."""
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parallel_support as ps  # noqa: E402

browser = ps.browser   # the Playwright fixture

PAGES = ["/", "/apply/personal", "/apply/address", "/apply/permit", "/apply/documents", "/apply/review", "/apply/payment"]


def reference_in(body: str) -> str:
    m = re.search(r"data-reference='([^']+)'", body)
    assert m, body[:600]
    return m.group(1)


def test_every_page_is_served_in_order(tmp_path):
    with ps.portal_server() as (base, _):
        c = ps.Client(base)
        assert c.json("/api/health")["ok"] is True
        for static in ("/static/portal.css", "/static/portal-v2.css", "/static/portal.js"):
            assert c.get(static)[0] == 200
        assert c.get("/static/../portal.py")[0] == 404
        assert c.get("/apply/nowhere")[0] == 404
        status, _, body, _ = c.get("/")
        assert status == 200 and "Start application" in body and "Demo · fake data only" in body
        status, headers, _, _ = c.get("/apply/review", follow=False)   # can't skip ahead
        assert status == 303 and headers["Location"] == "/apply/personal"
        seen = {}
        n = ps.names("v1")
        for path, keys in [("/apply/personal", ["full_name", "date_of_birth", "email", "phone"]),
                           ("/apply/address", ["street", "city", "pin_code"]),
                           ("/apply/permit", ["permit_type", "start_date", "duration_days", "description"])]:
            status, headers, body, _ = c.get(path)
            seen[path] = status
            assert status == 200 and all(f"id='{n[k]}'" in body for k in keys), path
            c.post(path, {**{n[k]: ps.SEED[k] for k in keys}, "_nav": "next"})
        status, _, body, _ = c.get("/apply/documents")
        assert status == 200 and "type='file'" in body and "enctype='multipart/form-data'" in body
        c.post("/apply/documents", {n["id_proof_type"]: "passport", "_nav": "next"},
               files={n["site_plan"]: (ps.SITE_PLAN.name, ps.SITE_PLAN.read_bytes())})
        for path in ("/apply/review", "/apply/payment"):
            assert c.get(path)[0] == 200, path
            if path == "/apply/review":
                c.post(path, {"_nav": "next"})
        assert c.get("/", follow=False)[0] == 200 and set(PAGES) >= set(seen)


def test_application_goes_through_and_can_be_checked_afterwards():
    with ps.portal_server() as (base, portal):
        c = ps.Client(base)
        c.get("/")
        status, url, body = ps.fill_application(c)
        assert status == 200 and "/apply/done?ref=" in url and "Application received" in body
        ref = reference_in(body)
        assert ps.REFERENCE.match(ref) and "id='reference'" in body
        app = c.json(f"/api/applications/{ref}")
        assert app["status"] == "received" and app["applicant"]["full_name"] == ps.SEED["full_name"]
        assert app["applicant"]["permit_type"] == "home_renovation" and app["payment"] == {"method": "counter", "fee": 500}
        upload = app["applicant"]["site_plan"]
        assert upload == {"name": "site_plan.pdf", "bytes": ps.SITE_PLAN.stat().st_size,
                          "sha256": hashlib.sha256(ps.SITE_PLAN.read_bytes()).hexdigest()}   # an agent can prove the right file went up
        assert c.json("/api/applications")["references"] == [ref]
        assert c.get("/apply/done?ref=CPP-2026-NOPE00")[0] == 404
        assert c.get("/apply/review", follow=False)[0] == 303     # a fresh application starts after submitting
        assert c.post("/api/reset")[0] == 200 and c.json("/api/applications")["references"] == []


def test_card_payment_is_the_users_part_and_only_last4_is_kept():
    with ps.portal_server() as (base, _):
        c = ps.Client(base)
        status, url, body = ps.fill_application(c, pay={"pay_method": "card"})
        assert status == 400 and url.endswith("/apply/payment")
        errors = re.findall(r"class='error' id='([\w-]+)-error'", body)
        assert sorted(errors) == sorted(["card_number", "card_expiry", "card_cvv", "card_name", "otp", "portal_password"])
        n = ps.names("v1")
        secret = {n["card_number"]: "4111 1111 1111 1111", n["card_expiry"]: "12/29", n["card_cvv"]: "737",
                  n["card_name"]: "ASHA RAO", n["otp"]: "482913", n["portal_password"]: "Hunter2-fake!",
                  n["pay_method"]: "card", n["declaration"]: "yes", "_nav": "next"}
        status, _, body, url = c.post("/apply/payment", {**secret, n["card_number"]: "4111 1111 1111 1112"})
        assert status == 400 and "Check the card number." in body          # Luhn
        assert "737" not in body and "482913" not in body and "Hunter2" not in body   # never echoed back
        status, _, body, url = c.post("/apply/payment", secret)
        ref = reference_in(body)
        assert "card ending 1111" in body
        stored = str(c.json(f"/api/applications/{ref}"))
        assert c.json(f"/api/applications/{ref}")["payment"] == {"method": "card", "fee": 500, "card_last4": "1111"}
        assert all(v not in stored for v in ("4111 1111", "737", "482913", "Hunter2"))


def test_payment_page_carries_the_hard_stop_fields_in_both_layouts():
    for layout in ("v1", "v2"):
        with ps.portal_server(layout) as (base, _):
            c = ps.Client(base)
            ps.fill_application(c, layout, pay={"pay_method": "nope"})
            body = c.get("/apply/payment")[2]
            n = ps.names(layout)
            for key in ("card_number", "card_expiry", "card_cvv", "card_name", "otp", "portal_password"):
                assert f"id='{n[key]}'" in body and f"<label for='{n[key]}'>" in body, (layout, key)
            assert body.count("type='password'") == 2                      # CVV and portal password
            hints = {"cc-number", "cc-csc", "one-time-code", "current-password"}
            if layout == "v1":
                assert all(f"autocomplete='{h}'" in body for h in hints)
            else:                                                           # v2: a hard-stop check has to read labels
                assert not any(f"autocomplete='{h}'" in body for h in hints)
                assert "Card no." in body and "Verification code (SMS)" in body and "Security code" in body


def test_validation_explains_and_keeps_what_was_typed():
    with ps.portal_server() as (base, _):
        c = ps.Client(base)
        n = ps.names("v1")
        status, _, body, _ = c.post("/apply/personal", {n["full_name"]: "Asha Rao", n["date_of_birth"]: "1994-03-12",
                                                        n["email"]: "not-an-email", n["phone"]: "123", "_nav": "next"})
        assert status == 400 and "role='alert'" in body and "value='Asha Rao'" in body
        assert "Enter an email address" in body and "10-digit mobile number" in body
        c.post("/apply/personal", {**{n[k]: ps.SEED[k] for k in ("full_name", "date_of_birth", "email", "phone")}, "_nav": "next"})
        status, _, body, _ = c.post("/apply/address", {n["street"]: "x", n["city"]: "y", n["pin_code"]: "12AB", "_nav": "next"})
        assert status == 400 and "A PIN code has 6 digits." in body
        c.post("/apply/address", {**{n[k]: ps.SEED[k] for k in ("street", "city", "pin_code")}, "_nav": "next"})
        c.post("/apply/permit", {**{n[k]: ps.SEED[k] for k in ("permit_type", "start_date", "duration_days", "description")},
                                 "_nav": "next"})
        status, _, body, _ = c.post("/apply/documents", {n["id_proof_type"]: "passport", "_nav": "next"},
                                    files={n["site_plan"]: ("plan.pdf", b"not really a pdf")})
        assert status == 400 and "look like a PDF file" in body
        status, _, body, _ = c.post("/apply/documents", {n["id_proof_type"]: "passport", "_nav": "next"},
                                    files={n["site_plan"]: ("plan.exe", b"MZ")})
        assert status == 400 and "Upload a PDF, PNG or JPG." in body
        status, headers, _, _ = c.post("/apply/address", {"_nav": "back"}, follow=False)
        assert status == 303 and headers["Location"] == "/apply/personal"


def test_layout_v2_is_really_different_and_switching_loses_nothing():
    v1, v2 = ps.LABELS["v1"], ps.LABELS["v2"]
    assert all(v1[k][0] == k for k in v1)                                  # v1: stable ids equal the keys
    assert not {i for i, _ in v1.values()} & {i for i, _ in v2.values()}  # v2: every id renamed
    assert all(v1[k][1] != v2[k][1] for k in v1)                           # ... and every label
    with ps.portal_server() as (base, _):
        c = ps.Client(base)
        b1 = c.get("/apply/personal")[2]
        b2 = c.get("/apply/personal?layout=v2")[2]
        assert "portal.css" in b1 and "portal-v2.css" in b2 and "data-layout='v2'" in b2
        assert b1.index("id='full_name'") < b1.index("id='next'")          # v1: button after the fields
        assert b2.index("id='btn-continue'") < b2.index("id='applicant-name'")  # v2: buttons moved above
        assert "Applicant&#x27;s name" in b2 and "Full name" not in b2
        # the layout cookie sticks; a page filled in v1 ps.names still saves after the switch
        n = ps.names("v1")
        c.post("/apply/personal", {**{n[k]: ps.SEED[k] for k in ("full_name", "date_of_birth", "email", "phone")}, "_nav": "next"})
        body = c.get("/apply/address")[2]
        assert "data-layout='v2'" in body and "id='addr-line-1'" in body
        status, url, body = ps.fill_application(c, "v2")
        assert "id='ref-code'" in body and ps.REFERENCE.match(reference_in(body))


def test_a_browser_fills_both_layouts_by_their_labels(browser):
    """What an agent sees: labels, a native date picker, a file chooser, radio buttons and a checkbox."""
    for layout, words in (("v1", {"start": "Start application", "next": "Next", "submit": "Submit application"}),
                          ("v2", {"start": "Begin", "next": "Continue →", "submit": "File my request"})):
        with ps.portal_server(layout) as (base, _):
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/")
            page.get_by_role("link", name=words["start"]).click()
            L = {k: v[1] for k, v in ps.LABELS[layout].items()}
            page.get_by_label(L["full_name"]).fill(ps.SEED["full_name"])
            page.get_by_label(L["date_of_birth"]).fill(ps.SEED["date_of_birth"])
            page.get_by_label(L["email"]).fill(ps.SEED["email"])
            page.get_by_label(L["phone"]).fill(ps.SEED["phone"])
            page.get_by_role("button", name=words["next"]).click()
            for key in ("street", "city", "pin_code"):
                page.get_by_label(L[key]).fill(ps.SEED[key])
            page.get_by_role("button", name=words["next"]).click()
            page.get_by_label(L["permit_type"]).select_option("home_renovation")
            page.get_by_label(L["start_date"]).fill(ps.SEED["start_date"])
            page.get_by_label(L["duration_days"]).fill(ps.SEED["duration_days"])
            page.get_by_label(L["description"]).fill(ps.SEED["description"])
            page.get_by_role("button", name=words["next"]).click()
            page.get_by_label(L["id_proof_type"]).select_option("passport")
            page.get_by_label(L["site_plan"]).set_input_files(str(ps.SITE_PLAN))
            page.get_by_role("button", name=words["next"]).click()
            assert "site_plan.pdf" in page.inner_text("table.review")
            page.locator("form button.primary").click()
            assert page.locator("#card-fields").is_hidden()                  # counter payment: card fields tucked away
            page.locator("input[type=radio][value=card]").check()
            assert page.locator("#card-fields").is_visible()                 # ...but they exist for the user
            page.locator("input[type=radio][value=counter]").check()
            page.get_by_label(L["declaration"]).check()
            page.get_by_role("button", name=words["submit"]).click()
            ref = page.locator("[data-reference]").get_attribute("data-reference")
            assert ps.REFERENCE.match(ref) and page.locator("h1").inner_text() == "Application received"
            assert errors == []
            page.close()
