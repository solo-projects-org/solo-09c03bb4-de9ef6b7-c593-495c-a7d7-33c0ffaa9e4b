import base64
import csv
import io
import re
import sqlite3

import pytest

from app import create_app, get_db

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": str(tmp_path / "test.db"),
            "UPLOAD_DIR": str(tmp_path / "uploads"),
        }
    )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def login(app, username):
    client = app.test_client()
    response = client.post(
        "/login", data={"username": username, "password": "demo1234"}
    )
    assert response.status_code in (302, 303)
    return client


def card_id(app, card_no):
    with app.app_context():
        row = get_db().execute(
            "SELECT id FROM cards WHERE card_no = ?", (card_no,)
        ).fetchone()
        return row["id"]


def entry_tokens(client, cid):
    """Read the optimistic-concurrency token embedded in the entry form."""
    page = client.get(f"/clerk/cards/{cid}").data.decode("utf-8")
    return dict(
        re.findall(
            r'name="(expected_entry_id|expected_entry_revision)" value="(\d*)"',
            page,
        )
    )


def entry_payload(client, cid, **overrides):
    payload = {
        "station_no": "30501200",
        "obs_date": "1986-07-14",
        "rainfall_mm": "42.5",
    }
    payload.update(entry_tokens(client, cid))
    payload.update(overrides)
    return payload


def get_export(app):
    reviewer = login(app, "reviewer")
    response = reviewer.get("/reviewer/export.csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.data.decode("utf-8"))))
    return response, rows


def review_form(app, card_no):
    reviewer = login(app, "reviewer")
    cid = card_id(app, card_no)
    page = reviewer.get(f"/reviewer/cards/{cid}").data.decode("utf-8")
    fields = dict(
        re.findall(
            r'name="(entry_[ab]_(?:id|revision))" value="(\d+)"', page
        )
    )
    assert len(fields) == 4
    return reviewer, cid, fields, page


def test_demo_login_and_roles(app):
    clerk = login(app, "clerk1")
    assert clerk.get("/clerk").status_code == 200
    assert clerk.get("/reviewer").status_code == 403

    reviewer = login(app, "reviewer")
    assert reviewer.get("/reviewer").status_code == 200
    assert reviewer.get("/clerk").status_code == 403

    anonymous = app.test_client()
    assert anonymous.get("/clerk").status_code == 302


def test_clerk_cannot_see_peer_entry(app):
    clerk = login(app, "clerk1")
    cid = card_id(app, "YC-1986-0715")
    page = clerk.get(f"/clerk/cards/{cid}").data.decode("utf-8")
    # The clerk sees their own 31.0 reading, but not clerk2's 13.0 reading.
    assert "31" in page
    assert ">13<" not in page
    assert "13 mm" not in page
    assert "录入员乙" not in page


def test_two_agreeing_entries_are_auto_approved(app):
    c1 = login(app, "clerk1")
    c2 = login(app, "clerk2")
    cid = card_id(app, "YC-1986-0714")
    for client, value in ((c1, "42.5"), (c2, "42.50")):
        response = client.post(
            f"/clerk/cards/{cid}",
            data=entry_payload(
                client, cid, rainfall_mm=value
            ),
        )
        assert response.status_code == 302

    _, rows = get_export(app)
    matching = [r for r in rows if r["card_no"] == "YC-1986-0714"]
    assert len(matching) == 1
    assert matching[0]["station_no"] == "30501200"
    assert matching[0]["observation_date"] == "1986-07-14"
    assert matching[0]["rainfall_mm"] == "42.5"
    assert matching[0]["verification"] == "double_entry_agreement"
    # One row per approved card.
    assert len({r["card_no"] for r in rows}) == len(rows)


def test_conflict_requires_review_and_uses_selected_version(app):
    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}

    reviewer, cid, fields, page = review_form(app, "YC-1986-0715")
    assert "31" in page and "13" in page
    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )
    assert response.status_code == 302

    _, rows = get_export(app)
    match = [r for r in rows if r["card_no"] == "YC-1986-0715"]
    assert len(match) == 1
    assert match[0]["rainfall_mm"] == "31"
    assert match[0]["verification"] == "reviewer_adjudication"

    # Any later save (even textually identical) revokes the award.
    c2 = login(app, "clerk2")
    response = c2.post(
        f"/clerk/cards/{cid}",
        data=entry_payload(
            c2,
            cid,
            station_no="30501210",
            obs_date="1986-07-15",
            rainfall_mm="13.0",
        ),
    )
    assert response.status_code == 302
    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}

    with app.app_context():
        decisions = get_db().execute(
            "SELECT * FROM decisions WHERE card_id = ?", (cid,)
        ).fetchall()
    assert len(decisions) == 1
    assert decisions[0]["voided_at"] is not None
    assert decisions[0]["voided_reason"] == "entry_edited"


