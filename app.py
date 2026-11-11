from __future__ import annotations

import csv
import io
import os
import secrets
import sqlite3
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent

STATUS_LABELS = {
    "pending": "待两份录入",
    "conflict": "有出入 · 待复核",
    "agreed": "两份一致 · 已核定",
    "adjudicated": "复核选定 · 已核定",
}

DEMO_ACCOUNTS = [
    ("clerk1", "录入员甲", "clerk"),
    ("clerk2", "录入员乙", "clerk"),
    ("reviewer", "复核员", "reviewer"),
]
DEMO_PASSWORD = "demo1234"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "demo-only-change-me-in-production"),
        DATABASE=os.environ.get("DATABASE_PATH", str(BASE_DIR / "data" / "rainfall.db")),
        UPLOAD_DIR=os.environ.get("UPLOAD_DIR", str(BASE_DIR / "data" / "uploads")),
        SAMPLE_CARD_DIR=str(BASE_DIR / "sample_cards"),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0")
        .lower()
        in {"1", "true", "yes"},
    )
    if test_config:
        app.config.update(test_config)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)

    app.teardown_appcontext(close_db)

    with app.app_context():
        init_db()
        seed_demo_data()

    register_routes(app)
    register_template_helpers(app)
    register_error_handlers(app)
    return app


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        g.db = conn
    return g.db


