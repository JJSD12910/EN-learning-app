import time
import uuid
from functools import wraps
from typing import Optional, Tuple

from flask import g, jsonify, redirect, request
from sqlalchemy.orm import Session

from .models import ClientUser, SessionToken, User

SESSION_TTL_SECONDS = 24 * 3600


def _api_error_payload(status: int):
    if status == 401:
        return {"code": 40101, "message": "unauthorized", "data": {}}
    if status == 403:
        return {"code": 40301, "message": "forbidden", "data": {}}
    return {"code": 50001, "message": "internal_error", "data": {}}


def issue_session(db: Session, username: str) -> str:
    token = uuid.uuid4().hex
    db.merge(SessionToken(token=token, user=username, ts=time.time()))
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
    # refresh timestamp
    session.ts = time.time()
    db.commit()
    user_row = db.query(User).filter(User.username == session.user).first()
    if user_row:
        if int(getattr(user_row, "is_active", 1) or 0) != 1:
            db.delete(session)
            db.commit()
            return None
        return {"username": user_row.username, "role": user_row.role or "admin"}
    client_row = db.query(ClientUser).filter(ClientUser.username == session.user).first()
    if client_row:
        if int(getattr(client_row, "is_active", 1) or 0) != 1:
            db.delete(session)
            db.commit()
            return None
        return {"username": client_row.username, "role": "client"}
    return None


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
                # Not logged in; delegate to login flow
                if api:
                    return jsonify(_api_error_payload(401)), 401
                return redirect("/login")
            # Logged in but insufficient permission
            if api:
                return jsonify(_api_error_payload(403)), 403
            return redirect("/")

        return wrapper

    return decorator