def test_stale_adjudication_is_refused(app):
    reviewer, cid, old_fields, _ = review_form(app, "YC-1986-0715")
    clerk2 = login(app, "clerk2")
    clerk2.post(
        f"/clerk/cards/{cid}",
        data=entry_payload(
            clerk2,
            cid,
            station_no="30501210",
            obs_date="1986-07-15",
            rainfall_mm="14",
        ),
    )

    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**old_fields, "selected_entry_id": old_fields["entry_a_id"]},
    )
    assert response.status_code == 409
    assert "旧裁定已拒绝" in response.data.decode("utf-8")

    with app.app_context():
        count = get_db().execute(
            "SELECT COUNT(*) FROM decisions WHERE card_id = ?", (cid,)
        ).fetchone()[0]
    assert count == 0

    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}


def test_resolution_can_be_redone_after_edit(app):
    reviewer, cid, fields, _ = review_form(app, "YC-1986-0715")
    reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )

    c2 = login(app, "clerk2")
    c2.post(
        f"/clerk/cards/{cid}",
        data=entry_payload(
            c2,
            cid,
            station_no="30501210",
            obs_date="1986-07-15",
            rainfall_mm="12.5",
        ),
    )

    reviewer2, cid2, fresh, _ = review_form(app, "YC-1986-0715")
    _, rows = get_export(app)
    assert "YC-1986-0715" not in {r["card_no"] for r in rows}

    response = reviewer2.post(
        f"/reviewer/cards/{cid2}/decide",
        data={**fresh, "selected_entry_id": fresh["entry_b_id"]},
    )
    assert response.status_code == 302
    _, rows = get_export(app)
    match = [r for r in rows if r["card_no"] == "YC-1986-0715"]
    assert len(match) == 1
    assert match[0]["rainfall_mm"] == "12.5"


def test_agreement_after_edit_wins_and_needs_no_adjudication(app):
    reviewer, cid, fields, _ = review_form(app, "YC-1986-0715")
    reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )

    c2 = login(app, "clerk2")
    c2.post(
        f"/clerk/cards/{cid}",
        data=entry_payload(
            c2,
            cid,
            station_no="30501210",
            obs_date="1986-07-15",
            rainfall_mm="31.0",
        ),
    )

    _, rows = get_export(app)
    match = [r for r in rows if r["card_no"] == "YC-1986-0715"]
    assert len(match) == 1
    assert match[0]["verification"] == "double_entry_agreement"
    assert match[0]["rainfall_mm"] == "31"
    # A decision against the pre-edit versions cannot be added now either.
    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )
    assert response.status_code == 409


