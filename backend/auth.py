import re
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Optional

from flask import g, jsonify, redirect, request
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

from .models import ClientUser, SessionToken, User

SESSION_TTL_SECONDS = 24 * 3600
HASHED_PASSWORD_PREFIXES = ("pbkdf2:", "scrypt:")


def _api_error_payload(status: int):
    if status == 401:
        return {"code": 40101, "message": "unauthorized", "data": {}}
    if status == 403:
        return {"code": 40301, "message": "forbidden", "data": {}}
    return {"code": 50001, "message": "internal_error", "data": {}}


def parse_validity_datetime(value, *, end_of_day: bool = False):
    text = str(value or "").strip()
    if not text:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        if end_of_day:
            text = f"{text}T23:59:59+00:00"
        else:
            text = f"{text}T00:00:00+00:00"
    elif text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def teacher_account_is_currently_valid(user_row, now=None):
    role = getattr(user_row, "role", None) or "admin"
    if role != "teacher":
        return True
    now_dt = now or datetime.now(timezone.utc)
    valid_from = parse_validity_datetime(getattr(user_row, "valid_from", None), end_of_day=False)
    valid_to = parse_validity_datetime(getattr(user_row, "valid_to", None), end_of_day=True)
    if valid_from and now_dt < valid_from:
        return False
    if valid_to and now_dt > valid_to:
        return False
    return True


def normalize_principal_type(value: Optional[str]) -> str:
    return "client" if str(value or "").strip().lower() == "client" else "user"


def is_password_hashed(value: Optional[str]) -> bool:
    text = str(value or "")
    return text.startswith(HASHED_PASSWORD_PREFIXES)


def ensure_password_hash(password: Optional[str]) -> str:
    text = str(password or "")
    if is_password_hashed(text):
        return text
    return generate_password_hash(text)


def verify_password(db: Session, row, raw_password: Optional[str]) -> bool:
    stored_password = str(getattr(row, "password", "") or "")
    candidate = str(raw_password or "")
    if not stored_password or not candidate:
        return False
    if is_password_hashed(stored_password):
        return check_password_hash(stored_password, candidate)
    if stored_password != candidate:
        return False
    row.password = ensure_password_hash(candidate)
    db.commit()
    return True


def apply_session_cookie(response, token: str, *, secure: bool):
    response.set_cookie(
        "session",
        token,
        httponly=True,
        samesite="Strict",
        secure=bool(secure),
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )
    return response


def clear_session_cookie(response, *, secure: bool):
    response.set_cookie(
        "session",
        "",
        expires=0,
        httponly=True,
        samesite="Strict",
        secure=bool(secure),
        path="/",
    )
    return response


def issue_session(db: Session, username: str, principal_type: str) -> str:
    token = uuid.uuid4().hex
    db.add(SessionToken(token=token, user=username, principal_type=normalize_principal_type(principal_type), ts=time.time()))
    db.commit()
    return token


def resolve_user(db: Session, req) -> Optional[dict]:
    token = None
    auth_header = req.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if not token:
        token = req.cookies.get("session")
    if not token:
        return None
    session = db.query(SessionToken).filter(SessionToken.token == token).first()
    if not session:
        return None
    if time.time() - session.ts > SESSION_TTL_SECONDS:
        db.delete(session)
        db.commit()
        return None
    session.ts = time.time()
    db.commit()
    principal_type = normalize_principal_type(getattr(session, "principal_type", None))
    if principal_type == "user":
        user_row = db.query(User).filter(User.username == session.user).first()
        if not user_row:
            return None
        if int(getattr(user_row, "is_active", 1) or 0) != 1 or not teacher_account_is_currently_valid(user_row):
            db.delete(session)
            db.commit()
            return None
        return {"username": user_row.username, "role": user_row.role or "admin"}
    client_row = db.query(ClientUser).filter(ClientUser.username == session.user).first()
    if not client_row:
        return None
    if int(getattr(client_row, "is_active", 1) or 0) != 1:
        db.delete(session)
        db.commit()
        return None
    return {"username": client_row.username, "role": "client"}


def login_required(api: bool = True):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if getattr(g, "current_user", None):
                return fn(*args, **kwargs)
            user_info = resolve_user(g.db, request)
            if not user_info:
                if api:
                    return jsonify(_api_error_payload(401)), 401
                return redirect("/login")
            g.current_user = user_info["username"]
            g.current_role = user_info["role"]
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def role_required(allowed_roles, api: bool = True):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            role = getattr(g, "current_role", None)
            if role in allowed_roles:
                return fn(*args, **kwargs)
            if getattr(g, "current_user", None) is None:
                if api:
                    return jsonify(_api_error_payload(401)), 401
                return redirect("/login")
            if api:
                return jsonify(_api_error_payload(403)), 403
            return redirect("/")

        return wrapper

    return decorator
