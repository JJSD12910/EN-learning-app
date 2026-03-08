import json
import sqlite3
import uuid

from flask import Flask, g, request
from sqlalchemy import func
from werkzeug.exceptions import HTTPException

from .api_response import api_error, is_api_path
from .auth import resolve_user
from .db import Base, DATA_DIR, DB_FILE, engine, get_session
from .migrations import run_migrations
from .models import ClientUser, Question, Record, User
from .routes import bp


def ensure_user_role_column():
    """Make sure users table has a role column (backwards-compatible)."""
    if not DB_FILE.exists():
        return
    con = sqlite3.connect(DB_FILE)
    try:
        cur = con.cursor()
        cur.execute("pragma table_info(users)")
        cols = [row[1] for row in cur.fetchall()]
        if "role" not in cols:
            cur.execute("ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'admin'")
            con.commit()
        cur.execute("UPDATE users SET role='admin' WHERE role IS NULL")
        con.commit()
    finally:
        con.close()


def import_if_empty():
    session = get_session()
    try:
        if session.query(func.count(User.id)).scalar() == 0:
            from_file = DATA_DIR / "server_users.json"
            if from_file.exists():
                users = json.loads(from_file.read_text(encoding="utf-8"))
                for u in users:
                    session.add(User(username=u.get("username"), password=u.get("password"), role=u.get("role") or "admin"))
            else:
                session.add(User(username="admin", password="admin123", role="admin"))
            session.commit()
        if session.query(func.count(ClientUser.id)).scalar() == 0:
            from_file = DATA_DIR / "client_users.json"
            if from_file.exists():
                users = json.loads(from_file.read_text(encoding="utf-8"))
                for u in users:
                    session.add(ClientUser(username=u.get("username"), password=u.get("password")))
            else:
                session.add(ClientUser(username="001", password="666"))
            session.commit()
        if session.query(func.count(Question.id)).scalar() == 0:
            from_file = DATA_DIR / "questions.json"
            if from_file.exists():
                questions = json.loads(from_file.read_text(encoding="utf-8"))
                if isinstance(questions, dict):
                    questions = questions.get("questions", [])
                for q in questions:
                    session.add(
                        Question(
                            id=q.get("id"),
                            stem=q.get("stem"),
                            options=json.dumps(q.get("options", []), ensure_ascii=False),
                            answer=int(q.get("answer")),
                            category=q.get("category"),
                            analysis=q.get("analysis"),
                        )
                    )
                session.commit()
        if session.query(func.count(Record.id)).scalar() == 0:
            from_file = DATA_DIR / "exam_records.json"
            if from_file.exists():
                try:
                    records = json.loads(from_file.read_text(encoding="utf-8"))
                    for r in records:
                        record_id = r.get("id") or uuid.uuid4().hex
                        session.add(
                            Record(
                                id=record_id,
                                timestamp=r.get("timestamp") or "",
                                client_ip=r.get("client_ip"),
                                user_id=r.get("user_id") or "",
                                quiz_id=r.get("quiz_id") or "",
                                score=int(r.get("score") or 0),
                                total=int(r.get("total") or 0),
                                wrong=json.dumps(r.get("wrong") or [], ensure_ascii=False),
                            )
                        )
                    session.commit()
                except Exception:
                    session.rollback()
        teacher_exists = session.query(User).filter(User.role == "teacher").first()
        if not teacher_exists:
            session.add(User(username="teacher", password="teacher123", role="teacher"))
            session.commit()
        assistant_exists = session.query(User).filter(User.role == "assistant").first()
        if not assistant_exists:
            session.add(User(username="assistant", password="assistant123", role="assistant"))
            session.commit()
    finally:
        session.close()


def create_app():
    app = Flask(__name__, static_folder=None, template_folder=None)
    app.config["JSON_AS_ASCII"] = False

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ensure_user_role_column()
    Base.metadata.create_all(bind=engine)
    run_migrations()
    import_if_empty()

    @app.before_request
    def attach_db():
        g.db = get_session()
        user_info = resolve_user(g.db, request)
        if user_info:
            g.current_user = user_info["username"]
            g.current_role = user_info["role"]
        else:
            g.current_user = None
            g.current_role = None

    @app.after_request
    def add_cors(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        return response

    @app.errorhandler(HTTPException)
    def handle_http_exception(err):
        if is_api_path(request.path):
            return api_error(err.description or "request error", status=err.code or 500)
        return err

    @app.errorhandler(Exception)
    def handle_unexpected_exception(err):
        if is_api_path(request.path):
            app.logger.exception("Unhandled API error", exc_info=err)
            return api_error("internal server error", status=500)
        raise err

    @app.teardown_request
    def teardown_db(exc=None):
        db = getattr(g, "db", None)
        if db is not None:
            db.close()

    app.register_blueprint(bp)
    return app