def test_upload_creates_card_and_rejects_non_image(app):
    reviewer = login(app, "reviewer")
    response = reviewer.post(
        "/reviewer/upload",
        data={
            "card_no": "UPLOAD-1",
            "clerk_a_id": "1",
            "clerk_b_id": "2",
            "scan": (io.BytesIO(PNG_1PX), "card.png"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302

    cid = card_id(app, "UPLOAD-1")
    assert reviewer.get(f"/cards/{cid}/image").status_code == 200
    assert reviewer.get(f"/reviewer/cards/{cid}").status_code == 200

    response = reviewer.post(
        "/reviewer/upload",
        data={
            "card_no": "BAD-1",
            "clerk_a_id": "1",
            "clerk_b_id": "2",
            "scan": (io.BytesIO(b"<svg><script>alert(1)</script></svg>"), "bad.svg"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    with app.app_context():
        assert (
            get_db()
            .execute("SELECT COUNT(*) FROM cards WHERE card_no = 'BAD-1'")
            .fetchone()[0]
            == 0
        )


def test_rainfall_and_date_validation(app):
    clerk = login(app, "clerk1")
    cid = card_id(app, "YC-1986-0714")
    base = {"station_no": "30501200", "obs_date": "1986-07-14"}
    for value in ("-1", "abc", "", "NaN", "100001"):
        response = clerk.post(
            f"/clerk/cards/{cid}",
            data=entry_payload(clerk, cid, **base, rainfall_mm=value),
        )
        assert response.status_code == 200
        assert "雨量" in response.data.decode("utf-8")
    response = clerk.post(
        f"/clerk/cards/{cid}",
        data=entry_payload(clerk, cid, obs_date="not-a-date", rainfall_mm="4"),
    )
    assert response.status_code == 200
    with app.app_context():
        entry = get_db().execute(
            "SELECT * FROM entries WHERE card_id = ? AND clerk_id = 1", (cid,)
        ).fetchone()
    assert entry is None


def test_csv_is_reviewer_only(app):
    clerk = login(app, "clerk1")
    assert clerk.get("/reviewer/export.csv").status_code == 403
    anonymous = app.test_client()
    assert anonymous.get("/reviewer/export.csv").status_code == 302


def test_decision_requires_both_entries_and_current_versions(app):
    reviewer = login(app, "reviewer")
    cid = card_id(app, "YC-1986-0717")  # only clerk1 has entered
    response = reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={
            "entry_a_id": "1",
            "entry_a_revision": "1",
            "entry_b_id": "2",
            "entry_b_revision": "1",
            "selected_entry_id": "1",
        },
    )
    assert response.status_code == 409
    with app.app_context():
        assert (
            get_db()
            .execute("SELECT COUNT(*) FROM decisions WHERE card_id = ?", (cid,))
            .fetchone()[0]
            == 0
        )


def _own_entry(app, cid, clerk_id):
    with app.app_context():
        return get_db().execute(
            "SELECT * FROM entries WHERE card_id = ? AND clerk_id = ?",
            (cid, clerk_id),
        ).fetchone()


def test_stale_first_entry_draft_is_refused_then_confirm_saves(app):
    cid = card_id(app, "YC-1986-0714")
    tab_a = login(app, "clerk1")
    tab_b = login(app, "clerk1")  # same clerk, second tab/session

    # Both pages open before any entry exists: blank tokens on both.
    old_tokens = entry_tokens(tab_a, cid)
    assert old_tokens["expected_entry_id"] == ""
    entry_tokens(tab_b, cid)

    # Tab B wins the first save.
    first = tab_b.post(
        f"/clerk/cards/{cid}",
        data=entry_payload(tab_b, cid, rainfall_mm="42.5"),
    )
    assert first.status_code == 302
    stored = _own_entry(app, cid, 1)
    assert stored is not None and stored["revision"] == 1
    assert stored["rainfall_mm"] == "42.5"

    # Tab A's stale blank-token draft must be rejected, draft kept visible.
    stale = tab_a.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501200",
            "obs_date": "1986-07-14",
            "rainfall_mm": "99.9",
            **old_tokens,
        },
    )
    assert stale.status_code == 409
    page = stale.data.decode("utf-8")
    assert "旧页提交已拒绝" in page
    assert "99.9" in page  # draft preserved in the form
    assert "42.5" in page  # latest stored values displayed
    # Nothing was written and no version was created.
    stored_after = _own_entry(app, cid, 1)
    assert stored_after["revision"] == 1
    assert stored_after["rainfall_mm"] == "42.5"
    assert stored_after["updated_at"] == stored["updated_at"]

    # The rejected page now carries the current token; confirming saves a v2.
    fresh_tokens = dict(
        re.findall(
            r'name="(expected_entry_id|expected_entry_revision)" value="(\d*)"',
            page,
        )
    )
    assert fresh_tokens["expected_entry_id"] == str(stored_after["id"])
    assert fresh_tokens["expected_entry_revision"] == "1"
    confirmed = tab_a.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501200",
            "obs_date": "1986-07-14",
            "rainfall_mm": "99.9",
            **fresh_tokens,
        },
    )
    assert confirmed.status_code == 302
    final = _own_entry(app, cid, 1)
    assert final["revision"] == 2
    assert final["rainfall_mm"] == "99.9"


def test_stale_resave_keeps_revision_and_active_decision(app):
    cid = card_id(app, "YC-1986-0715")  # conflict card, clerk1 has a revision 1
    tab_a = login(app, "clerk1")
    tab_b = login(app, "clerk1")

    # An active adjudication exists on this card.
    reviewer, _, fields, _ = review_form(app, "YC-1986-0715")
    reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )

    old_tokens = entry_tokens(tab_a, cid)
    entry_tokens(tab_b, cid)

    # Tab B re-saves the identical stored text: a real new version that voids
    # the decision (existing behaviour).
    saved = tab_b.post(
        f"/clerk/cards/{cid}",
        data=entry_payload(
            tab_b,
            cid,
            station_no="30501210",
            obs_date="1986-07-15",
            rainfall_mm="31",
        ),
    )
    assert saved.status_code == 302
    stored = _own_entry(app, cid, 1)
    assert stored["revision"] == 2

    # Tab A's old page (still token v1) submits the same original text again.
    stale = tab_a.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "31",
            **old_tokens,
        },
    )
    assert stale.status_code == 409
    assert "旧页提交已拒绝" in stale.data.decode("utf-8")
    after = _own_entry(app, cid, 1)
    assert after["revision"] == 2  # no bump from the rejected save
    assert after["updated_at"] == stored["updated_at"]
    # The save from tab B already voided the decision; the rejected resave must
    # not create or touch any decision row.
    with app.app_context():
        decisions = get_db().execute(
            "SELECT * FROM decisions WHERE card_id = ? ORDER BY id", (cid,)
        ).fetchall()
    assert len(decisions) == 1
    assert decisions[0]["voided_reason"] == "entry_edited"


