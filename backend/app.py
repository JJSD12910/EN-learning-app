import json
import sqlite3
import uuid
from flask import Flask, g, request
from sqlalchemy import func
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash

from .api_response import api_error, is_api_path
from .auth import ensure_password_hash, is_password_hashed, resolve_user
from .db import Base, DATA_DIR, DB_FILE, engine, get_session
from .migrations import run_migrations
from .models import ClientUser, Question, Record, User
from .routes import bp


DEFAULT_SERVER_PASSWORDS = {
    "2025": "2025",
    "admin": "admin123",
    "assistant": "assistant123",
    "teacher": "teacher123",
}
DEFAULT_CLIENT_PASSWORDS = {
    "001": "666",
}


def resolve_seed_password(username, provided_password, defaults):
    raw_password = str(provided_password or '').strip()
    if raw_password:
        return raw_password
    return defaults.get(username, '')


def password_matches(stored_password, raw_password):
    stored = str(stored_password or '')
    candidate = str(raw_password or '')
    if not stored or not candidate:
        return False
    if is_password_hashed(stored):
        return check_password_hash(stored, candidate)
    return stored == candidate


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
            from_file = DATA_DIR / 'server_users.json'
            if from_file.exists():
                users = json.loads(from_file.read_text(encoding='utf-8'))
                for u in users:
                    username = str(u.get('username') or '').strip()
                    if not username:
                        continue
                    raw_password = resolve_seed_password(username, u.get('password'), DEFAULT_SERVER_PASSWORDS) or username
                    session.add(
                        User(
                            username=username,
                            password=ensure_password_hash(raw_password),
                            role=u.get('role') or 'admin',
                        )
                    )
            else:
                session.add(User(username='admin', password=ensure_password_hash(DEFAULT_SERVER_PASSWORDS['admin']), role='admin'))
            session.commit()
        if session.query(func.count(ClientUser.id)).scalar() == 0:
            from_file = DATA_DIR / 'client_users.json'
            if from_file.exists():
                users = json.loads(from_file.read_text(encoding='utf-8'))
                for u in users:
                    username = str(u.get('username') or '').strip()
                    if not username:
                        continue
                    raw_password = resolve_seed_password(username, u.get('password'), DEFAULT_CLIENT_PASSWORDS) or username
                    session.add(ClientUser(username=username, password=ensure_password_hash(raw_password)))
            else:
                session.add(ClientUser(username='001', password=ensure_password_hash(DEFAULT_CLIENT_PASSWORDS['001'])))
            session.commit()
        if session.query(func.count(Question.id)).scalar() == 0:
            from_file = DATA_DIR / 'questions.json'
            if from_file.exists():
                questions = json.loads(from_file.read_text(encoding='utf-8'))
                if isinstance(questions, dict):
                    questions = questions.get('questions', [])
                for q in questions:
                    session.add(
                        Question(
                            id=q.get('id'),
                            stem=q.get('stem'),
                            options=json.dumps(q.get('options', []), ensure_ascii=False),
                            answer=int(q.get('answer')),
                            category=q.get('category'),
                            analysis=q.get('analysis'),
                        )
                    )
                session.commit()
        if session.query(func.count(Record.id)).scalar() == 0:
            from_file = DATA_DIR / 'exam_records.json'
            if from_file.exists():
                try:
                    records = json.loads(from_file.read_text(encoding='utf-8'))
                    for r in records:
                        record_id = r.get('id') or uuid.uuid4().hex
                        session.add(
                            Record(
                                id=record_id,
                                timestamp=r.get('timestamp') or '',
                                client_ip=r.get('client_ip'),
                                user_id=r.get('user_id') or '',
                                quiz_id=r.get('quiz_id') or '',
                                score=int(r.get('score') or 0),
                                total=int(r.get('total') or 0),
                                wrong=json.dumps(r.get('wrong') or [], ensure_ascii=False),
                            )
                        )
                    session.commit()
                except Exception:
                    session.rollback()
        teacher_exists = session.query(User).filter(User.role == 'teacher').first()
        if not teacher_exists:
            session.add(User(username='teacher', password=ensure_password_hash(DEFAULT_SERVER_PASSWORDS['teacher']), role='teacher'))
            session.commit()
        assistant_exists = session.query(User).filter(User.role == 'assistant').first()
        if not assistant_exists:
            session.add(User(username='assistant', password=ensure_password_hash(DEFAULT_SERVER_PASSWORDS['assistant']), role='assistant'))
            session.commit()
    finally:
        session.close()