def close_db(_error=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db() -> None:
    db = get_db()
    db.executescript((BASE_DIR / "schema.sql").read_text(encoding="utf-8"))
    db.execute("PRAGMA journal_mode = WAL")
    db.commit()


def seed_demo_data() -> None:
    """Idempotently add the demo accounts, cards and entries on a fresh volume."""
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] != 0:
        return

    now = utc_now()
    user_ids: dict[str, int] = {}
    for username, display_name, role in DEMO_ACCOUNTS:
        cur = db.execute(
            """INSERT INTO users (username, password_hash, display_name, role, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                username,
                generate_password_hash(DEMO_PASSWORD),
                display_name,
                role,
                now,
            ),
        )
        user_ids[username] = cur.lastrowid

    reviewer_id = user_ids["reviewer"]
    clerk1_id = user_ids["clerk1"]
    clerk2_id = user_ids["clerk2"]

    # The four paper cards deliberately show the states a new archive batch can
    # have: empty, one-sided, in conflict, and already agreed.
    seed_cards = [
        ("YC-1986-0714", "sample:rain-card-001.svg"),
        ("YC-1986-0715", "sample:rain-card-002.svg"),
        ("YC-1986-0716", "sample:rain-card-003.svg"),
        ("YC-1986-0717", "sample:rain-card-004.svg"),
    ]
    card_ids: dict[str, int] = {}
    for card_no, image_filename in seed_cards:
        cur = db.execute(
            """INSERT INTO cards
               (card_no, image_filename, clerk_a_id, clerk_b_id, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (card_no, image_filename, clerk1_id, clerk2_id, reviewer_id, now),
        )
        card_ids[card_no] = cur.lastrowid

    def add_entry(card_no: str, clerk_id: int, station: str, obs_date: str, rain: str):
        db.execute(
            """INSERT INTO entries
               (card_id, clerk_id, station_no, obs_date, rainfall_mm,
                revision, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
            (card_ids[card_no], clerk_id, station, obs_date, rain, now, now),
        )

    # 002: a real disagreement to demonstrate review and rejection of stale work.
    add_entry("YC-1986-0715", clerk1_id, "30501210", "1986-07-15", "31")
    add_entry("YC-1986-0715", clerk2_id, "30501210", "1986-07-15", "13")
    # 003: both clerks read the card correctly.
    add_entry("YC-1986-0716", clerk1_id, "30501220", "1986-07-16", "0")
    add_entry("YC-1986-0716", clerk2_id, "30501220", "1986-07-16", "0")
    # 004: waiting for the second clerk.
    add_entry("YC-1986-0717", clerk1_id, "30501230", "1986-07-17", "88")

    db.commit()


# ---------------------------------------------------------------------------
# Authentication / authorisation
# ---------------------------------------------------------------------------


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("请先登录。", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def role_required(role: str):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                flash("请先登录。", "warning")
                return redirect(url_for("login", next=request.path))
            if g.user["role"] != role:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def can_view_card(user, card) -> bool:
    if user is None:
        return False
    if user["role"] == "reviewer":
        return True
    return user["id"] in (card["clerk_a_id"], card["clerk_b_id"])


# ---------------------------------------------------------------------------
# Card entries and adjudication state
# ---------------------------------------------------------------------------


def entry_values(entry) -> tuple[str, str, str] | None:
    if entry is None:
        return None
    return (entry["station_no"], entry["obs_date"], entry["rainfall_mm"])


def get_entry(db, card_id: int, clerk_id: int):
    return db.execute(
        "SELECT * FROM entries WHERE card_id = ? AND clerk_id = ?",
        (card_id, clerk_id),
    ).fetchone()


def get_card_entries(db, card):
    return (
        get_entry(db, card["id"], card["clerk_a_id"]),
        get_entry(db, card["id"], card["clerk_b_id"]),
    )


def get_active_decision(db, card_id: int):
    return db.execute(
        """SELECT * FROM decisions
           WHERE card_id = ? AND voided_at IS NULL
           ORDER BY id DESC LIMIT 1""",
        (card_id,),
    ).fetchone()


def decision_matches_entries(decision, entry_a, entry_b) -> bool:
    if decision is None or decision["voided_at"] is not None:
        return False
    if entry_a is None or entry_b is None:
        return False
    if decision["entry_a_id"] != entry_a["id"]:
        return False
    if decision["entry_a_revision"] != entry_a["revision"]:
        return False
    if decision["entry_b_id"] != entry_b["id"]:
        return False
    if decision["entry_b_revision"] != entry_b["revision"]:
        return False
    selected = entry_a if decision["selected_entry_id"] == entry_a["id"] else entry_b
    return (
        decision["selected_entry_id"] in (entry_a["id"], entry_b["id"])
        and decision["selected_entry_revision"] == selected["revision"]
    )


def resolve_card(entry_a, entry_b, active_decision) -> dict:
    """The single source of truth for approved/conflicting/pending state."""
    if entry_a is None or entry_b is None:
        return {
            "status": "pending",
            "approved": False,
            "method": None,
            "values": None,
            "verified_at": None,
            "decision": None,
            "approved_entry": None,
        }

    if entry_values(entry_a) == entry_values(entry_b):
        verified_at = max(entry_a["updated_at"], entry_b["updated_at"])
        return {
            "status": "agreed",
            "approved": True,
            "method": "double_entry_agreement",
            "values": entry_values(entry_a),
            "verified_at": verified_at,
            "decision": None,
            "approved_entry": entry_a,
        }

    if decision_matches_entries(active_decision, entry_a, entry_b):
        selected = (
            entry_a
            if active_decision["selected_entry_id"] == entry_a["id"]
            else entry_b
        )
        return {
            "status": "adjudicated",
            "approved": True,
            "method": "reviewer_adjudication",
            "values": (
                active_decision["verified_station_no"],
                active_decision["verified_obs_date"],
                active_decision["verified_rainfall_mm"],
            ),
            "verified_at": active_decision["created_at"],
            "decision": active_decision,
            "approved_entry": selected,
        }

    return {
        "status": "conflict",
        "approved": False,
        "method": None,
        "values": None,
        "verified_at": None,
        "decision": active_decision,
        "approved_entry": None,
    }


def void_active_decisions(db, card_id: int, at: str, entry_id: int) -> None:
    """An edit to either entry permanently revokes the card's current award."""
    db.execute(
        """UPDATE decisions
           SET voided_at = ?, voided_reason = 'entry_edited',
               voided_by_entry_id = ?
           WHERE card_id = ? AND voided_at IS NULL""",
        (at, entry_id, card_id),
    )


def card_decision_history(db, card_id: int):
    return db.execute(
        """SELECT d.*,
                  r.display_name AS reviewer_name,
                  e.clerk_id AS selected_clerk_id,
                  u.display_name AS selected_clerk_name
           FROM decisions d
           JOIN users r ON r.id = d.reviewer_id
           LEFT JOIN entries e ON e.id = d.selected_entry_id
           LEFT JOIN users u ON u.id = e.clerk_id
           WHERE d.card_id = ?
           ORDER BY d.id DESC""",
        (card_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def parse_entry_form(form) -> tuple[dict, dict, dict]:
    """Return (normalised data, values for redisplay, field errors)."""
    raw_station = (form.get("station_no") or "").strip()
    raw_date = (form.get("obs_date") or "").strip()
    raw_rain = (form.get("rainfall_mm") or "").strip()
    displayed = {
        "station_no": raw_station,
        "obs_date": raw_date,
        "rainfall_mm": raw_rain,
    }
    errors: dict[str, str] = {}

    if not raw_station:
        errors["station_no"] = "请填写站号。"
    elif len(raw_station) > 40:
        errors["station_no"] = "站号不能超过 40 个字符。"
    elif any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_station):
        errors["station_no"] = "站号含有不允许的控制字符。"

    normal_date = ""
    if not raw_date:
        errors["obs_date"] = "请选择观测日。"
    else:
        try:
            normal_date = date.fromisoformat(raw_date.replace("/", "-")).isoformat()
        except ValueError:
            errors["obs_date"] = "观测日格式应为 YYYY-MM-DD。"

    normal_rain = ""
    if not raw_rain:
        errors["rainfall_mm"] = "请填写雨量。"
    else:
        try:
            amount = Decimal(raw_rain)
            if not amount.is_finite():
                raise InvalidOperation
            if amount < 0:
                errors["rainfall_mm"] = "雨量不能为负数。"
            elif amount > Decimal("100000"):
                errors["rainfall_mm"] = "雨量超出合理范围。"
            else:
                normal_rain = format(amount, "f")
                if "." in normal_rain:
                    normal_rain = normal_rain.rstrip("0").rstrip(".")
                if normal_rain in ("", "-0"):
                    normal_rain = "0"
        except (InvalidOperation, ValueError):
            errors["rainfall_mm"] = "雨量必须是毫米数值，例如 42.5。"

    data = {
        "station_no": raw_station,
        "obs_date": normal_date,
        "rainfall_mm": normal_rain,
    }
    return data, displayed, errors


def sniff_image(head: bytes) -> tuple[str, str] | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def register_routes(app: Flask) -> None:
    @app.before_request
    def load_logged_in_user():
        user_id = session.get("user_id")
        g.user = (
            get_db()
            .execute("SELECT * FROM users WHERE id = ?", (user_id,))
            .fetchone()
            if user_id is not None
            else None
        )

    @app.context_processor
    def inject_globals():
        return {"status_labels": STATUS_LABELS, "current_user": g.get("user")}

    @app.get("/")
    def index():
        if g.user is None:
            return redirect(url_for("login"))
        if g.user["role"] == "reviewer":
            return redirect(url_for("reviewer_dashboard"))
        return redirect(url_for("clerk_dashboard"))

    @app.route("/login", methods=("GET", "POST"))
    def login():
        if g.user is not None:
            return redirect(url_for("index"))
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            user = get_db().execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
            if user is None or not check_password_hash(user["password_hash"], password):
                flash("账号或密码不正确。", "error")
            else:
                session.clear()
                session["user_id"] = user["id"]
                flash(f"已登录：{user['display_name']}。", "success")
                next_url = request.args.get("next")
                if next_url and next_url.startswith("/") and not next_url.startswith("//"):
                    return redirect(next_url)
                return redirect(url_for("index"))
        return render_template("login.html", demo_accounts=DEMO_ACCOUNTS)

    @app.post("/logout")
    def logout():
        session.clear()
        flash("已退出登录。", "success")
        return redirect(url_for("login"))

    @app.get("/clerk")
    @role_required("clerk")
    def clerk_dashboard():
        db = get_db()
        cards = db.execute(
            """SELECT * FROM cards
               WHERE clerk_a_id = ? OR clerk_b_id = ?
               ORDER BY id DESC""",
            (g.user["id"], g.user["id"]),
        ).fetchall()
        card_rows = []
        for card in cards:
            entry_a, entry_b = get_card_entries(db, card)
            own = entry_a if card["clerk_a_id"] == g.user["id"] else entry_b
            info = resolve_card(entry_a, entry_b, get_active_decision(db, card["id"]))
            card_rows.append({"card": card, "own_entry": own, "info": info})
        return render_template("clerk_dashboard.html", card_rows=card_rows)

    @app.route("/clerk/cards/<int:card_id>", methods=("GET", "POST"))
    @role_required("clerk")
    def clerk_entry(card_id: int):
        db = get_db()
        card = db.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        if card is None:
            abort(404)
        if g.user["id"] not in (card["clerk_a_id"], card["clerk_b_id"]):
            abort(403)

        own_entry = get_entry(db, card_id, g.user["id"])
        values = {
            "station_no": own_entry["station_no"] if own_entry else "",
            "obs_date": own_entry["obs_date"] if own_entry else "",
            "rainfall_mm": own_entry["rainfall_mm"] if own_entry else "",
        }
        errors: dict[str, str] = {}

        if request.method == "POST":
            data, values, errors = parse_entry_form(request.form)
            if not errors:
                now = utc_now()
                db.execute("BEGIN IMMEDIATE")
                try:
                    # Re-read under the write lock so an update is never lost.
                    current_own = get_entry(db, card_id, g.user["id"])
                    if current_own is None:
                        cur = db.execute(
                            """INSERT INTO entries
                               (card_id, clerk_id, station_no, obs_date, rainfall_mm,
                                revision, created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                            (
                                card_id,
                                g.user["id"],
                                data["station_no"],
                                data["obs_date"],
                                data["rainfall_mm"],
                                now,
                                now,
                            ),
                        )
                        entry_id = cur.lastrowid
                    else:
                        db.execute(
                            """UPDATE entries
                               SET station_no = ?, obs_date = ?, rainfall_mm = ?,
                                   revision = revision + 1, updated_at = ?
                               WHERE id = ?""",
                            (
                                data["station_no"],
                                data["obs_date"],
                                data["rainfall_mm"],
                                now,
                                current_own["id"],
                            ),
                        )
                        entry_id = current_own["id"]
                    # Every save is a new version. Even a save that happens to
                    # repeat the same text revokes an earlier adjudication.
                    void_active_decisions(db, card_id, now, entry_id)
                    db.commit()
                except Exception:
                    db.rollback()
                    raise

                entry_a, entry_b = get_card_entries(db, card)
                result = resolve_card(
                    entry_a, entry_b, get_active_decision(db, card_id)
                )
                if result["status"] == "agreed":
                    flash("录入已保存。最新两份完全一致，卡片已自动核定。", "success")
                elif result["status"] == "conflict":
                    flash("录入已保存。最新两份有出入，等待复核员对照原图裁定。", "warning")
                else:
                    flash("录入已保存，等待另一位录入员。", "success")
                return redirect(url_for("clerk_entry", card_id=card_id))

        entry_a, entry_b = get_card_entries(db, card)
        own_entry = entry_a if card["clerk_a_id"] == g.user["id"] else entry_b
        info = resolve_card(entry_a, entry_b, get_active_decision(db, card_id))
        return render_template(
            "entry_form.html",
            card=card,
            own_entry=own_entry,
            info=info,
            values=values,
            errors=errors,
        )

    @app.get("/reviewer")
    @role_required("reviewer")
    def reviewer_dashboard():
        db = get_db()
        cards = db.execute(
            """SELECT c.*,
                      ua.display_name AS clerk_a_name,
                      ub.display_name AS clerk_b_name
               FROM cards c
               JOIN users ua ON ua.id = c.clerk_a_id
               JOIN users ub ON ub.id = c.clerk_b_id
               ORDER BY c.id DESC"""
        ).fetchall()
        clerks = db.execute(
            "SELECT * FROM users WHERE role = 'clerk' ORDER BY id"
        ).fetchall()
        card_rows = []
        for card in cards:
            entry_a, entry_b = get_card_entries(db, card)
            info = resolve_card(entry_a, entry_b, get_active_decision(db, card["id"]))
            card_rows.append(
                {
                    "card": card,
                    "entry_a": entry_a,
                    "entry_b": entry_b,
                    "info": info,
                }
            )

        selected_status = request.args.get("status", "")
        counts = {key: 0 for key in STATUS_LABELS}
        for row in card_rows:
            counts[row["info"]["status"]] += 1
        if selected_status in STATUS_LABELS:
            card_rows = [r for r in card_rows if r["info"]["status"] == selected_status]

        return render_template(
            "reviewer_dashboard.html",
            cards=cards,
            card_rows=card_rows,
            clerks=clerks,
            counts=counts,
            selected_status=selected_status,
        )

    @app.post("/reviewer/upload")
    @role_required("reviewer")
    def upload_card():
        db = get_db()
        card_no = (request.form.get("card_no") or "").strip()
        clerk_a_raw = request.form.get("clerk_a_id") or ""
        clerk_b_raw = request.form.get("clerk_b_id") or ""

        if not card_no:
            card_no = f"YC-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}"
        if len(card_no) > 60 or any(ord(ch) < 32 or ord(ch) == 127 for ch in card_no):
            flash("卡片编号不合法。", "error")
            return redirect(url_for("reviewer_dashboard"))

        if not clerk_a_raw.isdigit() or not clerk_b_raw.isdigit():
            flash("请选择两位不同的录入员。", "error")
            return redirect(url_for("reviewer_dashboard"))
        clerk_a_id, clerk_b_id = int(clerk_a_raw), int(clerk_b_raw)
        if clerk_a_id == clerk_b_id:
            flash("两位录入员不能是同一人。", "error")
            return redirect(url_for("reviewer_dashboard"))
        clerk_rows = db.execute(
            "SELECT id FROM users WHERE role = 'clerk' AND id IN (?, ?)",
            (clerk_a_id, clerk_b_id),
        ).fetchall()
        if len(clerk_rows) != 2:
            flash("录入员账号无效。", "error")
            return redirect(url_for("reviewer_dashboard"))

        scan = request.files.get("scan")
        if scan is None or not scan.filename:
            flash("请选择要上传的扫描图。", "error")
            return redirect(url_for("reviewer_dashboard"))
        head = scan.stream.read(16)
        scan.stream.seek(0)
        kind = sniff_image(head)
        if kind is None:
            flash("扫描图须为 PNG、JPEG、GIF 或 WebP 图片。", "error")
            return redirect(url_for("reviewer_dashboard"))

        ext, _mimetype = kind
        filename = f"{uuid.uuid4().hex}{ext}"
        destination = Path(app.config["UPLOAD_DIR"]) / filename
        scan.save(destination)

        try:
            cur = db.execute(
                """INSERT INTO cards
                   (card_no, image_filename, clerk_a_id, clerk_b_id, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    card_no,
                    filename,
                    clerk_a_id,
                    clerk_b_id,
                    g.user["id"],
                    utc_now(),
                ),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            destination.unlink(missing_ok=True)
            flash("卡片编号已存在，请换一个编号。", "error")
            return redirect(url_for("reviewer_dashboard"))

        flash(f"卡片 {card_no} 已建立，两位录入员可以开始录入。", "success")
        return redirect(url_for("reviewer_card", card_id=cur.lastrowid))

    @app.get("/reviewer/cards/<int:card_id>")
    @role_required("reviewer")
    def reviewer_card(card_id: int):
        return render_review_card(card_id)

    def render_review_card(card_id: int, error: str | None = None, http_status: int = 200):
        db = get_db()
        card = db.execute(
            """SELECT c.*,
                      ua.display_name AS clerk_a_name,
                      ub.display_name AS clerk_b_name
               FROM cards c
               JOIN users ua ON ua.id = c.clerk_a_id
               JOIN users ub ON ub.id = c.clerk_b_id
               WHERE c.id = ?""",
            (card_id,),
        ).fetchone()
        if card is None:
            abort(404)
        entry_a, entry_b = get_card_entries(db, card)
        active = get_active_decision(db, card_id)
        info = resolve_card(entry_a, entry_b, active)
        differences = set()
        if entry_a is not None and entry_b is not None:
            if entry_a["station_no"] != entry_b["station_no"]:
                differences.add("station_no")
            if entry_a["obs_date"] != entry_b["obs_date"]:
                differences.add("obs_date")
            if entry_a["rainfall_mm"] != entry_b["rainfall_mm"]:
                differences.add("rainfall_mm")
        history = card_decision_history(db, card_id)
        response = render_template(
            "review_card.html",
            card=card,
            entry_a=entry_a,
            entry_b=entry_b,
            info=info,
            active=active,
            differences=differences,
            history=history,
            decision_error=error,
        )
        return response, http_status

    @app.post("/reviewer/cards/<int:card_id>/decide")
    @role_required("reviewer")
    def decide_card(card_id: int):
        db = get_db()

        def reject(message: str):
            db.rollback()
            return render_review_card(card_id, error=message, http_status=409)

        # These IDs/revisions bind the submitted decision to precisely the two
        # versions the reviewer saw. If either changed, the decision is refused.
        expected_a_id = request.form.get("entry_a_id", type=int)
        expected_a_revision = request.form.get("entry_a_revision", type=int)
        expected_b_id = request.form.get("entry_b_id", type=int)
        expected_b_revision = request.form.get("entry_b_revision", type=int)
        selected_entry_id = request.form.get("selected_entry_id", type=int)

        db.execute("BEGIN IMMEDIATE")
        try:
            card = db.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
            if card is None:
                db.rollback()
                abort(404)
            entry_a, entry_b = get_card_entries(db, card)

            if entry_a is None or entry_b is None:
                return reject("这份卡片还没有两份录入，不能裁定。")
            if None in (
                expected_a_id,
                expected_a_revision,
                expected_b_id,
                expected_b_revision,
                selected_entry_id,
            ):
                return reject("裁定依据不完整，请重新打开卡片后再试。")

            if (
                expected_a_id != entry_a["id"]
                or expected_a_revision != entry_a["revision"]
                or expected_b_id != entry_b["id"]
                or expected_b_revision != entry_b["revision"]
            ):
                return reject("页面中的录入已被更新，旧裁定已拒绝。请以最新两份重新核对后再裁定。")

            if get_active_decision(db, card_id) is not None:
                return reject("这份卡片已有有效裁定。若录入员随后改填，请以新的版本重新裁定。")

            if entry_values(entry_a) == entry_values(entry_b):
                return reject("最新两份录入已经一致，无需裁定，系统已自动核定。")

            if selected_entry_id not in (entry_a["id"], entry_b["id"]):
                return reject("只能选择当前两份录入中的一份。")

            selected = entry_a if selected_entry_id == entry_a["id"] else entry_b
            now = utc_now()
            db.execute(
                """INSERT INTO decisions
                   (card_id, reviewer_id,
                    entry_a_id, entry_a_revision, entry_b_id, entry_b_revision,
                    selected_entry_id, selected_entry_revision,
                    verified_station_no, verified_obs_date, verified_rainfall_mm,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    card_id,
                    g.user["id"],
                    entry_a["id"],
                    entry_a["revision"],
                    entry_b["id"],
                    entry_b["revision"],
                    selected["id"],
                    selected["revision"],
                    selected["station_no"],
                    selected["obs_date"],
                    selected["rainfall_mm"],
                    now,
                ),
            )
            db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise

        flash("已对照原图完成核定。", "success")
        return redirect(url_for("reviewer_card", card_id=card_id))

    @app.get("/reviewer/export.csv")
    @role_required("reviewer")
    def export_csv():
        db = get_db()
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            [
                "card_no",
                "station_no",
                "observation_date",
                "rainfall_mm",
                "verification",
                "verified_at",
            ]
        )
        db.execute("BEGIN")
        try:
            cards = db.execute("SELECT * FROM cards ORDER BY id").fetchall()
            for card in cards:
                entry_a, entry_b = get_card_entries(db, card)
                info = resolve_card(
                    entry_a, entry_b, get_active_decision(db, card["id"])
                )
                if not info["approved"]:
                    continue
                writer.writerow(
                    [
                        card["card_no"],
                        info["values"][0],
                        info["values"][1],
                        info["values"][2],
                        info["method"],
                        info["verified_at"],
                    ]
                )
        finally:
            db.rollback()
        filename = f"verified-rainfall-{datetime.now(timezone.utc):%Y%m%d}.csv"
        return app.response_class(
            output.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Type": "text/csv; charset=utf-8",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.get("/cards/<int:card_id>/image")
    @login_required
    def card_image(card_id: int):
        db = get_db()
        card = db.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        if card is None:
            abort(404)
        if not can_view_card(g.user, card):
            abort(403)
        stored = card["image_filename"]
        if stored.startswith("sample:"):
            response = send_from_directory(
                app.config["SAMPLE_CARD_DIR"], stored.split(":", 1)[1]
            )
        else:
            response = send_from_directory(app.config["UPLOAD_DIR"], stored)
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response


def register_template_helpers(app: Flask) -> None:
    @app.template_filter("short_dt")
    def short_dt(value):
        if not value:
            return "—"
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value

    @app.template_filter("status_label")
    def status_label(value):
        return STATUS_LABELS.get(value, value)


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(403)
    def forbidden(_error):
        return render_template("error.html", code=403, title="无权访问", message="当前账号不能执行这个操作。"), 403

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, title="页面不存在", message="要查找的卡片或页面不存在。"), 404

    @app.errorhandler(413)
    def too_large(_error):
        return (
            render_template(
                "error.html",
                code=413,
                title="文件过大",
                message="上传的扫描图不能超过 16 MB。",
            ),
            413,
        )

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        return response


app = create_app()