def test_adjudication_survives_rejected_stale_save(app):
    cid = card_id(app, "YC-1986-0715")
    reviewer, _, fields, _ = review_form(app, "YC-1986-0715")
    reviewer.post(
        f"/reviewer/cards/{cid}/decide",
        data={**fields, "selected_entry_id": fields["entry_a_id"]},
    )

    tab_a = login(app, "clerk1")
    tab_b = login(app, "clerk1")
    old_tokens = entry_tokens(tab_a, cid)
    entry_tokens(tab_b, cid)

    # Tab B saves a genuine revision (this voids the decision, as before).
    tab_b.post(
        f"/clerk/cards/{cid}",
        data=entry_payload(
            tab_b, cid, station_no="30501210", obs_date="1986-07-15", rainfall_mm="30"
        ),
    )
    with app.app_context():
        voided_at = get_db().execute(
            "SELECT voided_at FROM decisions WHERE card_id = ?", (cid,)
        ).fetchone()["voided_at"]
    assert voided_at is not None

    # Re-adjudicate on the fresh versions so a live decision exists again.
    reviewer2, cid2, fresh, _ = review_form(app, "YC-1986-0715")
    reviewer2.post(
        f"/reviewer/cards/{cid2}/decide",
        data={**fresh, "selected_entry_id": fresh["entry_a_id"]},
    )

    # Now the stale tab A (still holding the v1 token) is refused; the new
    # active decision must remain intact.
    stale = tab_a.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "31",
            **old_tokens,
        },
    )
    assert stale.status_code == 409
    with app.app_context():
        active = get_db().execute(
            "SELECT COUNT(*) FROM decisions WHERE card_id = ? AND voided_at IS NULL",
            (cid,),
        ).fetchone()[0]
    assert active == 1
    # Rejected save did not bump the clerk's revision beyond tab B's save.
    assert _own_entry(app, cid, 1)["revision"] == 2