def sync_seed_account_passwords():
    session = get_session()
    try:
        changed = False
        server_file = DATA_DIR / 'server_users.json'
        if server_file.exists():
            users = json.loads(server_file.read_text(encoding='utf-8'))
            for u in users:
                username = str(u.get('username') or '').strip()
                if not username:
                    continue
                raw_password = resolve_seed_password(username, u.get('password'), DEFAULT_SERVER_PASSWORDS) or username
                row = session.query(User).filter(User.username == username).first()
                if not row:
                    continue
                desired_role = str(u.get('role') or '').strip() or row.role or 'admin'
                if row.role != desired_role:
                    row.role = desired_role
                    changed = True
                if not password_matches(getattr(row, 'password', ''), raw_password):
                    row.password = ensure_password_hash(raw_password)
                    changed = True
        client_file = DATA_DIR / 'client_users.json'
        if client_file.exists():
            users = json.loads(client_file.read_text(encoding='utf-8'))
            for u in users:
                username = str(u.get('username') or '').strip()
                if not username:
                    continue
                raw_password = resolve_seed_password(username, u.get('password'), DEFAULT_CLIENT_PASSWORDS) or username
                row = session.query(ClientUser).filter(ClientUser.username == username).first()
                if not row:
                    continue
                if not password_matches(getattr(row, 'password', ''), raw_password):
                    row.password = ensure_password_hash(raw_password)
                    changed = True
        if changed:
            session.commit()
    finally:
        session.close()


def normalize_password_storage():
    session = get_session()
    try:
        changed = False
        for row in session.query(User).all():
            next_password = ensure_password_hash(getattr(row, 'password', ''))
            if next_password != row.password:
                row.password = next_password
                changed = True
        for row in session.query(ClientUser).all():
            next_password = ensure_password_hash(getattr(row, 'password', ''))
            if next_password != row.password:
                row.password = next_password
                changed = True
        if changed:
            session.commit()
    finally:
        session.close()


def create_app():
    app = Flask(__name__, static_folder=None, template_folder=None)
    app.config['JSON_AS_ASCII'] = False

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ensure_user_role_column()
    Base.metadata.create_all(bind=engine)
    run_migrations()
    import_if_empty()
    sync_seed_account_passwords()
    normalize_password_storage()

    @app.before_request
    def attach_db():
        g.db = get_session()
        user_info = resolve_user(g.db, request)
        if user_info:
            g.current_user = user_info['username']
            g.current_role = user_info['role']
        else:
            g.current_user = None
            g.current_role = None

    @app.after_request
    def add_cors(response):
        origin = (request.headers.get('Origin') or '').rstrip('/')
        current_origin = request.host_url.rstrip('/')
        if origin and origin == current_origin:
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, PATCH, DELETE, OPTIONS'
            response.headers['Vary'] = 'Origin'
        return response

    @app.errorhandler(HTTPException)
    def handle_http_exception(err):
        if is_api_path(request.path):
            return api_error(err.description or 'request error', status=err.code or 500)
        return err

    @app.errorhandler(Exception)
    def handle_unexpected_exception(err):
        if is_api_path(request.path):
            app.logger.exception('Unhandled API error', exc_info=err)
            return api_error('internal server error', status=500)
        raise err

    @app.teardown_request
    def teardown_db(exc=None):
        db = getattr(g, 'db', None)
        if db is not None:
            db.close()

    app.register_blueprint(bp)
    return app
