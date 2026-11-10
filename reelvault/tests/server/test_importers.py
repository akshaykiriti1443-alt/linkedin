"""WP7 importers (DESIGN-SAAS.md 9.2, 11.8, 12.1): Skool fixtures (public group, private group, profile, missing
__NEXT_DATA__, sign-up page, the same __NEXT_DATA__ via page_read), LinkedIn page_read cleanup and PDF text,
trafilatura on saved HTML, the SSRF guard, save_ig_bio. No network: every fetch goes through a mock transport."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from reelvault.ai import net
from reelvault.importers import instagram, linkedin, merge, skool, website
from reelvault.server import db

FIX = Path(__file__).resolve().parents[1] / "fixtures"
SK = FIX / "skool"
WEB = FIX / "web"


# ---------------------------------------------------------------------------------------------- Skool

def test_skool_public_group_keeps_about_survey_owner_and_links():
    out = skool.parse_html((SK / "group_public.html").read_text())
    assert out["status"] == "ok"
    t, m = out["text"], out["meta"]
    assert t.startswith("Skool group: Clinic Growth Lab")
    assert "1,234 members · public group · categories: Wins, Ad reviews" in t
    assert "The 3-ad test we use for every new clinic" in t            # full About (lpDescription)
    assert "What's your biggest problem with ads right now?" in t      # membership questions
    assert "My ads stop working after 2 weeks | Leads don't pick up the phone" in t
    assert "email address" not in t                                    # the email question says nothing
    assert "Owner: Dana Fields (@dana-ads-1234)" in t and "Owner bio: Meta ads for local clinics" in t
    assert "Free ad audit: https://clinic-ads.example.com/free-audit" in t
    assert m["display_name"] == "Clinic Growth Lab" and m["total_members"] == 1234 and m["private"] is False
    assert m["owner_links"]["website"] == "https://clinic-ads.example.com"
    assert [q["q"] for q in m["survey"]][0].startswith("What's your biggest problem")
    kinds = {f["kind"]: f["url"] for f in m["found_links"]}
    assert kinds["website"] == "https://clinic-ads.example.com"
    assert kinds["instagram_bio"] == "https://www.instagram.com/dana.fields.ads/"
    # the page's viewer (currentUser on a group page) is not the subject of the page
    assert "not the page owner" not in t


def test_skool_private_group_and_profile():
    priv = skool.parse_html((SK / "group_private.html").read_text())
    assert priv["status"] == "ok" and "private group" in priv["text"] and priv["meta"]["private"] is True
    prof = skool.parse_html((SK / "profile.html").read_text())
    assert prof["status"] == "ok"
    assert prof["text"].splitlines()[0] == "Skool profile: Jane Doe (@jane-doe-4821)"
    assert "Bio: I run Meta ads for dentists." in prof["text"] and "Location: Leeds, UK" in prof["text"]
    assert prof["meta"]["kind"] == "profile" and prof["meta"]["links"]["website"].startswith("https://janedoe")
    assert {f["kind"] for f in prof["meta"]["found_links"]} == {"website", "instagram_bio"}


def test_skool_missing_next_data_and_signup_page():
    og = skool.parse_html((SK / "no_next_data.html").read_text())
    assert og["status"] == "ok" and og["text"].startswith("Clinic Growth Lab\nFill your clinic's calendar")
    signup = skool.parse_html((SK / "signup.html").read_text())
    assert signup["status"] == "needs_paste" and signup["text"] == ""
    assert "Paste your About text here." in signup["error"]
    assert skool.parse_html("")["status"] == "needs_paste"


def test_skool_same_next_data_via_page_read_gives_the_same_text():
    from_server = skool.parse_html((SK / "group_public.html").read_text())
    nd = json.loads((SK / "page_read_group.json").read_text())
    from_ext = skool.parse_next_data(nd)
    assert from_ext["text"] == from_server["text"]
    assert skool.parse_next_data(nd["props"]["pageProps"])["text"] == from_server["text"]   # bare pageProps
    assert skool.parse_next_data({})["text"] == "" and skool.parse_next_data("junk")["text"] == ""


def test_skool_url_normalizing():
    assert skool.normalize_url("skool.com/@Jane-Doe") == ("skool_profile", "https://www.skool.com/@Jane-Doe")
    assert skool.normalize_url("https://www.skool.com/clinic-lab") == ("skool_group",
                                                                       "https://www.skool.com/clinic-lab/about")
    assert skool.normalize_url("https://skool.com/clinic-lab/about?ref=x")[1] == "https://www.skool.com/clinic-lab/about"
    for bad in ("https://evil.com/@x", "skool.com/", "skool.com/signup", "skool.com/a b"):
        with pytest.raises(ValueError):
            skool.normalize_url(bad)


def _router(pages: dict[str, tuple[int, str, dict]], seen: list | None = None):
    """A transport that checks the address like PinnedTransport (resolver below) and answers from `pages`."""
    ips = {"clinic-ads.example.com": "93.184.216.34", "www.skool.com": "93.184.216.35",
           "evil.example.com": "10.0.0.7", "meta.example.com": "169.254.169.254"}

    class T(httpx.BaseTransport):
        def handle_request(self, request):
            host = request.url.host
            net.resolve_checked(host, 443, resolver=lambda h, p: [ips.get(h, "93.184.216.99")])
            if seen is not None:
                seen.append(str(request.url))
            key = f"{host}{request.url.path}"
            status, body, headers = pages.get(key, (404, "not found", {}))
            return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8", **headers},
                                  request=request)
    return T()


def test_skool_server_fetch_honest_ua_and_no_signup_redirect(monkeypatch):
    seen: list[str] = []
    uas: list[str] = []
    page = (SK / "group_public.html").read_text()

    class T(httpx.BaseTransport):
        def handle_request(self, request):
            seen.append(str(request.url))
            uas.append(request.headers.get("user-agent", ""))
            if request.url.path == "/clinic-growth-lab/about":
                return httpx.Response(200, text=page, headers={"content-type": "text/html"}, request=request)
            return httpx.Response(302, headers={"location": "https://www.skool.com/signup?next=x"}, request=request)

    ok = skool.fetch_page("skool.com/clinic-growth-lab", public_origin="https://app.example.com", transport=T())
    assert ok["status"] == "ok" and ok["meta"]["display_name"] == "Clinic Growth Lab"
    assert uas[0] == "ReelVaultMembers/1.0 (+https://app.example.com)"
    seen.clear()
    red = skool.fetch_page("https://www.skool.com/@someone", transport=T())
    assert red["status"] == "needs_paste"
    assert seen == ["https://www.skool.com/@someone"]      # the redirect to /signup was never requested


# ---------------------------------------------------------------------------------------------- website

def test_website_reads_home_and_matching_pages_only():
    seen: list[str] = []
    pages = {"clinic-ads.example.com/": (200, (WEB / "home.html").read_text(), {}),
             "clinic-ads.example.com/about-us": (200, (WEB / "about.html").read_text(), {}),
             "clinic-ads.example.com/services": (200, (WEB / "services.html").read_text(), {}),
             "clinic-ads.example.com/pricing": (404, "", {}),
             "clinic-ads.example.com/case-studies": (200, "<html><body><p>tiny</p></body></html>", {})}
    res = website.fetch_site("clinic-ads.example.com", transport=_router(pages, seen))
    assert res.status == "ok", res.error
    assert res.meta["title"] and res.meta["sitename"] == "Clinic Ads Co"
    assert res.meta["description"].startswith("We fill dental clinics")
    assert "three ad angles tested every two weeks" in res.text
    assert res.text.count("## Page: ") == 2 and "nobody called the leads back" in res.text
    assert "£2,500 a month" in res.text                                   # tables kept
    paths = [u.split("example.com", 1)[1] for u in seen]
    assert paths[0] == "/" and set(paths[1:]) == {"/about-us", "/services", "/pricing", "/case-studies"}
    assert not any("other-site" in u or "brochure" in u or "blog" in u for u in seen)
    assert len(res.text) <= 20_000 and all(p["chars"] <= website.PAGE_MAX for p in res.meta["pages"])


def test_website_js_only_site_needs_paste_and_link_checks():
    pages = {"clinic-ads.example.com/": (200, (WEB / "js_only.html").read_text(), {})}
    res = website.fetch_site("https://clinic-ads.example.com/", transport=_router(pages))
    assert res.status == "needs_paste" and res.error == "We couldn't read your site. Paste your homepage text."
    for bad in ("", "ftp://x.com", "notawebsite", "https://user:pw@x.com"):
        with pytest.raises(ValueError):
            website.normalize_url(bad)
    assert website.normalize_url("Clinic-Ads.example.com/About#x") == "https://clinic-ads.example.com/About"


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
                                 "http://[::1]/", "http://10.1.2.3/"])
def test_ssrf_guard_refuses_private_addresses(url):
    res = website.fetch_site(url)           # the real PinnedTransport: refused before any connection
    assert res.status == "needs_paste" and "private address" in res.error


def test_ssrf_guard_refuses_redirect_to_private():
    seen: list[str] = []
    pages = {"clinic-ads.example.com/": (302, "", {"location": "https://evil.example.com/admin"})}
    res = website.fetch_site("https://clinic-ads.example.com/", transport=_router(pages, seen))
    assert res.status == "needs_paste" and "private address" in res.error
    assert seen == ["https://clinic-ads.example.com/"]
    pages = {"clinic-ads.example.com/": (302, "", {"location": "http://meta.example.com/latest"})}
    assert "private address" in website.fetch_site("clinic-ads.example.com", transport=_router(pages)).error


# ---------------------------------------------------------------------------------------------- LinkedIn

PAGE_READ_TEXT = (
    "Skip to main content 3 notifications total Jane Doe Jane Doe Meta ads for dental clinics | Founder, Clinic Ads Co "
    "Leeds, United Kingdom Contact info 500+ connections Open to Add profile section Enhance profile Resources "
    "About About I help dental clinics get 30+ new patients a month with Meta ads that keep working. …see more "
    "Experience Experience Founder Founder Clinic Ads Co · Full-time Jan 2021 - Present · 5 yrs Took Smile Co from "
    "12 to 41 new patients a month in 90 days. Show all 4 experiences Education Education University of Leeds "
    "Skills Skills Meta Ads · Copywriting Endorse Show all 12 skills")


def test_linkedin_page_read_cleanup():
    out = linkedin.clean_page(PAGE_READ_TEXT)
    t = out["text"]
    for noise in ("Skip to main content", "notifications", "Add profile section", "Enhance profile", "see more",
                  "Show all", "Endorse", "Contact info"):
        assert noise not in t, noise
    assert "About:\nI help dental clinics get 30+ new patients" in t
    assert "Experience:\nFounder Clinic Ads Co · Full-time" in t
    assert t.count("Jane Doe") == 1
    assert out["meta"]["sections"] == ["About", "Experience", "Education", "Skills"]
    assert len(linkedin.clean_page("x " * 30000)["text"]) <= 20_000


def test_linkedin_pdf_text_and_bad_files():
    text = linkedin.pdf_text((WEB / "linkedin_profile.pdf").read_bytes())
    assert text.startswith("Jane Doe\nMeta ads for dental clinics")
    assert "Took Smile Co from 12 to 41 new patients" in text and "Page 1 of 1" not in text
    with pytest.raises(linkedin.PdfProblem, match="not a PDF"):
        linkedin.pdf_text(b"hello")
    with pytest.raises(linkedin.PdfProblem, match="couldn't read"):
        linkedin.pdf_text(b"%PDF-1.4\n garbage")
    assert linkedin.clean_paste("  a \r\n\r\n\r\n b  ") == "a\n\nb"


# ---------------------------------------------------------------------------------------------- Instagram bio

def test_save_ig_bio_writes_only_the_owners_source(rv_env, member, other_member):
    sid = db.new_id("src")
    with db.tx() as conn:
        conn.execute(db.profile_sources.insert().values(id=sid, user_id=member.id, kind="instagram_bio",
                                                        via="extension", status="waiting", meta={}))
    user = {"pk": "123", "username": "jane.doe.ads", "full_name": "Jane Doe", "category": "Marketing agency",
            "biography": "Meta ads for dentists 🦷\nComment AUDIT for a free audit", "followers": 4210,
            "bio_links": [{"url": "https://janedoe-ads.example.com", "title": "Free audit"},
                          {"url": "https://www.skool.com/clinic-growth-lab/about", "title": ""}],
            "external_url": "https://janedoe-ads.example.com", "is_private": False}
    instagram.save_ig_bio(other_member.id, sid, user)          # not theirs: nothing happens
    with db.read() as conn:
        assert conn.execute(select(db.profile_sources.c.status).where(db.profile_sources.c.id == sid)).scalar() \
            == "waiting"
    instagram.save_ig_bio(member.id, sid, user)
    with db.read() as conn:
        row = conn.execute(select(db.profile_sources).where(db.profile_sources.c.id == sid)).first()
    assert row.status == "ok" and row.text.startswith("Instagram @jane.doe.ads (Jane Doe)")
    assert "Followers: 4,210" in row.text and "Comment AUDIT" in row.text
    assert row.text.count("janedoe-ads.example.com") == 1
    assert [f["kind"] for f in row.meta["found_links"]] == ["website", "skool"]
    instagram.save_ig_bio(member.id, sid, {"username": "empty.one", "biography": "", "bio_links": []})
    with db.read() as conn:
        assert conn.execute(select(db.profile_sources.c.status).where(db.profile_sources.c.id == sid)).scalar() \
            == "needs_paste"


def test_instagram_handles():
    assert instagram.normalize_handle("@Jane.Doe") == "jane.doe"
    assert instagram.normalize_handle("https://www.instagram.com/jane.doe/?igsh=abc") == "jane.doe"
    for bad in ("", "has space", "https://instagram.com/p/xyz", "a" * 31):
        with pytest.raises(ValueError):
            instagram.normalize_handle(bad)


# ---------------------------------------------------------------------------------------------- digest

def test_questionnaire_copy_and_digest_is_deterministic():
    assert merge.QIDS == [f"q{i}" for i in range(1, 15)]
    q = merge.BY_ID
    assert q["q1"]["label"] == "Who do you help?"
    assert q["q1"]["help"] == 'Finish this: "I help ___ get ___." Add size, location or industry if it matters.'
    assert q["q12"]["label"] == "What can you actually make?" and q["q12"]["required"]
    assert [c["id"] for c in q["q12"]["choices"]] == ["on_camera", "voice_only", "screen_recording", "film_broll",
                                                      "hands_demo", "second_person", "go_on_location"]
    assert [x for x in merge.QIDS if q[x]["required"]] == ["q1", "q3", "q7", "q12"]
    ans = {
        "q1": merge.normalize_answer("q1", {"value": "I help dental clinics get 30 new patients a month"}),
        "q3": merge.normalize_answer("q3", {"value": "- Ads stop working\n2) Leads don't call back; Agencies vanish"}),
        "q7": merge.normalize_answer("q7", {"value": "Done-for-you Meta ads, £2,500 a month"}),
        "q8": merge.normalize_answer("q8", {"value": "Smile Co: 12 to 41 patients in 90 days\nBright Dental: 2x"}),
        "q10": merge.normalize_answer("q10", {"value": ["direct", "calm"],
                                              "text": "Always: patients, chairs. Never: leads. Like @Some.Creator"}),
        "q11": merge.normalize_answer("q11", {"value": "Instagram 4k followers, 3 posts a week. Comment AUDIT."}),
        "q12": merge.normalize_answer("q12", {"value": ["voice_only", "screen_recording"], "hours": "4",
                                              "editor": False, "languages": "English, Spanish"}),
        "q14": merge.normalize_answer("q14", {"value": ["@AlexHormozi", "https://instagram.com/x.y/"]}),
        "_draft": {"niche_label": "Meta ads for dentists", "search_terms": ["dental marketing"]},
    }
    d1 = merge.build_digest(ans, covered_topics=["ad-creative"], samples=["Stop boosting posts."])
    assert d1 == merge.build_digest(ans, covered_topics=["ad-creative"], samples=["Stop boosting posts."])
    assert d1["top_pains"] == ["Ads stop working", "Leads don't call back", "Agencies vanish"]
    assert d1["niche_label"] == "Meta ads for dentists" and d1["search_terms"] == ["dental marketing"]
    assert d1["capabilities"]["voice"] and not d1["capabilities"]["on_camera"] and d1["capabilities"]["screen"]
    assert d1["capabilities"]["hours_per_week"] == 4 and d1["capabilities"]["languages"] == ["English", "Spanish"]
    assert d1["language"] == "en" and d1["competitors"] == ["alexhormozi", "x.y"]
    assert d1["voice"]["tone"] == ["direct", "calm"] and d1["voice"]["always"] == ["patients", "chairs"]
    assert d1["voice"]["never"] == ["leads"] and d1["voice"]["style_refs"] == ["@some.creator"]
    assert d1["main_cta"].lower().startswith("comment audit") and d1["proof"][0].startswith("Smile Co")
    from reelvault import insights
    assert insights.can_make("VF01", d1["capabilities"]) == {"ok": False,
                                                             "reason": "Needs you on camera. You said voice only."}
    with pytest.raises(merge.BadAnswer):
        merge.normalize_answer("q12", {"value": ["fly_drone"]})
    with pytest.raises(merge.BadAnswer):
        merge.normalize_answer("q12", {"value": [], "hours": 200})
    with pytest.raises(merge.BadAnswer):
        merge.normalize_answer("q14", {"value": ["not a handle!"]})


def test_skool_profile_page_read_without_profile_data():
    """The extension uploads only props.pageProps.{currentGroup, currentUser}; a profile page may lack profileData."""
    from reelvault.importers import skool

    nd = {"props": {"pageProps": {"currentUser": {"name": "mia-member", "firstName": "Mia", "lastName": "Member",
                                                  "metadata": {"bio": "Meta ads agency for dentists",
                                                               "linkWebsite": "https://mia.example"}}}}}
    out = skool.parse_next_data(nd)
    assert out["text"].startswith("Skool profile: Mia Member (@mia-member)") and "dentists" in out["text"]
    assert out["meta"]["found_links"] == [{"kind": "website", "url": "https://mia.example"}]
    # a group page's viewer is never taken for the page's person
    g = {"props": {"pageProps": {"currentGroup": {"name": "x", "metadata": {}}, "currentUser": nd["props"]["pageProps"]["currentUser"]}}}
    assert skool.parse_next_data(g)["text"] == ""