def test_other_clerk_save_is_not_a_conflict(app):
    cid = card_id(app, "YC-1986-0717")  # clerk1 has entry; clerk2 has none
    clerk1_tab = login(app, "clerk1")
    clerk2 = login(app, "clerk2")

    c1_tokens = entry_tokens(clerk1_tab, cid)
    c2_tokens = entry_tokens(clerk2, cid)
    assert c2_tokens["expected_entry_id"] == ""

    # clerk2's first independent save must succeed even though clerk1's page
    # (and entry) already exists.
    response = clerk2.post(
        f"/clerk/cards/{cid}",
        data=entry_payload(
            clerk2,
            cid,
            station_no="30501230",
            obs_date="1986-07-17",
            rainfall_mm="88",
        ),
    )
    assert response.status_code == 302

    # clerk1 re-saving against the version they saw still succeeds: the other
    # clerk's save is not treated as a conflict on clerk1's own entry.
    response = clerk1_tab.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501230",
            "obs_date": "1986-07-17",
            "rainfall_mm": "88.0",
            **c1_tokens,
        },
    )
    assert response.status_code == 302
    assert _own_entry(app, cid, 1)["revision"] == 2
    # Agreement now auto-approves the card.
    _, rows = get_export(app)
    assert any(r["card_no"] == "YC-1986-0717" for r in rows)


def test_conflict_prompts_again_when_version_changes_after_warning(app):
    cid = card_id(app, "YC-1986-0715")
    tab_a = login(app, "clerk1")
    tab_b = login(app, "clerk1")
    tab_c = login(app, "clerk1")

    old_tokens = entry_tokens(tab_a, cid)
    entry_tokens(tab_b, cid)
    entry_tokens(tab_c, cid)

    # Tab B saves v2; tab A's stale save is refused and shows the v2 token.
    tab_b.post(
        f"/clerk/cards/{cid}",
        data=entry_payload(
            tab_b, cid, station_no="30501210", obs_date="1986-07-15", rainfall_mm="30"
        ),
    )
    stale_page = tab_a.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "31",
            **old_tokens,
        },
    )
    assert stale_page.status_code == 409
    v2_tokens = dict(
        re.findall(
            r'name="(expected_entry_id|expected_entry_revision)" value="(\d*)"',
            stale_page.data.decode("utf-8"),
        )
    )

    # Before the clerk confirms, tab C saves yet another version (v3).
    tab_c.post(
        f"/clerk/cards/{cid}",
        data=entry_payload(
            tab_c, cid, station_no="30501210", obs_date="1986-07-15", rainfall_mm="29"
        ),
    )

    # Confirming against the now-stale v2 token must be refused again.
    again = tab_a.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "31",
            **v2_tokens,
        },
    )
    assert again.status_code == 409
    assert "旧页提交已拒绝" in again.data.decode("utf-8")
    assert _own_entry(app, cid, 1)["revision"] == 3
    # The second rejected page surfaces the latest stored value (29).
    assert "29" in again.data.decode("utf-8")


def test_validation_error_keeps_original_version_token(app):
    cid = card_id(app, "YC-1986-0715")  # clerk1 already has revision 1
    tab_a = login(app, "clerk1")
    tab_b = login(app, "clerk1")
    old_tokens = entry_tokens(tab_a, cid)
    entry_tokens(tab_b, cid)

    # Tab B advances the entry to revision 2.
    tab_b.post(
        f"/clerk/cards/{cid}",
        data=entry_payload(
            tab_b, cid, station_no="30501210", obs_date="1986-07-15", rainfall_mm="30"
        ),
    )

    # Tab A fails field validation first: no write, and the page keeps the
    # original (now stale) token so the next corrected retry is judged against
    # the version originally seen.
    invalid = tab_a.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "not-a-number",
            **old_tokens,
        },
    )
    assert invalid.status_code == 200
    page = invalid.data.decode("utf-8")
    assert "雨量" in page
    assert f'name="expected_entry_revision" value="{old_tokens["expected_entry_revision"]}"' in page

    # Correcting the value and retrying with the same stale token is refused.
    retry = tab_a.post(
        f"/clerk/cards/{cid}",
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "31",
            **old_tokens,
        },
    )
    assert retry.status_code == 409
    assert _own_entry(app, cid, 1)["revision"] == 2
