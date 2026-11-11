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
            data={
                "station_no": "30501200",
                "obs_date": "1986-07-14",
                "rainfall_mm": value,
            },
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
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "13.0",
        },
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
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "14",
        },
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
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "12.5",
        },
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
        data={
            "station_no": "30501210",
            "obs_date": "1986-07-15",
            "rainfall_mm": "31.0",
        },
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
            data={**base, "rainfall_mm": value},
        )
        assert response.status_code == 200
        assert "雨量" in response.data.decode("utf-8")
    response = clerk.post(
        f"/clerk/cards/{cid}",
        data={"station_no": "30501200", "obs_date": "not-a-date", "rainfall_mm": "4"},
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
