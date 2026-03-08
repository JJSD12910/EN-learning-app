import csv
import json
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Sequence
import random
from xml.sax.saxutils import escape

from flask import Blueprint, g, jsonify, redirect, request, send_from_directory
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .api_response import api_error, api_ok, parse_pagination
from .auth import (
    apply_session_cookie,
    clear_session_cookie,
    ensure_password_hash,
    issue_session,
    login_required,
    parse_validity_datetime,
    role_required,
    teacher_account_is_currently_valid,
    verify_password,
)
from .db import DATA_DIR, FRONTEND_DIR, STATIC_DIR
from .models import (
    Answer,
    AppSetting,
    Attempt,
    AuditLog,
    ClientUser,
    Exam,
    ExamQuestion,
    Question,
    Record,
    School,
    SchoolClass,
    SessionToken,
    StudentProfile,
    User,
    WrongQuestion,
)

DEFAULT_QUESTION_COUNT = 10
ACTIVE_QUIZZES: Dict[str, Dict[str, object]] = {}
EXPORT_DIR = DATA_DIR / "exports"
QUIZ_SESSION_TTL_SECONDS = 300
WRONG_CLEAR_STREAK = 3
WRONG_TRAINING_CONFIG_KEY = "wrong_training_v2"
DEFAULT_WRONG_TRAINING_CONFIG = {"daily_total_count": 10, "reinforcement_count": 3, "mastery_streak": WRONG_CLEAR_STREAK}
VALID_CLASS_STATUS = {"active", "dismissed"}
VALID_STUDENT_STATUS = {"active", "inactive"}
VALID_GENDERS = {"M", "F", "U"}
VALID_EXAM_STATUS = {"draft", "published", "active", "ended", "archived"}
RESET_ALL_TEST_DATA_CONFIRM = "RESET_ALL_TEST_DATA"

HTTP_ERROR_CODE_MAP = {
    400: 40001,
    401: 40101,
    403: 40301,
    404: 40401,
    422: 42201,
    409: 40901,
    500: 50001,
}

bp = Blueprint("quiz", __name__)


def question_to_dict(q: Question, include_answer: bool = False) -> Dict:
    payload = {
        "id": q.id,
        "stem": q.stem,
        "options": json.loads(q.options or "[]"),
        "category": getattr(q, "category", None),
        "analysis": getattr(q, "analysis", None),
    }
    if include_answer:
        payload["answer"] = q.answer
    return payload


def record_to_dict(r: Record) -> Dict:
    return {
        "id": r.id,
        "timestamp": r.timestamp,
        "client_ip": r.client_ip,
        "user_id": r.user_id,
        "quiz_id": r.quiz_id,
        "score": r.score,
        "total": r.total,
        "wrong": json.loads(r.wrong or "[]"),
    }


def school_to_dict(row: School) -> Dict:
    return {"id": row.id, "name": row.name}


def class_to_dict(row: SchoolClass) -> Dict:
    return {
        "id": row.id,
        "school_id": row.school_id,
        "grade": row.grade,
        "name": row.name,
        "teacher_username": row.teacher_username,
        "status": row.status,
        "created_at": row.created_at,
    }


def student_to_dict(row: StudentProfile, wrong_pool_active_count: Optional[int] = None) -> Dict:
    payload = {
        "id": row.id,
        "client_username": row.client_username,
        "school_id": row.school_id,
        "grade": row.grade,
        "class_id": row.class_id,
        "student_no": row.student_no,
        "name": row.name,
        "gender": row.gender,
        "status": row.status,
        "wrong_training_enabled": bool(int(getattr(row, "wrong_training_enabled", 0) or 0)),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if wrong_pool_active_count is not None:
        payload["wrong_pool_active_count"] = int(wrong_pool_active_count or 0)
    return payload


def exam_to_dict(row: Exam) -> Dict:
    return {
        "id": row.id,
        "title": row.title,
        "class_id": row.class_id,
        "created_by": row.created_by,
        "question_count": row.question_count,
        "category": row.category,
        "start_at": row.start_at,
        "end_at": row.end_at,
        "allow_multiple_attempts": bool(int(getattr(row, "allow_multiple_attempts", 0) or 0)),
        "type": getattr(row, "exam_type", "exam") or "exam",
        "target_student_id": getattr(row, "target_student_profile_id", None),
        "status": row.status,
        "created_at": row.created_at,
        "updated_at": getattr(row, "updated_at", None),
    }


def attempt_to_dict(row: Attempt) -> Dict:
    return {
        "id": row.id,
        "exam_id": row.exam_id,
        "student_profile_id": row.student_profile_id,
        "started_at": row.started_at,
        "submitted_at": row.submitted_at,
        "score": row.score,
        "total": row.total,
        "progress_count": int(getattr(row, "progress_count", 0) or 0),
        "duration_sec": row.duration_sec,
    }


def pick_questions(db: Session, count: int, category: Optional[str] = None) -> List[Question]:
    query = db.query(Question)
    if category:
        query = query.filter(Question.category == category)
    questions = query.all()
    if not questions:
        raise RuntimeError("Question bank is empty")
    count_val = DEFAULT_QUESTION_COUNT if count is None else max(1, int(count))
    count_val = min(count_val, len(questions))
    return random.sample(questions, count_val)


def validate_credentials(db: Session, username: str, password: str, model):
    username = (username or "").strip()
    password = (password or "").strip()
    if not username or not password:
        return False, None, None
    user = db.query(model).filter(model.username == username).first()
    if user is not None and hasattr(user, "is_active"):
        if int(getattr(user, "is_active", 1) or 0) != 1:
            return False, None, None
    if user and model is User and not teacher_account_is_currently_valid(user):
        return False, None, None
    if user and verify_password(db, user, password):
        role = getattr(user, "role", None) or ("admin" if model is User else "client")
        return True, username, role
    return False, None, None


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def normalize_validity_value(value, *, end_of_day: bool = False) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    dt = parse_validity_datetime(text, end_of_day=end_of_day)
    if not dt:
        return None
    return text


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso_z(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def parse_limit_offset(default_limit: int = 20, max_limit: int = 100):
    return parse_pagination(default_limit=default_limit, max_limit=max_limit)


def parse_admin_limit_offset():
    return parse_pagination(default_limit=20, max_limit=100)


def parse_is_active_filter(raw_value):
    if raw_value is None or str(raw_value).strip() == "":
        return None
    text = str(raw_value).strip().lower()
    if text in {"1", "true", "active", "enabled", "yes"}:
        return 1
    if text in {"0", "false", "inactive", "disabled", "no"}:
        return 0
    return None


def require_confirm_phrase(data: dict, expected: str, field: str = "confirm"):
    actual = str((data or {}).get(field) or "").strip()
    if actual != expected:
        return api_error(f"{field} mismatch", status=400, expected=expected)
    return None


def user_to_admin_dict(row: User) -> Dict:
    return {
        "id": row.id,
        "username": row.username,
        "role": row.role,
        "display_name": row.display_name,
        "is_active": int(row.is_active or 0),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "valid_from": getattr(row, "valid_from", None),
        "valid_to": getattr(row, "valid_to", None),
        "last_login_at": row.last_login_at,
        "is_within_validity": teacher_account_is_currently_valid(row),
    }


def client_user_to_admin_dict(row: ClientUser) -> Dict:
    return {
        "id": row.id,
        "username": row.username,
        "is_active": int(row.is_active or 0),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "last_login_at": row.last_login_at,
    }


def add_audit_log(action: str, target_type: Optional[str] = None, target_id: Optional[str] = None, detail: Optional[Dict] = None):
    actor_username = getattr(g, "current_user", None) or ""
    actor_role = getattr(g, "current_role", None) or ""
    detail_json = json.dumps(detail, ensure_ascii=False) if detail is not None else None
    g.db.add(
        AuditLog(
            id=uuid.uuid4().hex,
            actor_username=actor_username,
            actor_role=actor_role,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            detail_json=detail_json,
            created_at=utc_now_iso(),
        )
    )


def parse_time_window():
    from_raw = (request.args.get("from") or "").strip() or None
    to_raw = (request.args.get("to") or "").strip() or None
    from_dt = parse_iso_datetime(from_raw)
    to_dt = parse_iso_datetime(to_raw)
    if from_raw and not from_dt:
        return None, None, v1_error("invalid_params", status=400, reason="invalid from datetime")
    if to_raw and not to_dt:
        return None, None, v1_error("invalid_params", status=400, reason="invalid to datetime")
    if from_dt and to_dt and to_dt < from_dt:
        return None, None, v1_error("invalid_params", status=400, reason="to must be >= from")
    return from_dt, to_dt, None


def parse_question_ids(value):
    if value is None:
        return None, None
    ids = []
    if isinstance(value, str):
        ids = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        ids = [str(part).strip() for part in value]
    else:
        return None, v1_error("invalid_params", status=400, reason="question_ids must be array or comma-separated string")
    clean_ids = []
    seen = set()
    for qid in ids:
        if not qid or qid in seen:
            continue
        seen.add(qid)
        clean_ids.append(qid)
    if not clean_ids:
        return None, v1_error("invalid_params", status=400, reason="question_ids cannot be empty")
    return clean_ids, None


def get_exam_attempt_stats(exam_id: str) -> Dict[str, int]:
    attempts_total = g.db.query(Attempt).filter(Attempt.exam_id == exam_id).count()
    submitted_total = g.db.query(Attempt).filter(Attempt.exam_id == exam_id, Attempt.submitted_at.isnot(None)).count()
    return {"attempts_total": int(attempts_total), "submitted_total": int(submitted_total)}


def get_exam_effective_status(exam: Exam, now: Optional[datetime] = None) -> str:
    status = str(exam.status or "draft").strip().lower()
    if status == "active":
        return "active"
    if status != "published":
        return status
    now_dt = now or datetime.now(timezone.utc)
    start_dt = parse_iso_datetime(exam.start_at)
    end_dt = parse_iso_datetime(exam.end_at)
    if end_dt and now_dt > end_dt:
        return "ended"
    if start_dt and now_dt >= start_dt:
        return "active"
    return "published"


def build_exam_payload(exam: Exam, class_row: Optional[SchoolClass] = None, stats: Optional[Dict] = None) -> Dict:
    payload = {
        "exam_id": exam.id,
        "title": exam.title,
        "class_id": class_row.id if class_row else exam.class_id,
        "class_name": class_row.name if class_row else None,
        "category": exam.category,
        "question_count": exam.question_count,
        "start_at": exam.start_at,
        "end_at": exam.end_at,
        "status": exam.status,
        "effective_status": get_exam_effective_status(exam),
        "created_at": exam.created_at,
        "updated_at": getattr(exam, "updated_at", None),
        "allow_multiple_attempts": bool(int(getattr(exam, "allow_multiple_attempts", 0) or 0)),
        "type": exam.exam_type or "exam",
        "target_student_id": exam.target_student_profile_id,
    }
    if stats is not None:
        payload["stats"] = stats
    return payload


def lifecycle_error(message: str, status: int, reason: str, data: Optional[Dict] = None):
    return v1_error(message, status=status, reason=reason, data=data)


def v1_ok(data=None, status: int = 200):
    return jsonify({"code": 0, "message": "ok", "data": data if data is not None else {}}), status


def v1_error(message: str, status: int = 400, code: Optional[int] = None, reason: Optional[str] = None, data: Optional[Dict] = None):
    payload = {"code": int(code or HTTP_ERROR_CODE_MAP.get(status, 50001)), "message": message, "data": data or {}}
    if reason:
        payload["data"]["reason"] = reason
    return jsonify(payload), status


def normalized_accuracy(correct: int, total: int) -> float:
    if not total:
        return 0.0
    return round(float(correct) / float(total), 4)


def sanitize_filename_part(value: str, fallback: str) -> str:
    text = (value or "").strip()
    if not text:
        return fallback
    bad_chars = '<>:"/\\|?*'
    for ch in bad_chars:
        text = text.replace(ch, "_")
    return text[:80] or fallback


def excel_col_name(col_idx: int) -> str:
    idx = col_idx
    chars = []
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        chars.append(chr(ord("A") + rem))
    return "".join(reversed(chars))


def excel_cell(row_idx: int, col_idx: int, value) -> str:
    ref = f"{excel_col_name(col_idx)}{row_idx}"
    if value is None:
        return f'<c r="{ref}"/>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="n"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}" t="n"><v>{value}</v></c>'
    text = escape(str(value))
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def build_sheet_xml(rows: Sequence[Sequence[object]]) -> str:
    row_parts = []
    for r_idx, row in enumerate(rows, start=1):
        cell_parts = [excel_cell(r_idx, c_idx, val) for c_idx, val in enumerate(row, start=1)]
        row_parts.append(f'<row r="{r_idx}">{"".join(cell_parts)}</row>')
    body = "".join(row_parts)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{body}</sheetData>"
        "</worksheet>"
    )


def build_xlsx_bytes(sheet_name: str, headers: Sequence[str], rows: Sequence[Sequence[object]]) -> bytes:
    safe_sheet_name = (sheet_name or "Sheet1")[:31]
    all_rows: List[Sequence[object]] = [list(headers)] + [list(row) for row in rows]
    sheet_xml = build_sheet_xml(all_rows)
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        f'<sheet name="{escape(safe_sheet_name)}" sheetId="1" r:id="rId1"/>'
        "</sheets>"
        "</workbook>"
    )
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    root_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    stream = BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", root_rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return stream.getvalue()


def write_xlsx_file(filename: str, sheet_name: str, headers: Sequence[str], rows: Sequence[Sequence[object]]) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = EXPORT_DIR / filename
    output_path.write_bytes(build_xlsx_bytes(sheet_name=sheet_name, headers=headers, rows=rows))
    return output_path


@contextmanager
def transactional(db: Session):
    try:
        yield
        db.commit()
    except Exception:
        db.rollback()
        raise


def parse_int(value, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_wrong_training_config_payload(raw: Optional[Dict]) -> Dict:
    raw = raw if isinstance(raw, dict) else {}
    total = parse_int(raw.get("daily_total_count"), DEFAULT_WRONG_TRAINING_CONFIG["daily_total_count"])
    reinforcement = parse_int(raw.get("reinforcement_count"), DEFAULT_WRONG_TRAINING_CONFIG["reinforcement_count"])
    mastery = parse_int(raw.get("mastery_streak"), DEFAULT_WRONG_TRAINING_CONFIG["mastery_streak"])
    total = max(1, min(int(total or DEFAULT_WRONG_TRAINING_CONFIG["daily_total_count"]), 100))
    reinforcement = max(0, min(int(reinforcement or 0), total))
    mastery = max(1, min(int(mastery or DEFAULT_WRONG_TRAINING_CONFIG["mastery_streak"]), 10))
    return {
        "daily_total_count": total,
        "reinforcement_count": reinforcement,
        "regular_count": max(0, total - reinforcement),
        "mastery_streak": mastery,
    }


def load_wrong_training_config(db: Session) -> Dict:
    setting = db.query(AppSetting).filter(AppSetting.key == WRONG_TRAINING_CONFIG_KEY).first()
    raw = {}
    if setting and setting.value:
        try:
            raw = json.loads(setting.value or "{}")
        except (TypeError, ValueError):
            raw = {}
    return _normalize_wrong_training_config_payload(raw)


def save_wrong_training_config(db: Session, data: Optional[Dict]) -> Dict:
    config = _normalize_wrong_training_config_payload(data)
    stored = {
        "daily_total_count": config["daily_total_count"],
        "reinforcement_count": config["reinforcement_count"],
        "mastery_streak": config["mastery_streak"],
    }
    now_str = utc_now_iso()
    setting = db.query(AppSetting).filter(AppSetting.key == WRONG_TRAINING_CONFIG_KEY).first()
    if setting:
        setting.value = json.dumps(stored, ensure_ascii=False)
        setting.updated_at = now_str
    else:
        db.add(AppSetting(key=WRONG_TRAINING_CONFIG_KEY, value=json.dumps(stored, ensure_ascii=False), updated_at=now_str))
    config["updated_at"] = now_str
    return config


def wrong_training_priority_label() -> str:
    return "最近做错 > 错误次数 > 历史耗时 > 普通题补足"


def _wrong_last_seen_sort_key(value: Optional[str]):
    return (1, value or "") if value else (0, "")


def sort_wrong_training_candidates(rows):
    rows = list(rows or [])
    rows.sort(key=lambda item: _wrong_last_seen_sort_key(getattr(item[0], "last_seen_at", None)))
    rows.sort(key=lambda item: int(getattr(item[0], "avg_cost_ms", 0) or 0), reverse=True)
    rows.sort(key=lambda item: int(getattr(item[0], "wrong_count", 0) or 0), reverse=True)
    rows.sort(key=lambda item: getattr(item[0], "last_wrong_at", None) or "", reverse=True)
    return rows


def _pick_random_question_ids(candidates: Sequence[str], count: int) -> List[str]:
    pool = list(dict.fromkeys([qid for qid in candidates if qid]))
    if count <= 0 or not pool:
        return []
    if len(pool) <= count:
        return pool
    return random.sample(pool, count)


def estimate_avg_cost_ms(duration_sec: Optional[int], total_questions: int) -> Optional[int]:
    if duration_sec is None or total_questions <= 0:
        return None
    try:
        seconds = max(0, int(duration_sec))
    except (TypeError, ValueError):
        return None
    return int((seconds * 1000) / max(1, total_questions))


def parse_client_answer_mapping(raw_answers) -> Dict[str, Optional[int]]:
    mapping: Dict[str, Optional[int]] = {}
    if isinstance(raw_answers, dict):
        for qid, value in raw_answers.items():
            mapping[str(qid)] = parse_int(value, None)
        return mapping
    if isinstance(raw_answers, list):
        for item in raw_answers:
            if not isinstance(item, dict):
                continue
            qid = item.get("question_id") or item.get("id")
            if not qid:
                continue
            your = item.get("your")
            if your is None:
                your = item.get("your_index")
            if your is None:
                your = item.get("choice")
            mapping[str(qid)] = parse_int(your, None)
    return mapping


def load_class(class_id: int) -> Optional[SchoolClass]:
    return g.db.query(SchoolClass).filter(SchoolClass.id == class_id).first()


def load_exam(exam_id: str) -> Optional[Exam]:
    return g.db.query(Exam).filter(Exam.id == exam_id).first()


def can_access_class(class_row: SchoolClass) -> bool:
    role = getattr(g, "current_role", None)
    user = getattr(g, "current_user", None)
    if role == "assistant":
        return True
    return role == "teacher" and class_row.teacher_username == user


def ensure_class_access(class_id: int):
    class_row = load_class(class_id)
    if not class_row:
        return None, v1_error("not_found", status=404, reason="class not found")
    if not can_access_class(class_row):
        return None, v1_error("forbidden", status=403, reason="class not owned")
    return class_row, None


def ensure_exam_access(exam_id: str):
    exam = load_exam(exam_id)
    if not exam:
        return None, None, v1_error("not_found", status=404, reason="exam not found")
    class_row = load_class(exam.class_id)
    if not class_row:
        return None, None, v1_error("not_found", status=404, reason="class not found")
    if not can_access_class(class_row):
        return None, None, v1_error("forbidden", status=403, reason="class not owned")
    return exam, class_row, None


def ensure_exam_access_lifecycle(exam_id: str):
    exam = load_exam(exam_id)
    if not exam:
        return None, None, lifecycle_error("404_NOT_FOUND", status=404, reason="exam not found")
    class_row = load_class(exam.class_id)
    if not class_row:
        return None, None, lifecycle_error("404_NOT_FOUND", status=404, reason="class not found")
    if not can_access_class(class_row):
        return None, None, lifecycle_error("403_FORBIDDEN", status=403, reason="class not owned")
    return exam, class_row, None


def ensure_student_access(student_id: int):
    student = g.db.query(StudentProfile).filter(StudentProfile.id == student_id).first()
    if not student or student.status != "active":
        return None, None, v1_error("not_found", status=404, reason="student not found")
    class_row = None
    if getattr(g, "current_role", None) == "teacher":
        if not student.class_id:
            return None, None, v1_error("forbidden", status=403, reason="student is not assigned to any class")
        class_row = load_class(student.class_id)
        if not class_row or class_row.teacher_username != getattr(g, "current_user", None):
            return None, None, v1_error("forbidden", status=403, reason="student not owned by current teacher")
    elif student.class_id:
        class_row = load_class(student.class_id)
    return student, class_row, None


def owned_class_ids() -> List[int]:
    if getattr(g, "current_role", None) == "assistant":
        rows = g.db.query(SchoolClass.id).all()
    else:
        rows = g.db.query(SchoolClass.id).filter(SchoolClass.teacher_username == getattr(g, "current_user", None)).all()
    return [row[0] for row in rows]


def ensure_client_profile(require_class: bool = True):
    username = getattr(g, "current_user", None)
    profile = g.db.query(StudentProfile).filter(StudentProfile.client_username == username).first()
    if not profile or profile.status != "active":
        return None, api_error("student profile not found", status=404, code="profile_missing")
    if require_class and profile.class_id is None:
        return None, api_error("student profile not assigned to class", status=400, code="class_unassigned")
    return profile, None


def exam_is_open_for_action(exam: Exam):
    now = datetime.now(timezone.utc)
    start_at = parse_iso_datetime(exam.start_at)
    end_at = parse_iso_datetime(exam.end_at)
    if start_at and now < start_at:
        return False, "exam has not started"
    if end_at and now > end_at:
        return False, "exam has ended"
    if exam.status not in {"published", "active"}:
        return False, "exam is not open"
    return True, None


@bp.route("/health")
def health():
    return jsonify({"status": "ok"})


@bp.route("/auth/status")
def auth_status():
    return jsonify(
        {"authenticated": bool(getattr(g, "current_user", None)), "user": getattr(g, "current_user", None), "role": getattr(g, "current_role", None)}
    )


@bp.route("/login", methods=["GET"])
def login_page():
    if getattr(g, "current_user", None):
        return redirect("/")
    return send_from_directory(FRONTEND_DIR, "login.html")


@bp.route("/login", methods=["POST"])
def login_api():
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    password = data.get("password")
    ok, user, role = validate_credentials(g.db, username, password, User)
    if not ok:
        return jsonify({"error": "invalid credentials"}), 401
    user_row = g.db.query(User).filter(User.username == user).first()
    if user_row:
        user_row.last_login_at = utc_now_iso()
        g.db.commit()
    token = issue_session(g.db, user, "user")
    resp = jsonify({"status": "ok", "user": user, "token": token, "role": role})
    return apply_session_cookie(resp, token, secure=request.is_secure)


@bp.route("/client/login", methods=["POST"])
def client_login():
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    password = data.get("password")
    ok, user, _role = validate_credentials(g.db, username, password, ClientUser)
    client_ip = request.remote_addr or "unknown"
    if not ok:
        print(f"[client-login] fail user={username!r} ip={client_ip}")
        return jsonify({"ok": False, "error": "invalid credentials"}), 401
    client_row = g.db.query(ClientUser).filter(ClientUser.username == user).first()
    if client_row:
        client_row.last_login_at = utc_now_iso()
        g.db.commit()
    token = issue_session(g.db, user, "client")
    print(f"[client-login] success user={username!r} ip={client_ip}")
    return jsonify({"ok": True, "user": user, "token": token})


@bp.route("/logout")
def logout():
    token = request.cookies.get("session")
    auth_header = request.headers.get("Authorization", "")
    if not token and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if token:
        g.db.query(SessionToken).filter(SessionToken.token == token).delete()
        g.db.commit()
    resp = redirect("/login")
    return clear_session_cookie(resp, secure=request.is_secure)


@bp.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@bp.route("/", methods=["GET"])
@bp.route("/index.html", methods=["GET"])
@login_required(api=False)
def home():
    role = getattr(g, "current_role", None)
    if role == "admin":
        return send_from_directory(FRONTEND_DIR, "admin.html")
    if role == "assistant":
        return send_from_directory(FRONTEND_DIR, "assistant.html")
    if role == "teacher":
        return send_from_directory(FRONTEND_DIR, "teacher.html")
    return redirect("/login")


@bp.route("/assistant", methods=["GET"])
@login_required(api=False)
@role_required(["assistant"], api=False)
def assistant_page():
    return send_from_directory(FRONTEND_DIR, "assistant.html")


@bp.route("/teacher", methods=["GET"])
@login_required(api=False)
def teacher_page():
    if getattr(g, "current_role", None) not in ("teacher", "assistant"):
        return redirect("/")
    return send_from_directory(FRONTEND_DIR, "teacher.html")


@bp.route("/submit", methods=["GET"])
@login_required(api=False)
def submit_page():
    return send_from_directory(FRONTEND_DIR, "submit.html")


@bp.route("/records", methods=["GET"])
@login_required(api=False)
def records_page():
    return send_from_directory(FRONTEND_DIR, "records.html")


@bp.route("/admin", methods=["GET"])
@login_required(api=False)
@role_required(["admin"], api=False)
def admin_page():
    return send_from_directory(FRONTEND_DIR, "admin.html")


@bp.route("/questions")
@login_required(api=True)
@role_required(["client"])
def questions():
    try:
        user_id = getattr(g, "current_user", None)
        if not user_id:
            return jsonify({"error": "unauthorized"}), 401
        count_param = request.args.get("count")
        category = (request.args.get("category") or "").strip() or None
        try:
            max_count = g.db.query(Question).filter(Question.category == category).count() if category else g.db.query(Question).count()
            count_int = min(max_count, max(1, int(count_param))) if count_param else DEFAULT_QUESTION_COUNT
        except (TypeError, ValueError):
            count_int = DEFAULT_QUESTION_COUNT
        quiz_id = uuid.uuid4().hex
        questions = pick_questions(g.db, count_int, category=category)
        question_ids = [q.id for q in questions]
        ACTIVE_QUIZZES[quiz_id] = {
            "user_id": user_id,
            "ts": time.time(),
            "count": len(question_ids),
            "question_ids": question_ids,
        }
        return jsonify(
            {
                "user_id": user_id,
                "quiz_id": quiz_id,
                "questions": [question_to_dict(q, include_answer=False) for q in questions],
                "total": len(questions),
                "bank_size": g.db.query(Question).count(),
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@bp.route("/records.json")
@login_required(api=True)
def records_json():
    limit_val, offset_val = parse_pagination(default_limit=20, max_limit=500)
    role = getattr(g, "current_role", None)
    current_user = getattr(g, "current_user", None)
    query = g.db.query(Record)
    if role == "client":
        query = query.filter(Record.user_id == current_user)
    elif role not in {"assistant", "admin"}:
        return jsonify({"error": "forbidden"}), 403
    record_id = (request.args.get("id") or "").strip()
    if record_id:
        query = query.filter(Record.id == record_id)
    query = query.order_by(Record.timestamp.desc())
    total = query.count()
    records = query.offset(offset_val).limit(limit_val).all()
    return jsonify({"records": [record_to_dict(r) for r in records], "total": total, "limit": limit_val, "offset": offset_val})


@bp.route("/results.json")
@login_required(api=True)
def results_json():
    rid = (request.args.get("id") or "").strip()
    role = getattr(g, "current_role", None)
    current_user = getattr(g, "current_user", None)
    query = g.db.query(Record)
    if role == "client":
        query = query.filter(Record.user_id == current_user)
    elif role not in {"assistant", "admin"}:
        return jsonify({"error": "forbidden"}), 403
    if rid:
        query = query.filter(Record.id == rid)
    record = query.order_by(Record.timestamp.desc()).first()
    if record:
        return jsonify({"record": record_to_dict(record)})
    return jsonify({"error": "no record found"}), 404


@bp.route("/submit", methods=["POST"])
@login_required(api=True)
def submit():
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "invalid json"}), 400

    user_id = getattr(g, "current_user", None)
    request_user_id = data.get("user_id")
    quiz_id = data.get("quiz_id")
    answers = data.get("answers")

    if not user_id or not quiz_id:
        return jsonify({"error": "quiz_id is required"}), 400
    if request_user_id and str(request_user_id) != str(user_id):
        return jsonify({"error": "user_id mismatch"}), 403

    if not isinstance(answers, list):
        return jsonify({"error": "answers array required"}), 400

    entry = ACTIVE_QUIZZES.get(quiz_id)
    if not entry:
        return jsonify({"error": "quiz_id not found"}), 400

    if entry["user_id"] != user_id:
        return jsonify({"error": "user_id mismatch"}), 403

    if time.time() - entry["ts"] > QUIZ_SESSION_TTL_SECONDS:
        del ACTIVE_QUIZZES[quiz_id]
        return jsonify({"error": "quiz expired"}), 403

    expected_question_ids = entry.get("question_ids") or []
    if not isinstance(expected_question_ids, list) or not expected_question_ids:
        return jsonify({"error": "quiz session is invalid"}), 400

    graded = grade_submission(answers, expected_question_ids)
    if graded.get("error"):
        return jsonify({"error": graded["error"]}), 400
    score_val = graded["score"]
    wrong_list = graded["wrong"]
    total_val = graded["total"]

    print(f"[submit] user={user_id}, quiz_id={quiz_id}, score={score_val}")
    del ACTIVE_QUIZZES[quiz_id]

    client_ip = request.remote_addr or "unknown"
    record_id = store_score_record(g.db, user_id, quiz_id, score_val, total_val, client_ip, wrong_list)
    return jsonify({"status": "ok", "record_id": record_id})


def grade_submission(answers, expected_question_ids):
    expected_ids = [str(qid) for qid in (expected_question_ids or []) if qid is not None]
    expected_set = set(expected_ids)
    if not expected_set:
        return {"error": "quiz session has no questions"}

    answer_map = {}
    for answer in answers or []:
        if not isinstance(answer, dict):
            return {"error": "answers must be objects"}
        qid = str(answer.get("id") or "").strip()
        if not qid:
            return {"error": "answer id is required"}
        if qid not in expected_set:
            return {"error": "answers contain invalid question id"}
        if qid in answer_map:
            return {"error": "duplicate question answers are not allowed"}
        answer_map[qid] = answer.get("choice")

    total = len(expected_ids)
    correct = 0
    wrong = []
    for qid in expected_ids:
        choice = answer_map.get(qid)
        question = g.db.query(Question).filter(Question.id == qid).first()
        if question is None:
            continue
        if choice == question.answer:
            correct += 1
        else:
            wrong.append({"id": qid, "correct": question.answer, "your": choice})
    return {"score": correct, "total": total, "wrong": wrong}


def store_score_record(db: Session, user_id: str, quiz_id: str, score: int, total: int, client_ip: str, wrong=None):
    record_id = uuid.uuid4().hex
    record = Record(
        id=record_id,
        timestamp=utc_now_iso(),
        client_ip=client_ip,
        user_id=user_id,
        quiz_id=quiz_id,
        score=score,
        total=total,
        wrong=json.dumps(wrong or [], ensure_ascii=False),
    )
    db.add(record)
    db.commit()
    return record_id


@bp.route("/api/questions/bank")
@login_required(api=True)
@role_required(["teacher", "assistant"])
def bank_info():
    limit, offset = parse_pagination(default_limit=20, max_limit=100)
    category = (request.args.get("category") or "").strip()
    keyword = (request.args.get("q") or "").strip()
    query = g.db.query(Question)
    if category:
        query = query.filter(Question.category == category)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(or_(Question.id.like(like), Question.stem.like(like), Question.analysis.like(like)))
    query = query.order_by(Question.id.asc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return api_ok(
        {
            "questions": [question_to_dict(q, include_answer=True) for q in rows],
            "total": total,
            "default_count": DEFAULT_QUESTION_COUNT,
            "limit": limit,
            "offset": offset,
        }
    )


@bp.route("/api/questions", methods=["GET"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def list_questions():
    limit, offset = parse_pagination(default_limit=20, max_limit=100)
    category = (request.args.get("category") or "").strip()
    keyword = (request.args.get("q") or "").strip()
    query = g.db.query(Question)
    if category:
        query = query.filter(Question.category == category)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(or_(Question.id.like(like), Question.stem.like(like), Question.analysis.like(like)))
    query = query.order_by(Question.id.asc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return api_ok({"items": [question_to_dict(q, include_answer=True) for q in rows], "total": total, "limit": limit, "offset": offset})


@bp.route("/api/questions/import", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def import_questions():
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        data = data.get("questions")
    if not isinstance(data, list):
        return api_error("payload must be an array of questions", status=400)
    sanitized = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if not {"id", "stem", "options", "answer"} <= item.keys():
            continue
        options = item.get("options")
        answer = parse_int(item.get("answer"), None)
        if not isinstance(options, list) or len(options) < 2 or answer is None:
            continue
        if answer < 0 or answer >= len(options):
            continue
        sanitized.append(
            {
                "id": str(item.get("id")),
                "stem": str(item.get("stem")),
                "options": options,
                "answer": answer,
                "category": item.get("category"),
                "analysis": item.get("analysis"),
            }
        )
    if not sanitized:
        return api_error("no valid questions found", status=400)
    data = request.get_json(silent=True) or {}
    confirm_resp = require_confirm_phrase(data if isinstance(data, dict) else {}, "IMPORT_QUESTIONS")
    if confirm_resp:
        return confirm_resp
    with transactional(g.db):
        g.db.query(Question).delete()
        for item in sanitized:
            g.db.add(
                Question(
                    id=item["id"],
                    stem=item["stem"],
                    options=json.dumps(item["options"], ensure_ascii=False),
                    answer=int(item["answer"]),
                    category=item.get("category"),
                    analysis=item.get("analysis"),
                )
            )
        add_audit_log("IMPORT_QUESTIONS", target_type="questions", target_id="all", detail={"count": len(sanitized)})
    return api_ok({"total": len(sanitized)})


@bp.route("/api/questions", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def add_question():
    data = request.get_json(silent=True) or {}
    try:
        qid = data["id"]
        stem = data["stem"]
        options = data["options"]
        answer = int(data["answer"])
    except Exception:
        return api_error("id, stem, options, answer required", status=400)
    if not isinstance(options, list) or len(options) < 2:
        return api_error("options must be an array with at least 2 items", status=400)
    if answer < 0 or answer >= len(options):
        return api_error("answer index out of range", status=400)
    existing = g.db.query(Question).filter(Question.id == qid).first()
    if existing:
        return api_error("id already exists", status=400)
    with transactional(g.db):
        g.db.add(
            Question(
                id=qid,
                stem=stem,
                options=json.dumps(options, ensure_ascii=False),
                answer=answer,
                category=data.get("category"),
                analysis=data.get("analysis"),
            )
        )
        add_audit_log("CREATE_QUESTION", target_type="questions", target_id=str(qid))
    return api_ok()


@bp.route("/api/questions/<qid>", methods=["PUT"])
@login_required(api=True)
@role_required(["assistant"])
def update_question(qid):
    data = request.get_json(silent=True) or {}
    question = g.db.query(Question).filter(Question.id == qid).first()
    if not question:
        return api_error("not found", status=404)
    stem = data.get("stem", question.stem)
    options = data.get("options", json.loads(question.options or "[]"))
    answer = data.get("answer", question.answer)
    category = data.get("category", question.category)
    analysis = data.get("analysis", question.analysis)
    if not isinstance(options, list) or len(options) < 2:
        return api_error("options must be an array with at least 2 items", status=400)
    try:
        answer_int = int(answer)
    except Exception:
        return api_error("answer must be int", status=400)
    if answer_int < 0 or answer_int >= len(options):
        return api_error("answer index out of range", status=400)
    with transactional(g.db):
        question.stem = stem
        question.options = json.dumps(options, ensure_ascii=False)
        question.answer = answer_int
        question.category = category
        question.analysis = analysis
        add_audit_log("UPDATE_QUESTION", target_type="questions", target_id=str(qid))
    return api_ok()


@bp.route("/api/questions/<qid>", methods=["DELETE"])
@login_required(api=True)
@role_required(["assistant"])
def delete_question(qid):
    data = request.get_json(silent=True) or {}
    confirm_resp = require_confirm_phrase(data, "DELETE_QUESTION")
    if confirm_resp:
        return confirm_resp
    with transactional(g.db):
        deleted = g.db.query(Question).filter(Question.id == qid).delete()
        if deleted:
            add_audit_log("DELETE_QUESTION", target_type="questions", target_id=str(qid))
    if not deleted:
        return api_error("not found", status=404)
    return api_ok()


@bp.route("/api/records/clear", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def clear_records():
    data = request.get_json(silent=True) or {}
    confirm_resp = require_confirm_phrase(data, "CLEAR_RECORDS")
    if confirm_resp:
        return confirm_resp
    with transactional(g.db):
        deleted = g.db.query(Record).delete()
        add_audit_log("CLEAR_RECORDS", target_type="records", target_id="all", detail={"cleared": int(deleted or 0)})
    return api_ok({"cleared": deleted})


def _find_teacher(username: str) -> Optional[User]:
    return g.db.query(User).filter(User.username == username, User.role == "teacher").first()


def _find_client_user(username: str) -> Optional[ClientUser]:
    return g.db.query(ClientUser).filter(ClientUser.username == username).first()


def _username_exists_anywhere(username: str) -> bool:
    username = str(username or "").strip()
    if not username:
        return False
    return bool(g.db.query(User.id).filter(User.username == username).first() or g.db.query(ClientUser.id).filter(ClientUser.username == username).first())


@bp.route("/api/admin/teachers", methods=["GET"])
@login_required(api=True)
@role_required(["admin", "assistant"])
def admin_list_teachers():
    limit, offset = parse_admin_limit_offset()
    keyword = (request.args.get("q") or "").strip()
    is_active = parse_is_active_filter(request.args.get("is_active"))
    query = g.db.query(User).filter(User.role == "teacher")
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(or_(User.username.like(like), User.display_name.like(like)))
    if is_active is not None:
        query = query.filter(User.is_active == is_active)
    query = query.order_by(User.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return api_ok({"items": [user_to_admin_dict(row) for row in rows], "total": total, "limit": limit, "offset": offset})


@bp.route("/api/admin/teachers", methods=["POST"])
@login_required(api=True)
@role_required(["admin"])
def admin_create_teacher():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "").strip()
    display_name = str(data.get("display_name") or "").strip() or None
    valid_from = normalize_validity_value(data.get("valid_from"), end_of_day=False)
    valid_to = normalize_validity_value(data.get("valid_to"), end_of_day=True)
    if data.get("valid_from") not in (None, "") and valid_from is None:
        return api_error("valid_from must be ISO8601 or YYYY-MM-DD", status=400)
    if data.get("valid_to") not in (None, "") and valid_to is None:
        return api_error("valid_to must be ISO8601 or YYYY-MM-DD", status=400)
    if valid_from and valid_to and parse_validity_datetime(valid_to, end_of_day=True) < parse_validity_datetime(valid_from):
        return api_error("valid_to must be >= valid_from", status=400)
    if not username:
        return api_error("username is required", status=400)
    if not password or len(password) < 6:
        return api_error("password must be at least 6 chars", status=400)
    exists = _username_exists_anywhere(username)
    if exists:
        return api_error("username already exists in another account namespace", status=409)
    now_str = utc_now_iso()
    with transactional(g.db):
        row = User(
            username=username,
            password=ensure_password_hash(password),
            role="teacher",
            display_name=display_name,
            is_active=1,
            created_at=now_str,
            updated_at=now_str,
            valid_from=valid_from,
            valid_to=valid_to,
            last_login_at=None,
        )
        g.db.add(row)
        g.db.flush()
        add_audit_log(
            "CREATE_TEACHER",
            target_type="users",
            target_id=username,
            detail={"display_name": display_name, "is_active": 1, "valid_from": valid_from, "valid_to": valid_to},
        )
        result = user_to_admin_dict(row)
    return api_ok({"teacher": result}, status=201)


@bp.route("/api/admin/teachers/<username>/enable", methods=["POST"])
@login_required(api=True)
@role_required(["admin"])
def admin_enable_teacher(username):
    data = request.get_json(silent=True) or {}
    confirm_resp = require_confirm_phrase(data, "ENABLE_TEACHER")
    if confirm_resp:
        return confirm_resp
    row = _find_teacher(username)
    if not row:
        return api_error("teacher not found", status=404)
    now_str = utc_now_iso()
    with transactional(g.db):
        row.is_active = 1
        row.updated_at = now_str
        g.db.query(SessionToken).filter(SessionToken.user == row.username, SessionToken.principal_type == "user").delete()
        add_audit_log("ENABLE_TEACHER", target_type="users", target_id=row.username)
    return api_ok({"teacher": user_to_admin_dict(row)})


@bp.route("/api/admin/teachers/<username>/disable", methods=["POST"])
@login_required(api=True)
@role_required(["admin"])
def admin_disable_teacher(username):
    data = request.get_json(silent=True) or {}
    confirm_resp = require_confirm_phrase(data, "DISABLE_TEACHER")
    if confirm_resp:
        return confirm_resp
    row = _find_teacher(username)
    if not row:
        return api_error("teacher not found", status=404)
    now_str = utc_now_iso()
    with transactional(g.db):
        row.is_active = 0
        row.updated_at = now_str
        g.db.query(SessionToken).filter(SessionToken.user == row.username, SessionToken.principal_type == "user").delete()
        add_audit_log("DISABLE_TEACHER", target_type="users", target_id=row.username)
    return api_ok({"teacher": user_to_admin_dict(row)})


@bp.route("/api/admin/teachers/<username>/password/reset", methods=["POST"])
@login_required(api=True)
@role_required(["admin"])
def admin_reset_teacher_password(username):
    data = request.get_json(silent=True) or {}
    confirm_resp = require_confirm_phrase(data, "RESET_TEACHER_PASSWORD")
    if confirm_resp:
        return confirm_resp
    new_password = str(data.get("new_password") or "").strip()
    if not new_password or len(new_password) < 6:
        return api_error("new_password must be at least 6 chars", status=400)
    row = _find_teacher(username)
    if not row:
        return api_error("teacher not found", status=404)
    with transactional(g.db):
        row.password = ensure_password_hash(new_password)
        row.updated_at = utc_now_iso()
        g.db.query(SessionToken).filter(SessionToken.user == row.username, SessionToken.principal_type == "user").delete()
        add_audit_log("RESET_TEACHER_PASSWORD", target_type="users", target_id=row.username)
    return api_ok({"teacher": user_to_admin_dict(row)})


@bp.route("/api/admin/teachers/<username>/profile", methods=["PUT"])
@login_required(api=True)
@role_required(["admin"])
def admin_update_teacher_profile(username):
    row = _find_teacher(username)
    if not row:
        return api_error("teacher not found", status=404)
    data = request.get_json(silent=True) or {}
    display_name = data.get("display_name")
    display_name = str(display_name).strip() if display_name is not None else None
    if display_name == "":
        display_name = None
    valid_from = normalize_validity_value(data.get("valid_from"), end_of_day=False) if "valid_from" in data else getattr(row, "valid_from", None)
    valid_to = normalize_validity_value(data.get("valid_to"), end_of_day=True) if "valid_to" in data else getattr(row, "valid_to", None)
    if "valid_from" in data and data.get("valid_from") not in (None, "") and valid_from is None:
        return api_error("valid_from must be ISO8601 or YYYY-MM-DD", status=400)
    if "valid_to" in data and data.get("valid_to") not in (None, "") and valid_to is None:
        return api_error("valid_to must be ISO8601 or YYYY-MM-DD", status=400)
    if valid_from and valid_to and parse_validity_datetime(valid_to, end_of_day=True) < parse_validity_datetime(valid_from):
        return api_error("valid_to must be >= valid_from", status=400)
    with transactional(g.db):
        row.display_name = display_name
        if "valid_from" in data:
            row.valid_from = valid_from
        if "valid_to" in data:
            row.valid_to = valid_to
        row.updated_at = utc_now_iso()
        add_audit_log("UPDATE_TEACHER_PROFILE", target_type="users", target_id=row.username, detail={"display_name": display_name, "valid_from": row.valid_from, "valid_to": row.valid_to})
    return api_ok({"teacher": user_to_admin_dict(row)})


@bp.route("/api/admin/client_users", methods=["GET"])
@login_required(api=True)
@role_required(["assistant"])
def admin_list_client_users():
    limit, offset = parse_admin_limit_offset()
    keyword = (request.args.get("q") or "").strip()
    is_active = parse_is_active_filter(request.args.get("is_active"))
    query = g.db.query(ClientUser)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(ClientUser.username.like(like))
    if is_active is not None:
        query = query.filter(ClientUser.is_active == is_active)
    query = query.order_by(ClientUser.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return api_ok({"items": [client_user_to_admin_dict(row) for row in rows], "total": total, "limit": limit, "offset": offset})


@bp.route("/api/admin/client_users", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def admin_create_client_user():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "").strip()
    if not username:
        return api_error("username is required", status=400)
    if not password or len(password) < 6:
        return api_error("password must be at least 6 chars", status=400)
    exists = _username_exists_anywhere(username)
    if exists:
        return api_error("username already exists in another account namespace", status=409)
    now_str = utc_now_iso()
    with transactional(g.db):
        row = ClientUser(
            username=username,
            password=ensure_password_hash(password),
            is_active=1,
            created_at=now_str,
            updated_at=now_str,
            last_login_at=None,
        )
        g.db.add(row)
        g.db.flush()
        add_audit_log("CREATE_CLIENT_USER", target_type="client_users", target_id=username, detail={"is_active": 1})
        result = client_user_to_admin_dict(row)
    return api_ok({"client_user": result}, status=201)


@bp.route("/api/admin/client_users/<username>/enable", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def admin_enable_client_user(username):
    data = request.get_json(silent=True) or {}
    confirm_resp = require_confirm_phrase(data, "ENABLE_CLIENT_USER")
    if confirm_resp:
        return confirm_resp
    row = _find_client_user(username)
    if not row:
        return api_error("client user not found", status=404)
    with transactional(g.db):
        row.is_active = 1
        row.updated_at = utc_now_iso()
        g.db.query(SessionToken).filter(SessionToken.user == row.username, SessionToken.principal_type == "client").delete()
        add_audit_log("ENABLE_CLIENT_USER", target_type="client_users", target_id=row.username)
    return api_ok({"client_user": client_user_to_admin_dict(row)})


@bp.route("/api/admin/client_users/<username>/disable", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def admin_disable_client_user(username):
    data = request.get_json(silent=True) or {}
    confirm_resp = require_confirm_phrase(data, "DISABLE_CLIENT_USER")
    if confirm_resp:
        return confirm_resp
    row = _find_client_user(username)
    if not row:
        return api_error("client user not found", status=404)
    with transactional(g.db):
        row.is_active = 0
        row.updated_at = utc_now_iso()
        g.db.query(SessionToken).filter(SessionToken.user == row.username, SessionToken.principal_type == "client").delete()
        add_audit_log("DISABLE_CLIENT_USER", target_type="client_users", target_id=row.username)
    return api_ok({"client_user": client_user_to_admin_dict(row)})


@bp.route("/api/admin/client_users/<username>/password/reset", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def admin_reset_client_password(username):
    data = request.get_json(silent=True) or {}
    confirm_resp = require_confirm_phrase(data, "RESET_CLIENT_PASSWORD")
    if confirm_resp:
        return confirm_resp
    new_password = str(data.get("new_password") or "").strip()
    if not new_password or len(new_password) < 6:
        return api_error("new_password must be at least 6 chars", status=400)
    row = _find_client_user(username)
    if not row:
        return api_error("client user not found", status=404)
    with transactional(g.db):
        row.password = ensure_password_hash(new_password)
        row.updated_at = utc_now_iso()
        g.db.query(SessionToken).filter(SessionToken.user == row.username, SessionToken.principal_type == "client").delete()
        add_audit_log("RESET_CLIENT_PASSWORD", target_type="client_users", target_id=row.username)
    return api_ok({"client_user": client_user_to_admin_dict(row)})


@bp.route("/api/admin/schools", methods=["POST"])
@login_required(api=True)
@role_required(["admin"])
def admin_create_school():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return api_error("name is required", status=400)
    existing = g.db.query(School).filter(School.name == name).first()
    if existing:
        return api_error("school already exists", status=409)
    with transactional(g.db):
        row = School(name=name, created_at=utc_now_iso())
        g.db.add(row)
        g.db.flush()
        add_audit_log("CREATE_SCHOOL", target_type="schools", target_id=str(row.id), detail={"name": row.name})
        result = school_to_dict(row)
    return api_ok({"school": result}, status=201)


@bp.route("/api/admin/schools", methods=["GET"])
@login_required(api=True)
@role_required(["admin", "assistant"])
def admin_list_schools():
    limit, offset = parse_admin_limit_offset()
    keyword = (request.args.get("q") or "").strip()
    query = g.db.query(School)
    if keyword:
        query = query.filter(School.name.like(f"%{keyword}%"))
    query = query.order_by(School.id.asc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return api_ok({"items": [school_to_dict(s) for s in rows], "total": total, "limit": limit, "offset": offset})


@bp.route("/api/admin/classes", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def admin_create_class():
    data = request.get_json(silent=True) or {}
    school_id = parse_int(data.get("school_id"), None)
    grade = (data.get("grade") or "").strip()
    name = (data.get("name") or "").strip()
    teacher_username = (data.get("teacher_username") or "").strip() or None
    status = str(data.get("status") or "active").strip() or "active"
    if school_id is None or not grade or not name:
        return api_error("school_id, grade, name are required", status=400)
    if status not in VALID_CLASS_STATUS:
        return api_error("status must be active or dismissed", status=400)
    school = g.db.query(School).filter(School.id == school_id).first()
    if not school:
        return api_error("school not found", status=404)
    duplicate = g.db.query(SchoolClass).filter(SchoolClass.school_id == school_id, SchoolClass.grade == grade, SchoolClass.name == name).first()
    if duplicate:
        return api_error("class already exists in same school/grade", status=409)
    teacher = None
    if teacher_username:
        teacher = g.db.query(User).filter(User.username == teacher_username).first()
        if not teacher:
            return api_error("teacher user not found", status=404)
        if teacher.role != "teacher" or int(teacher.is_active or 0) != 1:
            return api_error("teacher user must be active teacher", status=409)
    with transactional(g.db):
        row = SchoolClass(
            school_id=school_id,
            grade=grade,
            name=name,
            teacher_username=teacher_username,
            status=status,
            created_at=utc_now_iso(),
        )
        g.db.add(row)
        g.db.flush()
        add_audit_log(
            "CREATE_CLASS",
            target_type="classes",
            target_id=str(row.id),
            detail={"school_id": school_id, "grade": grade, "name": name, "teacher_username": teacher_username, "status": status},
        )
        result = class_to_dict(row)
    return api_ok({"class": result}, status=201)


@bp.route("/api/admin/classes", methods=["GET"])
@login_required(api=True)
@role_required(["assistant"])
def admin_list_classes():
    limit, offset = parse_admin_limit_offset()
    query = g.db.query(SchoolClass)
    school_id = parse_int(request.args.get("school_id"), None)
    grade = (request.args.get("grade") or "").strip()
    teacher_username = (request.args.get("teacher_username") or "").strip()
    status = (request.args.get("status") or "").strip()
    if school_id is not None:
        query = query.filter(SchoolClass.school_id == school_id)
    if grade:
        query = query.filter(SchoolClass.grade == grade)
    if teacher_username:
        query = query.filter(SchoolClass.teacher_username == teacher_username)
    if status:
        query = query.filter(SchoolClass.status == status)
    query = query.order_by(SchoolClass.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return api_ok({"items": [class_to_dict(c) for c in rows], "total": total, "limit": limit, "offset": offset})


@bp.route("/api/admin/classes", methods=["PUT"])
@login_required(api=True)
@role_required(["assistant"])
def admin_update_class():
    data = request.get_json(silent=True) or {}
    class_id = parse_int(data.get("id"), None)
    if class_id is None:
        return api_error("id is required", status=400)
    row = load_class(class_id)
    if not row:
        return api_error("class not found", status=404)
    old_teacher_username = row.teacher_username
    old_status = row.status
    school_id = parse_int(data.get("school_id", row.school_id), None)
    grade = str(data.get("grade", row.grade) or "").strip() or row.grade
    name = str(data.get("name", row.name) or "").strip() or row.name
    teacher_username = str(data.get("teacher_username", row.teacher_username) or "").strip() or None
    status = str(data.get("status", row.status) or "").strip() or row.status
    if school_id is None:
        return api_error("school_id invalid", status=400)
    school = g.db.query(School).filter(School.id == school_id).first()
    if not school:
        return api_error("school not found", status=404)
    teacher = None
    if teacher_username:
        teacher = g.db.query(User).filter(User.username == teacher_username).first()
        if not teacher:
            return api_error("teacher user not found", status=404)
        if teacher.role != "teacher" or int(teacher.is_active or 0) != 1:
            return api_error("teacher user must be active teacher", status=409)
    if status not in VALID_CLASS_STATUS:
        return api_error("status must be active or dismissed", status=400)
    duplicate = (
        g.db.query(SchoolClass)
        .filter(SchoolClass.school_id == school_id, SchoolClass.grade == grade, SchoolClass.name == name, SchoolClass.id != row.id)
        .first()
    )
    if duplicate:
        return api_error("class already exists in same school/grade", status=409)
    with transactional(g.db):
        row.school_id = school_id
        row.grade = grade
        row.name = name
        row.teacher_username = teacher_username
        row.status = status
        add_audit_log(
            "UPDATE_CLASS",
            target_type="classes",
            target_id=str(row.id),
            detail={"school_id": school_id, "grade": grade, "name": name, "teacher_username": teacher_username, "status": status},
        )
        if teacher_username != old_teacher_username:
            add_audit_log(
                "ASSIGN_TEACHER_TO_CLASS",
                target_type="classes",
                target_id=str(row.id),
                detail={"teacher_username": teacher_username},
            )
        if old_status != "dismissed" and status == "dismissed":
            add_audit_log("DISMISS_CLASS", target_type="classes", target_id=str(row.id))
    return api_ok({"class": class_to_dict(row)})


@bp.route("/api/admin/students", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def admin_create_student():
    data = request.get_json(silent=True) or {}
    client_username = (data.get("client_username") or "").strip()
    if not client_username:
        return api_error("client_username is required", status=400)
    user_row = g.db.query(ClientUser).filter(ClientUser.username == client_username).first()
    if not user_row:
        return api_error("client_username not found in client_users", status=400)
    existing = g.db.query(StudentProfile).filter(StudentProfile.client_username == client_username).first()
    if existing:
        return api_error("student profile already exists", status=409)

    school_id = parse_int(data.get("school_id"), None)
    class_id = parse_int(data.get("class_id"), None)
    grade = str(data.get("grade") or "").strip() or None
    if class_id is not None:
        class_row = load_class(class_id)
        if not class_row:
            return api_error("class not found", status=404)
        if class_row.status == "dismissed":
            return api_error("cannot assign to dismissed class", status=400)
        if school_id is not None and school_id != class_row.school_id:
            return api_error("class.school_id must match student school_id", status=409)
        if grade and grade != class_row.grade:
            return api_error("class.grade must match student grade", status=409)
        if school_id is None:
            school_id = class_row.school_id
        if not grade:
            grade = class_row.grade
    if school_id is None:
        return api_error("school_id is required", status=400)
    school = g.db.query(School).filter(School.id == school_id).first()
    if not school:
        return api_error("school not found", status=404)
    if not grade:
        return api_error("grade is required", status=400)
    name = str(data.get("name") or "").strip() or client_username
    gender = str(data.get("gender") or "").strip() or None
    if gender and gender not in VALID_GENDERS:
        return api_error("gender must be M, F or U", status=400)
    status = str(data.get("status") or "active").strip() or "active"
    if status not in VALID_STUDENT_STATUS:
        return api_error("status must be active or inactive", status=400)
    wrong_training_enabled = 1 if parse_bool(data.get("wrong_training_enabled"), default=False) else 0
    now_str = utc_now_iso()

    with transactional(g.db):
        row = StudentProfile(
            client_username=client_username,
            school_id=school_id,
            grade=grade,
            class_id=class_id,
            student_no=data.get("student_no"),
            name=name,
            gender=gender,
            status=status,
            wrong_training_enabled=wrong_training_enabled,
            created_at=now_str,
            updated_at=now_str,
        )
        g.db.add(row)
        g.db.flush()
        add_audit_log(
            "CREATE_STUDENT_PROFILE",
            target_type="student_profiles",
            target_id=str(row.id),
            detail={
                "client_username": row.client_username,
                "school_id": row.school_id,
                "grade": row.grade,
                "class_id": row.class_id,
                "wrong_training_enabled": wrong_training_enabled,
            },
        )
        if row.class_id is not None:
            add_audit_log(
                "ASSIGN_CLASS",
                target_type="student_profiles",
                target_id=str(row.id),
                detail={"class_id": row.class_id},
            )
        result = student_to_dict(row)
    return api_ok({"student": result}, status=201)


@bp.route("/api/admin/students", methods=["GET"])
@login_required(api=True)
@role_required(["assistant"])
def admin_list_students():
    limit, offset = parse_admin_limit_offset()
    query = g.db.query(StudentProfile)
    school_id = parse_int(request.args.get("school_id"), None)
    grade = (request.args.get("grade") or "").strip()
    status = (request.args.get("status") or "").strip()
    keyword = (request.args.get("q") or "").strip()

    class_arg = request.args.get("class_id")
    if class_arg is not None:
        class_id = parse_int(class_arg, None)
        if class_id is None:
            query = query.filter(StudentProfile.class_id.is_(None))
        else:
            query = query.filter(StudentProfile.class_id == class_id)
    if school_id is not None:
        query = query.filter(StudentProfile.school_id == school_id)
    if grade:
        query = query.filter(StudentProfile.grade == grade)
    if status:
        query = query.filter(StudentProfile.status == status)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            or_(
                StudentProfile.client_username.like(like),
                StudentProfile.student_no.like(like),
                StudentProfile.name.like(like),
            )
        )
    query = query.order_by(StudentProfile.id.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    wrong_counts = {}
    student_ids = [row.id for row in rows]
    if student_ids:
        count_rows = (
            g.db.query(WrongQuestion.student_profile_id, func.count(WrongQuestion.question_id))
            .filter(WrongQuestion.student_profile_id.in_(student_ids), WrongQuestion.is_active == 1)
            .group_by(WrongQuestion.student_profile_id)
            .all()
        )
        wrong_counts = {int(student_id): int(count or 0) for student_id, count in count_rows}
    return api_ok({
        "items": [student_to_dict(s, wrong_pool_active_count=wrong_counts.get(int(s.id), 0)) for s in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@bp.route("/api/admin/students/<int:student_id>", methods=["PUT"])
@login_required(api=True)
@role_required(["assistant"])
def admin_update_student(student_id):
    row = g.db.query(StudentProfile).filter(StudentProfile.id == student_id).first()
    if not row:
        return api_error("student not found", status=404)
    data = request.get_json(silent=True) or {}
    old_class_id = row.class_id
    old_wrong_training_enabled = int(getattr(row, "wrong_training_enabled", 0) or 0)

    if "client_username" in data:
        new_username = (data.get("client_username") or "").strip()
        if not new_username:
            return api_error("client_username cannot be empty", status=400)
        existing_client = g.db.query(ClientUser).filter(ClientUser.username == new_username).first()
        if not existing_client:
            return api_error("client_username not found in client_users", status=400)
        conflict = g.db.query(StudentProfile).filter(StudentProfile.client_username == new_username, StudentProfile.id != student_id).first()
        if conflict:
            return api_error("client_username already bound", status=409)
        row.client_username = new_username

    if "school_id" in data:
        school_id = parse_int(data.get("school_id"), None)
        if school_id is None:
            return api_error("school_id invalid", status=400)
        school = g.db.query(School).filter(School.id == school_id).first()
        if not school:
            return api_error("school not found", status=404)
        row.school_id = school_id

    if "class_id" in data:
        class_value = data.get("class_id")
        class_id = parse_int(class_value, None)
        if class_value is None or class_id is None:
            row.class_id = None
        else:
            class_row = load_class(class_id)
            if not class_row:
                return api_error("class not found", status=404)
            if class_row.status == "dismissed":
                return api_error("cannot assign to dismissed class", status=400)
            if row.school_id is not None and row.school_id != class_row.school_id:
                return api_error("class.school_id must match student school_id", status=409)
            if row.grade and row.grade != class_row.grade:
                return api_error("class.grade must match student grade", status=409)
            row.class_id = class_id
            if row.school_id is None:
                row.school_id = class_row.school_id
            if not row.grade:
                row.grade = class_row.grade

    if "grade" in data:
        grade_value = str(data.get("grade") or "").strip()
        if not grade_value:
            return api_error("grade cannot be empty", status=400)
        row.grade = grade_value
    if "student_no" in data:
        row.student_no = data.get("student_no")
    if "name" in data:
        name_value = str(data.get("name") or "").strip()
        if not name_value:
            return api_error("name cannot be empty", status=400)
        row.name = name_value
    if "gender" in data:
        gender_value = str(data.get("gender") or "").strip() or None
        if gender_value and gender_value not in VALID_GENDERS:
            return api_error("gender must be M, F or U", status=400)
        row.gender = gender_value
    if "status" in data:
        status_value = str(data.get("status") or "").strip()
        if status_value not in VALID_STUDENT_STATUS:
            return api_error("status must be active or inactive", status=400)
        row.status = status_value
    if "wrong_training_enabled" in data:
        row.wrong_training_enabled = 1 if parse_bool(data.get("wrong_training_enabled"), default=False) else 0
    if row.school_id is None:
        return api_error("school_id is required", status=400)
    if not row.grade:
        return api_error("grade is required", status=400)
    if row.class_id is not None:
        class_row = load_class(row.class_id)
        if not class_row:
            return api_error("class not found", status=404)
        if row.school_id != class_row.school_id:
            return api_error("class.school_id must match student school_id", status=409)
        if row.grade != class_row.grade:
            return api_error("class.grade must match student grade", status=409)
    if not row.name:
        row.name = row.client_username
    now_str = utc_now_iso()
    with transactional(g.db):
        row.updated_at = now_str
        add_audit_log(
            "UPDATE_STUDENT_PROFILE",
            target_type="student_profiles",
            target_id=str(row.id),
            detail={
                "client_username": row.client_username,
                "school_id": row.school_id,
                "grade": row.grade,
                "class_id": row.class_id,
                "status": row.status,
                "wrong_training_enabled": int(getattr(row, "wrong_training_enabled", 0) or 0),
            },
        )
        if old_wrong_training_enabled == 1 and int(getattr(row, "wrong_training_enabled", 0) or 0) == 0:
            add_audit_log(
                "DISABLE_STUDENT_WRONG_TRAINING",
                target_type="student_profiles",
                target_id=str(row.id),
                detail={"wrong_training_enabled": 0},
            )
        elif old_wrong_training_enabled == 0 and int(getattr(row, "wrong_training_enabled", 0) or 0) == 1:
            add_audit_log(
                "ENABLE_STUDENT_WRONG_TRAINING",
                target_type="student_profiles",
                target_id=str(row.id),
                detail={"wrong_training_enabled": 1},
            )
        if old_class_id != row.class_id:
            if row.class_id is None:
                add_audit_log(
                    "UNASSIGN_CLASS",
                    target_type="student_profiles",
                    target_id=str(row.id),
                    detail={"old_class_id": old_class_id},
                )
            else:
                add_audit_log(
                    "ASSIGN_CLASS",
                    target_type="student_profiles",
                    target_id=str(row.id),
                    detail={"old_class_id": old_class_id, "class_id": row.class_id},
                )
    return api_ok({"student": student_to_dict(row)})


@bp.route("/api/admin/students/<int:student_id>", methods=["DELETE"])
@login_required(api=True)
@role_required(["assistant"])
def admin_delete_student(student_id):
    row = g.db.query(StudentProfile).filter(StudentProfile.id == student_id).first()
    if not row:
        return api_error("student not found", status=404)
    with transactional(g.db):
        row.status = "inactive"
        row.updated_at = utc_now_iso()
        add_audit_log("UPDATE_STUDENT_PROFILE", target_type="student_profiles", target_id=str(row.id), detail={"status": "inactive"})
    return api_ok({"student": student_to_dict(row)})


@bp.route("/api/admin/attempts", methods=["GET"])
@login_required(api=True)
@role_required(["assistant"])
def admin_list_attempts():
    limit, offset = parse_admin_limit_offset()
    school_id = parse_int(request.args.get("school_id"), None)
    grade = (request.args.get("grade") or "").strip() or None
    class_id = parse_int(request.args.get("class_id"), None)
    teacher_username = (request.args.get("teacher_username") or "").strip() or None
    student_no = (request.args.get("student_no") or "").strip() or None
    student_name = (request.args.get("name") or "").strip() or None
    client_username = (request.args.get("client_username") or "").strip() or None
    exam_id = (request.args.get("exam_id") or "").strip() or None
    exam_type = (request.args.get("type") or "").strip() or None
    from_raw = (request.args.get("from") or "").strip() or None
    to_raw = (request.args.get("to") or "").strip() or None
    from_dt = parse_iso_datetime(from_raw)
    to_dt = parse_iso_datetime(to_raw)
    if from_raw and not from_dt:
        return api_error("invalid from datetime", status=400)
    if to_raw and not to_dt:
        return api_error("invalid to datetime", status=400)
    if from_dt and to_dt and to_dt < from_dt:
        return api_error("to must be >= from", status=400)

    query = (
        g.db.query(Attempt, Exam, StudentProfile)
        .join(Exam, Attempt.exam_id == Exam.id)
        .join(StudentProfile, Attempt.student_profile_id == StudentProfile.id)
        .filter(Attempt.submitted_at.isnot(None))
    )
    if school_id is not None:
        query = query.filter(StudentProfile.school_id == school_id)
    if grade:
        query = query.filter(StudentProfile.grade == grade)
    if class_id is not None:
        query = query.filter(StudentProfile.class_id == class_id)
    if teacher_username:
        query = query.filter(Exam.created_by == teacher_username)
    if student_no:
        query = query.filter(StudentProfile.student_no.like(f"%{student_no}%"))
    if student_name:
        query = query.filter(StudentProfile.name.like(f"%{student_name}%"))
    if client_username:
        query = query.filter(StudentProfile.client_username.like(f"%{client_username}%"))
    if exam_id:
        query = query.filter(Attempt.exam_id == exam_id)
    if exam_type:
        query = query.filter(Exam.exam_type == exam_type)
    if from_dt:
        query = query.filter(Attempt.submitted_at >= to_iso_z(from_dt))
    if to_dt:
        query = query.filter(Attempt.submitted_at <= to_iso_z(to_dt))
    query = query.order_by(Attempt.submitted_at.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()

    class_ids = [student.class_id for _attempt, _exam, student in rows if student.class_id is not None]
    class_map = {}
    if class_ids:
        classes = g.db.query(SchoolClass).filter(SchoolClass.id.in_(class_ids)).all()
        class_map = {row.id: row for row in classes}

    items = []
    for attempt, exam, student in rows:
        class_row = class_map.get(student.class_id)
        total_q = int(attempt.total or 0)
        score_val = int(attempt.score or 0)
        items.append(
            {
                "attempt_id": attempt.id,
                "exam_id": attempt.exam_id,
                "exam_type": exam.exam_type,
                "teacher_username": exam.created_by,
                "school_id": student.school_id,
                "grade": student.grade,
                "class_id": student.class_id,
                "class_name": class_row.name if class_row else None,
                "student_id": student.id,
                "student_no": student.student_no,
                "name": student.name,
                "client_username": student.client_username,
                "score": score_val,
                "total": total_q,
                "accuracy": normalized_accuracy(score_val, total_q),
                "duration_sec": attempt.duration_sec,
                "submitted_at": attempt.submitted_at,
            }
        )

    with transactional(g.db):
        add_audit_log(
            "ADMIN_VIEW_ATTEMPTS",
            target_type="attempts",
            target_id=None,
            detail={"total": total, "limit": limit, "offset": offset},
        )
    return api_ok({"items": items, "total": total, "limit": limit, "offset": offset})


@bp.route("/api/admin/attempts/<attempt_id>", methods=["GET"])
@login_required(api=True)
@role_required(["assistant"])
def admin_attempt_detail(attempt_id):
    attempt = g.db.query(Attempt).filter(Attempt.id == attempt_id).first()
    if not attempt:
        return api_error("attempt not found", status=404)
    exam = load_exam(attempt.exam_id)
    if not exam:
        return api_error("exam not found", status=404)
    student = g.db.query(StudentProfile).filter(StudentProfile.id == attempt.student_profile_id).first()
    if not student:
        return api_error("student not found", status=404)
    include_analysis = parse_bool(request.args.get("include_analysis"), default=True)
    answer_rows = g.db.query(Answer).filter(Answer.attempt_id == attempt.id).all()
    question_ids = [x.question_id for x in answer_rows]
    questions = g.db.query(Question).filter(Question.id.in_(question_ids)).all() if question_ids else []
    qmap = {q.id: q for q in questions}
    items = []
    for row in answer_rows:
        question = qmap.get(row.question_id)
        if not question:
            continue
        item = {
            "question_id": row.question_id,
            "stem": question.stem,
            "options": json.loads(question.options or "[]"),
            "your": row.your,
            "correct": row.correct,
            "is_correct": bool(row.is_correct),
            "category": question.category,
        }
        if include_analysis:
            item["analysis"] = question.analysis
        items.append(item)

    total_q = int(attempt.total or 0)
    score_val = int(attempt.score or 0)
    with transactional(g.db):
        add_audit_log("ADMIN_VIEW_ATTEMPT_DETAIL", target_type="attempts", target_id=attempt.id)
    return api_ok(
        {
            "attempt": {
                "attempt_id": attempt.id,
                "exam_id": exam.id,
                "exam_title": exam.title,
                "student_id": student.id,
                "student_no": student.student_no,
                "student_name": student.name,
                "score": score_val,
                "total": total_q,
                "accuracy": normalized_accuracy(score_val, total_q),
                "started_at": attempt.started_at,
                "submitted_at": attempt.submitted_at,
                "duration_sec": attempt.duration_sec,
            },
            "items": items,
        }
    )


@bp.route("/api/admin/audit_logs", methods=["GET"])
@login_required(api=True)
@role_required(["assistant"])
def admin_list_audit_logs():
    limit, offset = parse_admin_limit_offset()
    action = (request.args.get("action") or "").strip() or None
    actor_username = (request.args.get("actor_username") or "").strip() or None
    from_raw = (request.args.get("from") or "").strip() or None
    to_raw = (request.args.get("to") or "").strip() or None
    from_dt = parse_iso_datetime(from_raw)
    to_dt = parse_iso_datetime(to_raw)
    if from_raw and not from_dt:
        return api_error("invalid from datetime", status=400)
    if to_raw and not to_dt:
        return api_error("invalid to datetime", status=400)
    if from_dt and to_dt and to_dt < from_dt:
        return api_error("to must be >= from", status=400)

    query = g.db.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action == action)
    if actor_username:
        query = query.filter(AuditLog.actor_username == actor_username)
    if from_dt:
        query = query.filter(AuditLog.created_at >= to_iso_z(from_dt))
    if to_dt:
        query = query.filter(AuditLog.created_at <= to_iso_z(to_dt))
    query = query.order_by(AuditLog.created_at.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    items = []
    for row in rows:
        detail = None
        if row.detail_json:
            try:
                detail = json.loads(row.detail_json)
            except Exception:
                detail = None
        items.append(
            {
                "id": row.id,
                "actor_username": row.actor_username,
                "actor_role": row.actor_role,
                "action": row.action,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "detail": detail,
                "created_at": row.created_at,
            }
        )
    return api_ok({"items": items, "total": total, "limit": limit, "offset": offset})


@bp.route("/api/admin/system/reset", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def admin_system_reset():
    data = request.get_json(silent=True) or {}
    confirm_resp = require_confirm_phrase(data, RESET_ALL_TEST_DATA_CONFIRM)
    if confirm_resp:
        return confirm_resp
    keep_questions = parse_bool(data.get("keep_questions"), default=True)
    keep_users = parse_bool(data.get("keep_users"), default=True)

    with transactional(g.db):
        deleted_counts = {
            "answers": g.db.query(Answer).delete(),
            "attempts": g.db.query(Attempt).delete(),
            "exam_questions": g.db.query(ExamQuestion).delete(),
            "exams": g.db.query(Exam).delete(),
            "wrong_questions": g.db.query(WrongQuestion).delete(),
            "student_profiles": g.db.query(StudentProfile).delete(),
            "classes": g.db.query(SchoolClass).delete(),
            "schools": g.db.query(School).delete(),
            "sessions": g.db.query(SessionToken).delete(),
        }
        if not keep_questions:
            deleted_counts["questions"] = g.db.query(Question).delete()
        if not keep_users:
            deleted_counts["teachers"] = g.db.query(User).filter(User.role == "teacher").delete()
            deleted_counts["client_users"] = g.db.query(ClientUser).delete()
        add_audit_log(
            "SYSTEM_RESET",
            target_type="system",
            target_id="test_data",
            detail={"deleted": deleted_counts, "keep_questions": keep_questions, "keep_users": keep_users},
        )
    return api_ok({"reset": True, "deleted": deleted_counts, "keep_questions": keep_questions, "keep_users": keep_users})


@bp.route("/api/teacher/classes", methods=["GET"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_list_classes():
    limit, offset = parse_limit_offset()
    query = g.db.query(SchoolClass)
    if getattr(g, "current_role", None) == "teacher":
        query = query.filter(SchoolClass.teacher_username == getattr(g, "current_user", None))
    status = (request.args.get("status") or "").strip()
    if status:
        query = query.filter(SchoolClass.status == status)
    query = query.order_by(SchoolClass.created_at.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    school_ids = [row.school_id for row in rows]
    schools = g.db.query(School).filter(School.id.in_(school_ids)).all() if school_ids else []
    school_map = {x.id: x for x in schools}
    class_ids = [row.id for row in rows]
    student_counts = {}
    if class_ids:
        count_rows = (
            g.db.query(StudentProfile.class_id, func.count(StudentProfile.id))
            .filter(
                StudentProfile.class_id.in_(class_ids),
                StudentProfile.status == "active",
            )
            .group_by(StudentProfile.class_id)
            .all()
        )
        student_counts = {row[0]: row[1] for row in count_rows}
    items = []
    for row in rows:
        school = school_map.get(row.school_id)
        items.append(
            {
                "class_id": row.id,
                "school_id": row.school_id,
                "school_name": school.name if school else None,
                "grade": row.grade,
                "name": row.name,
                "status": row.status,
                "student_count": int(student_counts.get(row.id, 0)),
                "created_at": row.created_at,
            }
        )
    return v1_ok({"items": items, "total": total, "limit": limit, "offset": offset})


@bp.route("/api/teacher/classes/<int:class_id>/students", methods=["GET"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_class_students(class_id):
    class_row, error_resp = ensure_class_access(class_id)
    if error_resp:
        return error_resp
    limit, offset = parse_limit_offset()
    query = (
        g.db.query(StudentProfile)
        .filter(StudentProfile.class_id == class_row.id, StudentProfile.status == "active")
        .order_by(StudentProfile.student_no.asc(), StudentProfile.id.asc())
    )
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    items = [
        {
            "student_id": row.id,
            "student_no": row.student_no,
            "name": row.name,
            "gender": row.gender,
            "status": row.status,
            "client_username": row.client_username,
        }
        for row in rows
    ]
    return v1_ok({"items": items, "total": total, "limit": limit, "offset": offset})


@bp.route("/api/teacher/classes", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_create_class():
    data = request.get_json(silent=True) or {}
    school_id = parse_int(data.get("school_id"), None)
    grade = (data.get("grade") or "").strip()
    name = (data.get("name") or "").strip()
    status = str(data.get("status") or "active").strip() or "active"
    if school_id is None or not grade or not name:
        return api_error("school_id, grade, name are required", status=400)
    if status not in VALID_CLASS_STATUS:
        return api_error("status must be active or dismissed", status=400)
    school = g.db.query(School).filter(School.id == school_id).first()
    if not school:
        return api_error("school not found", status=404)
    teacher_username = (data.get("teacher_username") or "").strip() or None
    teacher = None
    if teacher_username:
        teacher = g.db.query(User).filter(User.username == teacher_username).first()
        if not teacher:
            return api_error("teacher user not found", status=404)
        if teacher.role != "teacher" or int(teacher.is_active or 0) != 1:
            return api_error("teacher user must be active teacher", status=409)
    with transactional(g.db):
        row = SchoolClass(
            school_id=school_id,
            grade=grade,
            name=name,
            teacher_username=teacher_username,
            status=status,
            created_at=utc_now_iso(),
        )
        g.db.add(row)
        g.db.flush()
        result = class_to_dict(row)
    return api_ok({"class": result}, status=201)


@bp.route("/api/teacher/classes/<int:class_id>/dismiss", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_dismiss_class(class_id):
    class_row, error_resp = ensure_class_access(class_id)
    if error_resp:
        return error_resp
    with transactional(g.db):
        class_row.status = "dismissed"
    return api_ok({"class": class_to_dict(class_row)})


@bp.route("/api/teacher/classes/<int:class_id>/students/batch_add", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_batch_add_students(class_id):
    class_row, error_resp = ensure_class_access(class_id)
    if error_resp:
        return error_resp
    if class_row.status == "dismissed":
        return api_error("cannot assign students to dismissed class", status=400)
    data = request.get_json(silent=True) or {}
    student_ids = data.get("student_ids") or []
    if not isinstance(student_ids, list) or not student_ids:
        return api_error("student_ids array is required", status=400)
    parsed_ids = [sid for sid in [parse_int(x, None) for x in student_ids] if sid is not None]
    if not parsed_ids:
        return api_error("no valid student_ids", status=400)
    students = g.db.query(StudentProfile).filter(StudentProfile.id.in_(parsed_ids)).all()
    with transactional(g.db):
        for student in students:
            if student.status != "active":
                continue
            student.class_id = class_id
            if student.school_id is None:
                student.school_id = class_row.school_id
            if not student.grade:
                student.grade = class_row.grade
    return api_ok({"updated": len(students)})


@bp.route("/api/teacher/classes/<int:class_id>/students/batch_remove", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_batch_remove_students(class_id):
    class_row, error_resp = ensure_class_access(class_id)
    if error_resp:
        return error_resp
    data = request.get_json(silent=True) or {}
    student_ids = data.get("student_ids") or []
    if not isinstance(student_ids, list) or not student_ids:
        return api_error("student_ids array is required", status=400)
    parsed_ids = [sid for sid in [parse_int(x, None) for x in student_ids] if sid is not None]
    if not parsed_ids:
        return api_error("no valid student_ids", status=400)
    students = g.db.query(StudentProfile).filter(StudentProfile.id.in_(parsed_ids), StudentProfile.class_id == class_id).all()
    with transactional(g.db):
        for student in students:
            student.class_id = None
    return api_ok({"updated": len(students)})


@bp.route("/api/teacher/exams", methods=["POST"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_create_exam():
    data = request.get_json(silent=True) or {}
    class_id = parse_int(data.get("class_id"), None)
    question_count = parse_int(data.get("question_count"), None)
    title = (data.get("title") or "").strip() or f"Exam {datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    category = (data.get("category") or "").strip() or None
    start_at = data.get("start_at")
    end_at = data.get("end_at")
    allow_multiple_attempts = 1 if parse_bool(data.get("allow_multiple_attempts"), default=False) else 0
    exam_type = str(data.get("type") or "exam").strip() or "exam"
    target_student_id = parse_int(data.get("target_student_id"), None)
    question_ids, question_ids_error = parse_question_ids(data.get("question_ids"))
    if question_ids_error:
        return question_ids_error
    if exam_type not in {"exam", "practice"}:
        return v1_error("invalid_params", status=400, reason="type must be exam or practice")
    if class_id is None:
        return v1_error("invalid_params", status=400, reason="class_id is required")
    if question_ids is None and (question_count is None or question_count <= 0):
        return v1_error("invalid_params", status=400, reason="positive question_count is required when question_ids is empty")
    class_row, error_resp = ensure_class_access(class_id)
    if error_resp:
        return error_resp
    if target_student_id is not None:
        target_student = g.db.query(StudentProfile).filter(StudentProfile.id == target_student_id).first()
        if not target_student or target_student.status != "active":
            return v1_error("not_found", status=404, reason="target student not found")
        if target_student.class_id != class_row.id:
            return v1_error("invalid_params", status=400, reason="target student not in class")
    if class_row.status == "dismissed":
        return v1_error("conflict", status=409, reason="dismissed class cannot publish new exams")
    start_dt = parse_iso_datetime(start_at)
    end_dt = parse_iso_datetime(end_at)
    if start_at and not start_dt:
        return lifecycle_error("422_TIME_INVALID", status=422, reason="start_at must be ISO8601")
    if end_at and not end_dt:
        return lifecycle_error("422_TIME_INVALID", status=422, reason="end_at must be ISO8601")
    if start_dt and end_dt and end_dt < start_dt:
        return lifecycle_error("422_TIME_INVALID", status=422, reason="end_at must be after start_at")

    selected = []
    if question_ids is not None:
        rows = g.db.query(Question).filter(Question.id.in_(question_ids)).all()
        qmap = {row.id: row for row in rows}
        missing_ids = [qid for qid in question_ids if qid not in qmap]
        if missing_ids:
            return lifecycle_error("404_NOT_FOUND", status=404, reason="some question_ids not found", data={"missing_question_ids": missing_ids})
        selected = [qmap[qid] for qid in question_ids]
        question_count = len(selected)
    else:
        question_query = g.db.query(Question)
        if category:
            question_query = question_query.filter(Question.category == category)
        bank = question_query.all()
        if len(bank) < question_count:
            return v1_error(
                "conflict",
                status=409,
                reason="not enough questions",
                data={"available": len(bank), "requested": question_count},
            )
        selected = random.sample(bank, question_count)

    exam_id = ("e_" if exam_type == "exam" else "p_") + uuid.uuid4().hex[:8]
    with transactional(g.db):
        exam_row = Exam(
            id=exam_id,
            title=title,
            class_id=class_id,
            created_by=getattr(g, "current_user", None),
            question_count=question_count,
            category=category,
            start_at=to_iso_z(start_dt),
            end_at=to_iso_z(end_dt),
            allow_multiple_attempts=allow_multiple_attempts,
            exam_type=exam_type,
            target_student_profile_id=target_student_id,
            status="draft",
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
        g.db.add(exam_row)
        for q in selected:
            g.db.add(ExamQuestion(exam_id=exam_id, question_id=q.id))
        add_audit_log(
            "CREATE_EXAM_DRAFT",
            target_type="exams",
            target_id=exam_id,
            detail={
                "class_id": class_id,
                "title": title,
                "question_count": question_count,
                "category": category,
                "type": exam_type,
            },
        )
    return v1_ok(
        {
            "exam_id": exam_row.id,
            "status": exam_row.status,
            "question_count": exam_row.question_count,
            "class_id": exam_row.class_id,
            "created_at": exam_row.created_at,
            "updated_at": exam_row.updated_at,
            "allow_multiple_attempts": bool(int(exam_row.allow_multiple_attempts or 0)),
            "type": exam_row.exam_type,
            "target_student_id": exam_row.target_student_profile_id,
        }
    )


@bp.route("/api/teacher/exams", methods=["GET"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_list_exams():
    limit, offset = parse_limit_offset()
    query = g.db.query(Exam)
    class_id = parse_int(request.args.get("class_id"), None)
    status = (request.args.get("status") or "").strip().lower()
    keyword = (request.args.get("keyword") or "").strip()
    from_dt, to_dt, error_resp = parse_time_window()
    if error_resp:
        return error_resp
    if class_id is not None:
        class_row, error_resp = ensure_class_access(class_id)
        if error_resp:
            return error_resp
        query = query.filter(Exam.class_id == class_row.id)
    if getattr(g, "current_role", None) == "teacher":
        owned_ids = owned_class_ids()
        if not owned_ids:
            return v1_ok({"items": [], "total": 0, "limit": limit, "offset": offset})
        query = query.filter(Exam.class_id.in_(owned_ids))
    if keyword:
        query = query.filter(or_(Exam.id.like(f"%{keyword}%"), Exam.title.like(f"%{keyword}%")))
    if from_dt:
        query = query.filter(Exam.created_at >= to_iso_z(from_dt))
    if to_dt:
        query = query.filter(Exam.created_at <= to_iso_z(to_dt))

    if status and status not in VALID_EXAM_STATUS:
        return v1_error("invalid_params", status=400, reason="status must be one of draft/published/active/ended/archived")

    now_dt = datetime.now(timezone.utc)
    if not status:
        query = query.filter(Exam.status != "archived")
        query = query.order_by(Exam.created_at.desc())
        total = query.count()
        rows = query.offset(offset).limit(limit).all()
    elif status in {"draft", "archived"}:
        query = query.filter(Exam.status == status).order_by(Exam.created_at.desc())
        total = query.count()
        rows = query.offset(offset).limit(limit).all()
    else:
        if status == "active":
            candidate_rows = query.filter(Exam.status.in_(["published", "active"])).order_by(Exam.created_at.desc()).all()
        elif status == "ended":
            candidate_rows = query.filter(Exam.status.in_(["published", "ended"])).order_by(Exam.created_at.desc()).all()
        else:
            candidate_rows = query.filter(Exam.status == "published").order_by(Exam.created_at.desc()).all()
        filtered = [row for row in candidate_rows if get_exam_effective_status(row, now=now_dt) == status]
        total = len(filtered)
        rows = filtered[offset : offset + limit]

    class_ids = [row.class_id for row in rows]
    classes = g.db.query(SchoolClass).filter(SchoolClass.id.in_(class_ids)).all() if class_ids else []
    class_map = {x.id: x for x in classes}

    exam_ids = [row.id for row in rows]
    attempt_counts: Dict[str, int] = {}
    submitted_counts: Dict[str, int] = {}
    if exam_ids:
        attempts_group = g.db.query(Attempt.exam_id, func.count(Attempt.id)).filter(Attempt.exam_id.in_(exam_ids)).group_by(Attempt.exam_id).all()
        submitted_group = (
            g.db.query(Attempt.exam_id, func.count(Attempt.id))
            .filter(Attempt.exam_id.in_(exam_ids), Attempt.submitted_at.isnot(None))
            .group_by(Attempt.exam_id)
            .all()
        )
        attempt_counts = {row[0]: int(row[1] or 0) for row in attempts_group}
        submitted_counts = {row[0]: int(row[1] or 0) for row in submitted_group}

    items = []
    for row in rows:
        class_row = class_map.get(row.class_id)
        attempts_total = int(attempt_counts.get(row.id, 0))
        submitted_total = int(submitted_counts.get(row.id, 0))
        items.append(
            {
                "exam_id": row.id,
                "title": row.title,
                "class_id": row.class_id,
                "class_name": class_row.name if class_row else None,
                "category": row.category,
                "question_count": row.question_count,
                "start_at": row.start_at,
                "end_at": row.end_at,
                "status": row.status,
                "effective_status": get_exam_effective_status(row, now=now_dt),
                "created_at": row.created_at,
                "updated_at": getattr(row, "updated_at", None),
                "allow_multiple_attempts": bool(int(getattr(row, "allow_multiple_attempts", 0) or 0)),
                "type": row.exam_type or "exam",
                "target_student_id": row.target_student_profile_id,
                "stats": {
                    "attempts_total": attempts_total,
                    "submitted_total": submitted_total,
                },
            }
        )
    return v1_ok({"items": items, "total": total, "limit": limit, "offset": offset})


@bp.route("/api/teacher/exams/<exam_id>", methods=["GET"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_get_exam(exam_id):
    exam, class_row, error_resp = ensure_exam_access(exam_id)
    if error_resp:
        return error_resp
    submitted_rows = g.db.query(Attempt).filter(Attempt.exam_id == exam.id, Attempt.submitted_at.isnot(None)).all()
    submitted_scores = [row.score for row in submitted_rows if row.score is not None]
    stats = {
        "attempts_total": g.db.query(Attempt).filter(Attempt.exam_id == exam.id).count(),
        "submitted_total": len(submitted_rows),
        "avg_score": round(sum(submitted_scores) / len(submitted_scores), 2) if submitted_scores else 0,
        "max_score": max(submitted_scores) if submitted_scores else 0,
        "min_score": min(submitted_scores) if submitted_scores else 0,
    }
    return v1_ok(build_exam_payload(exam, class_row=class_row, stats=stats))


@bp.route("/api/teacher/exams/<exam_id>", methods=["PATCH"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_update_exam(exam_id):
    exam, class_row, error_resp = ensure_exam_access_lifecycle(exam_id)
    if error_resp:
        return error_resp

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return v1_error("invalid_params", status=400, reason="json body is required")

    if exam.status in {"ended", "archived"}:
        return lifecycle_error("400_INVALID_STATE", status=400, reason="ended or archived exam cannot be updated")

    title = exam.title
    if "title" in data:
        title = str(data.get("title") or "").strip()
        if not title:
            return v1_error("invalid_params", status=400, reason="title cannot be empty")

    start_at_raw = exam.start_at
    if "start_at" in data:
        start_at_raw = data.get("start_at")
    end_at_raw = exam.end_at
    if "end_at" in data:
        end_at_raw = data.get("end_at")
    start_dt = parse_iso_datetime(start_at_raw)
    end_dt = parse_iso_datetime(end_at_raw)
    if start_at_raw and not start_dt:
        return lifecycle_error("422_TIME_INVALID", status=422, reason="start_at must be ISO8601")
    if end_at_raw and not end_dt:
        return lifecycle_error("422_TIME_INVALID", status=422, reason="end_at must be ISO8601")
    if start_dt and end_dt and end_dt < start_dt:
        return lifecycle_error("422_TIME_INVALID", status=422, reason="end_at must be after start_at")

    allow_multiple_attempts = int(getattr(exam, "allow_multiple_attempts", 0) or 0)
    if "allow_multiple_attempts" in data:
        allow_multiple_attempts = 1 if parse_bool(data.get("allow_multiple_attempts"), default=False) else 0

    question_fields_touched = any(key in data for key in ["question_ids", "question_count", "category", "class_id"])
    if exam.status != "draft" and question_fields_touched:
        return lifecycle_error("400_INVALID_STATE", status=400, reason="published exam cannot change class or question set")

    next_class_id = exam.class_id
    if exam.status == "draft" and "class_id" in data:
        next_class_id = parse_int(data.get("class_id"), None)
        if next_class_id is None:
            return v1_error("invalid_params", status=400, reason="class_id must be integer")
        next_class_row, class_error = ensure_class_access(next_class_id)
        if class_error:
            return class_error
        if next_class_row.status == "dismissed":
            return lifecycle_error("400_INVALID_STATE", status=400, reason="dismissed class cannot be used")
        class_row = next_class_row

    next_category = exam.category
    if "category" in data:
        next_category = (data.get("category") or "").strip() or None

    selected_questions = None
    next_question_count = int(exam.question_count or 0)
    if exam.status == "draft":
        question_ids, question_ids_error = parse_question_ids(data.get("question_ids")) if "question_ids" in data else (None, None)
        if question_ids_error:
            return question_ids_error

        if question_ids is not None:
            rows = g.db.query(Question).filter(Question.id.in_(question_ids)).all()
            qmap = {row.id: row for row in rows}
            missing_ids = [qid for qid in question_ids if qid not in qmap]
            if missing_ids:
                return lifecycle_error("404_NOT_FOUND", status=404, reason="some question_ids not found", data={"missing_question_ids": missing_ids})
            selected_questions = [qmap[qid] for qid in question_ids]
            next_question_count = len(selected_questions)
        elif any(key in data for key in ["question_count", "category"]):
            next_question_count = parse_int(data.get("question_count"), exam.question_count)
            if next_question_count is None or next_question_count <= 0:
                return v1_error("invalid_params", status=400, reason="question_count must be positive")
            question_query = g.db.query(Question)
            if next_category:
                question_query = question_query.filter(Question.category == next_category)
            bank = question_query.all()
            if len(bank) < next_question_count:
                return v1_error(
                    "conflict",
                    status=409,
                    reason="not enough questions",
                    data={"available": len(bank), "requested": next_question_count},
                )
            selected_questions = random.sample(bank, next_question_count)

    with transactional(g.db):
        exam.title = title
        exam.class_id = next_class_id
        exam.category = next_category
        exam.start_at = to_iso_z(start_dt)
        exam.end_at = to_iso_z(end_dt)
        exam.allow_multiple_attempts = allow_multiple_attempts
        if selected_questions is not None:
            g.db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam.id).delete()
            for q in selected_questions:
                g.db.add(ExamQuestion(exam_id=exam.id, question_id=q.id))
            exam.question_count = next_question_count
        exam.updated_at = utc_now_iso()
        add_audit_log(
            "UPDATE_EXAM",
            target_type="exams",
            target_id=exam.id,
            detail={
                "status": exam.status,
                "class_id": exam.class_id,
                "question_count": exam.question_count,
                "category": exam.category,
                "start_at": exam.start_at,
                "end_at": exam.end_at,
                "allow_multiple_attempts": bool(allow_multiple_attempts),
            },
        )

    stats = get_exam_attempt_stats(exam.id)
    return v1_ok(build_exam_payload(exam, class_row=class_row, stats=stats))


@bp.route("/api/teacher/exams/<exam_id>/publish", methods=["POST"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_publish_exam(exam_id):
    exam, class_row, error_resp = ensure_exam_access_lifecycle(exam_id)
    if error_resp:
        return error_resp
    if exam.status != "draft":
        return lifecycle_error("400_INVALID_STATE", status=400, reason="only draft exam can be published")
    if class_row.status == "dismissed":
        return lifecycle_error("400_INVALID_STATE", status=400, reason="dismissed class cannot publish new exams")

    qcount = g.db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam.id).count()
    if qcount <= 0:
        return lifecycle_error("400_INVALID_STATE", status=400, reason="exam has no questions")

    with transactional(g.db):
        exam.question_count = int(qcount)
        exam.status = "published"
        exam.updated_at = utc_now_iso()
        add_audit_log("PUBLISH_EXAM", target_type="exams", target_id=exam.id, detail={"question_count": qcount})
    return v1_ok(build_exam_payload(exam, class_row=class_row, stats=get_exam_attempt_stats(exam.id)))


@bp.route("/api/teacher/exams/<exam_id>/unpublish", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_unpublish_exam(exam_id):
    exam, class_row, error_resp = ensure_exam_access_lifecycle(exam_id)
    if error_resp:
        return error_resp
    if exam.status != "published":
        return lifecycle_error("400_INVALID_STATE", status=400, reason="only published exam can be withdrawn")
    effective_status = get_exam_effective_status(exam)
    if effective_status != "published":
        return lifecycle_error("400_INVALID_STATE", status=400, reason="active or ended exam cannot be withdrawn")

    stats = get_exam_attempt_stats(exam.id)
    if int(stats["attempts_total"]) > 0 or int(stats["submitted_total"]) > 0:
        return lifecycle_error("409_HAS_SUBMISSIONS", status=409, reason="exam already has attempts or submissions")

    with transactional(g.db):
        exam.status = "draft"
        exam.updated_at = utc_now_iso()
        add_audit_log("UNPUBLISH_EXAM", target_type="exams", target_id=exam.id, detail={"attempts_total": stats["attempts_total"]})
    return v1_ok(build_exam_payload(exam, class_row=class_row, stats=stats))


@bp.route("/api/teacher/exams/<exam_id>/end", methods=["POST"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_end_exam(exam_id):
    exam, class_row, error_resp = ensure_exam_access_lifecycle(exam_id)
    if error_resp:
        return error_resp
    if exam.status not in {"published", "active"}:
        return lifecycle_error("400_INVALID_STATE", status=400, reason="only published or active exam can be ended")

    now_iso = utc_now_iso()
    now_dt = parse_iso_datetime(now_iso)
    end_dt = parse_iso_datetime(exam.end_at)
    with transactional(g.db):
        exam.status = "ended"
        if not end_dt or (now_dt and end_dt > now_dt):
            exam.end_at = to_iso_z(now_dt)
        exam.updated_at = now_iso
        add_audit_log("END_EXAM", target_type="exams", target_id=exam.id, detail={"end_at": exam.end_at})
    return v1_ok(build_exam_payload(exam, class_row=class_row, stats=get_exam_attempt_stats(exam.id)))


@bp.route("/api/teacher/exams/<exam_id>/archive", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_archive_exam(exam_id):
    exam, class_row, error_resp = ensure_exam_access_lifecycle(exam_id)
    if error_resp:
        return error_resp
    if exam.status == "archived":
        return v1_ok(build_exam_payload(exam, class_row=class_row, stats=get_exam_attempt_stats(exam.id)))
    if exam.status not in {"draft", "published", "active", "ended"}:
        return lifecycle_error("400_INVALID_STATE", status=400, reason="exam cannot be archived from current state")

    from_status = exam.status
    with transactional(g.db):
        exam.status = "archived"
        exam.updated_at = utc_now_iso()
        add_audit_log("ARCHIVE_EXAM", target_type="exams", target_id=exam.id, detail={"from_status": from_status})
    return v1_ok(build_exam_payload(exam, class_row=class_row, stats=get_exam_attempt_stats(exam.id)))


@bp.route("/api/teacher/exams/<exam_id>", methods=["DELETE"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_delete_exam(exam_id):
    exam, class_row, error_resp = ensure_exam_access_lifecycle(exam_id)
    if error_resp:
        return error_resp
    if exam.status != "draft":
        return lifecycle_error("400_INVALID_STATE", status=400, reason="only draft exam can be deleted")

    stats = get_exam_attempt_stats(exam.id)
    if int(stats["attempts_total"]) > 0 or int(stats["submitted_total"]) > 0:
        return lifecycle_error("409_HAS_SUBMISSIONS", status=409, reason="exam already has attempts or submissions")

    with transactional(g.db):
        exam.status = "archived"
        exam.updated_at = utc_now_iso()
        add_audit_log("DELETE_EXAM_SOFT", target_type="exams", target_id=exam.id, detail={"from_status": "draft"})
    payload = build_exam_payload(exam, class_row=class_row, stats=stats)
    payload["deleted"] = True
    return v1_ok(payload)


@bp.route("/api/teacher/exams/<exam_id>/questions", methods=["GET"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_exam_questions(exam_id):
    exam, _class_row, error_resp = ensure_exam_access(exam_id)
    if error_resp:
        return error_resp
    include_answer = parse_bool(request.args.get("include_answer"), default=False)
    include_analysis = parse_bool(request.args.get("include_analysis"), default=False)
    links = g.db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam.id).all()
    qids = [row.question_id for row in links]
    questions = g.db.query(Question).filter(Question.id.in_(qids)).all() if qids else []
    qmap = {q.id: q for q in questions}
    items = []
    for qid in qids:
        question = qmap.get(qid)
        if not question:
            continue
        item = {
            "question_id": question.id,
            "stem": question.stem,
            "options": json.loads(question.options or "[]"),
            "category": question.category,
        }
        if include_answer:
            item["answer"] = question.answer
        if include_analysis:
            item["analysis"] = question.analysis
        items.append(item)
    return v1_ok({"exam_id": exam.id, "items": items})


@bp.route("/api/teacher/exams/<exam_id>/question_stats", methods=["GET"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_exam_question_stats(exam_id):
    exam, _class_row, error_resp = ensure_exam_access(exam_id)
    if error_resp:
        return error_resp

    links = g.db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam.id).all()
    qids = [row.question_id for row in links]
    questions = g.db.query(Question).filter(Question.id.in_(qids)).all() if qids else []
    qmap = {q.id: q for q in questions}
    option_limit_map = {q.id: max(4, len(json.loads(q.options or "[]"))) for q in questions}

    submitted_attempts = (
        g.db.query(Attempt)
        .filter(Attempt.exam_id == exam.id, Attempt.submitted_at.isnot(None))
        .order_by(Attempt.submitted_at.desc())
        .all()
    )
    latest_attempts = {}
    for attempt in submitted_attempts:
        if attempt.student_profile_id not in latest_attempts:
            latest_attempts[attempt.student_profile_id] = attempt
    attempt_ids = [row.id for row in latest_attempts.values()]

    answer_rows = g.db.query(Answer).filter(Answer.attempt_id.in_(attempt_ids)).all() if attempt_ids else []
    stats_map = {}
    for row in answer_rows:
        if row.your is None:
            continue
        try:
            option_index = int(row.your)
        except (TypeError, ValueError):
            continue
        if option_index < 0:
            continue
        item = stats_map.setdefault(
            row.question_id,
            {"answer_count": 0, "correct_count": 0, "option_counts": {}, "max_option_index": -1},
        )
        item["answer_count"] += 1
        item["option_counts"][option_index] = int(item["option_counts"].get(option_index, 0)) + 1
        item["max_option_index"] = max(int(item.get("max_option_index", -1)), option_index)
        if int(row.is_correct or 0) == 1:
            item["correct_count"] += 1

    items = []
    submitted_total = len(latest_attempts)
    for index, qid in enumerate(qids, start=1):
        question = qmap.get(qid)
        if not question:
            continue
        options = json.loads(question.options or "[]")
        stat = stats_map.get(qid, {})
        answer_count = int(stat.get("answer_count", 0))
        correct_count = int(stat.get("correct_count", 0))
        option_counts = stat.get("option_counts", {})
        max_option_index = max(int(stat.get("max_option_index", -1)), max(option_counts.keys(), default=-1))
        option_total = max(4, len(options), max_option_index + 1)
        option_stats = []
        for option_index in range(option_total):
            option_label = chr(65 + option_index) if option_index < 26 else str(option_index + 1)
            if option_index < len(options):
                option_text = options[option_index]
            elif int(option_counts.get(option_index, 0)) > 0:
                option_text = f"[historical option {option_index + 1}]"
            else:
                option_text = ""
            option_stats.append(
                {
                    "index": option_index,
                    "label": option_label,
                    "text": option_text,
                    "count": int(option_counts.get(option_index, 0)),
                    "is_correct": option_index == int(question.answer or 0),
                }
            )
        items.append(
            {
                "question_id": question.id,
                "sequence": index,
                "stem": question.stem,
                "category": question.category,
                "answer": int(question.answer or 0),
                "answer_label": chr(65 + int(question.answer or 0)) if int(question.answer or 0) < 26 else str(int(question.answer or 0) + 1),
                "answer_text": options[int(question.answer or 0)] if int(question.answer or 0) < len(options) else "",
                "submitted_count": submitted_total,
                "answered_count": answer_count,
                "correct_count": correct_count,
                "wrong_count": max(answer_count - correct_count, 0),
                "accuracy": normalized_accuracy(correct_count, answer_count),
                "options": option_stats,
            }
        )

    return v1_ok({"exam_id": exam.id, "submitted_total": submitted_total, "items": items})


@bp.route("/api/teacher/exams/<exam_id>/attempts", methods=["GET"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_exam_attempts(exam_id):
    exam, _class_row, error_resp = ensure_exam_access(exam_id)
    if error_resp:
        return error_resp
    limit, offset = parse_pagination(default_limit=50, max_limit=500)
    query = g.db.query(Attempt).filter(Attempt.exam_id == exam.id).order_by(Attempt.started_at.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    student_ids = [a.student_profile_id for a in rows]
    profiles = g.db.query(StudentProfile).filter(StudentProfile.id.in_(student_ids)).all() if student_ids else []
    profile_map = {p.id: p for p in profiles}
    items = []
    for row in rows:
        payload = attempt_to_dict(row)
        profile = profile_map.get(row.student_profile_id)
        if profile:
            payload["student"] = student_to_dict(profile)
        items.append(payload)
    return api_ok({"items": items, "total": total, "limit": limit, "offset": offset})


@bp.route("/api/client/exams", methods=["GET"])
@login_required(api=True)
@role_required(["client"])
def client_list_exams():
    profile, error_resp = ensure_client_profile(require_class=True)
    if error_resp:
        return v1_error("forbidden", status=403, reason="student profile not assigned to class")
    class_row = load_class(profile.class_id)
    if not class_row:
        return v1_error("not_found", status=404, reason="class not found")
    if class_row.status == "dismissed":
        return v1_error("forbidden", status=403, reason="class is dismissed")

    limit, offset = parse_limit_offset()
    candidates = g.db.query(Exam).filter(Exam.class_id == profile.class_id).order_by(Exam.created_at.desc()).all()
    now = datetime.now(timezone.utc)
    available = []
    for exam in candidates:
        if exam.status != "published":
            continue
        if (exam.exam_type or "exam") == "practice":
            if exam.target_student_profile_id != profile.id:
                continue
        elif exam.target_student_profile_id is not None and exam.target_student_profile_id != profile.id:
            continue
        start_at = parse_iso_datetime(exam.start_at)
        end_at = parse_iso_datetime(exam.end_at)
        if start_at and now < start_at:
            continue
        if end_at and now > end_at:
            continue
        available.append(exam)
    total = len(available)
    rows = available[offset : offset + limit]
    items = []
    for exam in rows:
        items.append(
            {
                "exam_id": exam.id,
                "title": exam.title,
                "question_count": exam.question_count,
                "start_at": exam.start_at,
                "end_at": exam.end_at,
                "status": exam.status,
                "allow_multiple_attempts": bool(int(getattr(exam, "allow_multiple_attempts", 0) or 0)),
                "type": exam.exam_type or "exam",
                "target_student_id": exam.target_student_profile_id,
                "practice_owner": exam.created_by if (exam.exam_type or "exam") == "practice" else None,
                "selected_count": int(exam.question_count or 0) if (exam.exam_type or "exam") == "practice" else None,
            }
        )
    return v1_ok({"items": items, "total": total, "limit": limit, "offset": offset})


@bp.route("/api/client/exams/<exam_id>/start", methods=["POST"])
@login_required(api=True)
@role_required(["client"])
def client_start_exam(exam_id):
    profile, error_resp = ensure_client_profile(require_class=True)
    if error_resp:
        return v1_error("forbidden", status=403, reason="student profile not assigned to class")
    exam = load_exam(exam_id)
    if not exam:
        return v1_error("not_found", status=404, reason="exam not found")
    if profile.class_id != exam.class_id:
        return v1_error("forbidden", status=403, reason="exam not assigned to your class")
    if exam.target_student_profile_id is not None and exam.target_student_profile_id != profile.id:
        return v1_error("forbidden", status=403, reason="exam does not belong to current student")
    class_row = load_class(exam.class_id)
    if not class_row:
        return v1_error("not_found", status=404, reason="class not found")
    if class_row.status == "dismissed":
        return v1_error("forbidden", status=403, reason="class is dismissed")
    opened, msg = exam_is_open_for_action(exam)
    if not opened:
        return v1_error("forbidden", status=403, reason=msg)

    latest_submitted = (
        g.db.query(Attempt)
        .filter(Attempt.exam_id == exam.id, Attempt.student_profile_id == profile.id, Attempt.submitted_at.isnot(None))
        .order_by(Attempt.submitted_at.desc())
        .first()
    )
    allow_multiple_attempts = int(getattr(exam, "allow_multiple_attempts", 0) or 0) == 1
    if latest_submitted and not allow_multiple_attempts:
        reason = "already submitted" if (exam.exam_type or "exam") == "practice" else "attempt already submitted"
        return v1_error(
            "conflict",
            status=409,
            reason=reason,
            data={"attempt_id": latest_submitted.id},
        )

    attempt = (
        g.db.query(Attempt)
        .filter(Attempt.exam_id == exam.id, Attempt.student_profile_id == profile.id, Attempt.submitted_at.is_(None))
        .order_by(Attempt.started_at.desc())
        .first()
    )
    if attempt and getattr(attempt, "progress_count", None) is None:
        with transactional(g.db):
            attempt.progress_count = 0
    if not attempt:
        attempt = Attempt(
            id=uuid.uuid4().hex,
            exam_id=exam.id,
            student_profile_id=profile.id,
            started_at=utc_now_iso(),
            submitted_at=None,
            score=0,
            total=0,
            progress_count=0,
            duration_sec=None,
        )
        with transactional(g.db):
            g.db.add(attempt)

    links = g.db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam.id).all()
    question_ids = [x.question_id for x in links]
    questions = g.db.query(Question).filter(Question.id.in_(question_ids)).all() if question_ids else []
    qmap = {q.id: q for q in questions}
    payload_questions = []
    for qid in question_ids:
        q = qmap.get(qid)
        if q:
            payload_questions.append(
                {
                    "question_id": q.id,
                    "stem": q.stem,
                    "options": json.loads(q.options or "[]"),
                    "category": q.category,
                }
            )
    return v1_ok({"attempt_id": attempt.id, "exam_id": exam.id, "items": payload_questions})


@bp.route("/api/client/attempts/<attempt_id>/progress", methods=["POST"])
@login_required(api=True)
@role_required(["client"])
def client_update_attempt_progress(attempt_id):
    profile, error_resp = ensure_client_profile(require_class=True)
    if error_resp:
        return v1_error("forbidden", status=403, reason="student profile not assigned to class")
    attempt = g.db.query(Attempt).filter(Attempt.id == attempt_id).first()
    if not attempt:
        return v1_error("not_found", status=404, reason="attempt not found")
    if attempt.student_profile_id != profile.id:
        return v1_error("forbidden", status=403, reason="attempt does not belong to current student")
    if attempt.submitted_at:
        return v1_error("conflict", status=409, reason="attempt already submitted")
    exam = load_exam(attempt.exam_id)
    if not exam:
        return v1_error("not_found", status=404, reason="exam not found")
    opened, msg = exam_is_open_for_action(exam)
    if not opened:
        return v1_error("forbidden", status=403, reason=msg)
    data = request.get_json(silent=True) or {}
    progress_count = parse_int(data.get("progress_count"), None)
    if progress_count is None:
        return v1_error("invalid_params", status=400, reason="progress_count is required")
    question_count = max(0, int(exam.question_count or 0))
    progress_count = max(0, min(progress_count, question_count))
    with transactional(g.db):
        attempt.progress_count = progress_count
    return v1_ok({"attempt_id": attempt.id, "progress_count": progress_count, "question_count": question_count})


@bp.route("/api/client/attempts/<attempt_id>/submit", methods=["POST"])
@login_required(api=True)
@role_required(["client"])
def client_submit_attempt(attempt_id):
    profile, error_resp = ensure_client_profile(require_class=True)
    if error_resp:
        return v1_error("forbidden", status=403, reason="student profile not assigned to class")
    attempt = g.db.query(Attempt).filter(Attempt.id == attempt_id).first()
    if not attempt:
        return v1_error("not_found", status=404, reason="attempt not found")
    if attempt.student_profile_id != profile.id:
        return v1_error("forbidden", status=403, reason="attempt does not belong to current student")
    if attempt.submitted_at:
        return v1_error("conflict", status=409, reason="attempt already submitted")
    exam = load_exam(attempt.exam_id)
    if not exam:
        return v1_error("not_found", status=404, reason="exam not found")
    opened, msg = exam_is_open_for_action(exam)
    if not opened:
        return v1_error("forbidden", status=403, reason=msg)

    data = request.get_json(silent=True) or {}
    answer_mapping = parse_client_answer_mapping(data.get("answers"))
    duration_sec = parse_int(data.get("duration_sec") if data.get("duration_sec") is not None else data.get("duration"), None)
    links = g.db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam.id).all()
    question_ids = [x.question_id for x in links]
    if not question_ids:
        return v1_error("invalid_params", status=400, reason="exam has no questions")
    questions = g.db.query(Question).filter(Question.id.in_(question_ids)).all()
    qmap = {q.id: q for q in questions}
    score = 0
    total = 0
    submitted_at = utc_now_iso()
    wrong_training_config = load_wrong_training_config(g.db)
    mastery_streak = int(wrong_training_config.get("mastery_streak") or WRONG_CLEAR_STREAK)

    with transactional(g.db):
        for qid in question_ids:
            q = qmap.get(qid)
            if not q:
                continue
            total += 1
            your = answer_mapping.get(qid)
            your_value = -1 if your is None else int(your)
            correct = int(q.answer)
            is_correct = 1 if your_value == correct else 0
            avg_cost_ms = estimate_avg_cost_ms(duration_sec, len(question_ids))
            wrong_row = (
                g.db.query(WrongQuestion)
                .filter(WrongQuestion.student_profile_id == profile.id, WrongQuestion.question_id == qid)
                .first()
            )
            if is_correct:
                score += 1
                if wrong_row:
                    next_streak = int(wrong_row.correct_streak or 0) + 1
                    wrong_row.correct_streak = next_streak
                    wrong_row.last_correct_at = submitted_at
                    if avg_cost_ms is not None:
                        history_avg = parse_int(getattr(wrong_row, "avg_cost_ms", None), None)
                        wrong_row.avg_cost_ms = avg_cost_ms if history_avg is None else int((history_avg + avg_cost_ms) / 2)
                    if next_streak >= mastery_streak:
                        wrong_row.is_active = 0
            else:
                if wrong_row:
                    wrong_row.wrong_count = int(wrong_row.wrong_count or 0) + 1
                    wrong_row.correct_streak = 0
                    wrong_row.is_active = 1
                    wrong_row.last_wrong_at = submitted_at
                    if avg_cost_ms is not None:
                        history_avg = parse_int(getattr(wrong_row, "avg_cost_ms", None), None)
                        wrong_row.avg_cost_ms = avg_cost_ms if history_avg is None else int((history_avg + avg_cost_ms) / 2)
                else:
                    g.db.add(
                        WrongQuestion(
                            student_profile_id=profile.id,
                            question_id=qid,
                            wrong_count=1,
                            correct_streak=0,
                            is_active=1,
                            last_wrong_at=submitted_at,
                            last_correct_at=None,
                            last_seen_at=None,
                            avg_cost_ms=avg_cost_ms,
                        )
                    )
            g.db.add(Answer(id=uuid.uuid4().hex, attempt_id=attempt.id, question_id=qid, your=your_value, correct=correct, is_correct=is_correct))
        attempt.score = score
        attempt.total = total
        attempt.progress_count = total
        attempt.submitted_at = submitted_at
        attempt.duration_sec = duration_sec
    return v1_ok(
        {
            "attempt_id": attempt.id,
            "score": score,
            "total": total,
            "accuracy": normalized_accuracy(score, total),
            "submitted_at": attempt.submitted_at,
        }
    )


@bp.route("/api/teacher/classes/<int:class_id>/scores", methods=["GET"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_class_scores(class_id):
    class_row, error_resp = ensure_class_access(class_id)
    if error_resp:
        return error_resp
    limit, offset = parse_limit_offset()
    exam_id = (request.args.get("exam_id") or "").strip()
    if not exam_id:
        return v1_error("invalid_params", status=400, reason="exam_id is required")
    exam = g.db.query(Exam).filter(Exam.id == exam_id, Exam.class_id == class_row.id).first()
    if not exam:
        return v1_error("not_found", status=404, reason="exam not found in class")
    student_query = (
        g.db.query(StudentProfile)
        .filter(StudentProfile.class_id == class_row.id, StudentProfile.status == "active")
        .order_by(StudentProfile.student_no.asc(), StudentProfile.id.asc())
    )
    total_students = student_query.count()
    students = student_query.offset(offset).limit(limit).all()
    student_ids = [row.id for row in students]
    attempts = (
        g.db.query(Attempt)
        .filter(Attempt.exam_id == exam.id, Attempt.student_profile_id.in_(student_ids), Attempt.submitted_at.isnot(None))
        .order_by(Attempt.submitted_at.desc())
        .all()
        if student_ids
        else []
    )
    attempts_by_student: Dict[int, List[Attempt]] = {}
    for row in attempts:
        attempts_by_student.setdefault(row.student_profile_id, []).append(row)
    items = []
    for student in students:
        own_attempts = attempts_by_student.get(student.id, [])
        latest = own_attempts[0] if own_attempts else None
        submit_count = len(own_attempts)
        total_questions = latest.total if latest and latest.total is not None else exam.question_count
        items.append(
            {
                "student_id": student.id,
                "student_no": student.student_no,
                "name": student.name,
                "attempt_id": latest.id if latest else None,
                "score": latest.score if latest else None,
                "total": total_questions,
                "accuracy": normalized_accuracy(latest.score or 0, total_questions or 0) if latest else None,
                "duration_sec": latest.duration_sec if latest else None,
                "submitted_at": latest.submitted_at if latest else None,
                "submit_count": submit_count,
            }
        )
    all_submitted_rows = (
        g.db.query(Attempt)
        .filter(Attempt.exam_id == exam.id, Attempt.submitted_at.isnot(None))
        .all()
    )
    submitted_count = len(all_submitted_rows)
    summary_scores = [row.score for row in all_submitted_rows if row.score is not None]
    summary = {
        "submitted_count": submitted_count,
        "avg_score": round(sum(summary_scores) / len(summary_scores), 2) if summary_scores else 0,
        "max_score": max(summary_scores) if summary_scores else 0,
        "min_score": min(summary_scores) if summary_scores else 0,
    }
    return v1_ok(
        {
            "class_id": class_row.id,
            "exam_id": exam.id,
            "exam_title": exam.title,
            "total_students": total_students,
            "items": items,
            "summary": summary,
            "limit": limit,
            "offset": offset,
        }
    )

@bp.route("/api/teacher/students/<int:student_id>/overview", methods=["GET"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_student_overview(student_id):
    student, class_row, error_resp = ensure_student_access(student_id)
    if error_resp:
        return error_resp
    range_raw = (request.args.get("range") or "30d").strip().lower()
    query = g.db.query(Attempt).filter(Attempt.student_profile_id == student.id, Attempt.submitted_at.isnot(None))
    if range_raw.endswith("d"):
        days = parse_int(range_raw[:-1], 30) or 30
        if days <= 0:
            return v1_error("invalid_params", status=400, reason="range days must be positive")
        min_dt = datetime.now(timezone.utc) - timedelta(days=days)
        query = query.filter(Attempt.submitted_at >= to_iso_z(min_dt))
    else:
        n = parse_int(range_raw, None)
        if n is None or n <= 0:
            return v1_error("invalid_params", status=400, reason="range must be Nd or positive integer")
        query = query.order_by(Attempt.submitted_at.desc()).limit(n)
    attempts = query.all()
    attempts_count = len(attempts)
    total_questions = sum(int(row.total or 0) for row in attempts)
    correct_questions = sum(int(row.score or 0) for row in attempts)
    school = g.db.query(School).filter(School.id == student.school_id).first() if student.school_id else None
    payload = {
        "student": {
            "student_id": student.id,
            "student_no": student.student_no,
            "name": student.name,
            "gender": student.gender,
            "wrong_training_enabled": bool(int(getattr(student, "wrong_training_enabled", 0) or 0)),
            "class_id": class_row.id if class_row else student.class_id,
            "class_name": class_row.name if class_row else None,
            "school_name": school.name if school else None,
        },
        "metrics": {
            "attempts": attempts_count,
            "total_questions": total_questions,
            "correct_questions": correct_questions,
            "accuracy": normalized_accuracy(correct_questions, total_questions),
            "last_submitted_at": max((row.submitted_at for row in attempts if row.submitted_at), default=None),
        },
    }
    return v1_ok(payload)


@bp.route("/api/teacher/students/<int:student_id>/attempts", methods=["GET"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_student_attempts(student_id):
    student, _class_row, error_resp = ensure_student_access(student_id)
    if error_resp:
        return error_resp
    limit, offset = parse_limit_offset()
    exam_id = (request.args.get("exam_id") or "").strip() or None
    from_dt, to_dt, error_resp = parse_time_window()
    if error_resp:
        return error_resp
    query = g.db.query(Attempt).filter(Attempt.student_profile_id == student.id, Attempt.submitted_at.isnot(None))
    if exam_id:
        query = query.filter(Attempt.exam_id == exam_id)
    if from_dt:
        query = query.filter(Attempt.submitted_at >= to_iso_z(from_dt))
    if to_dt:
        query = query.filter(Attempt.submitted_at <= to_iso_z(to_dt))
    query = query.order_by(Attempt.submitted_at.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    exam_ids = [a.exam_id for a in rows]
    exams = g.db.query(Exam).filter(Exam.id.in_(exam_ids)).all() if exam_ids else []
    emap = {e.id: e for e in exams}
    items = []
    for row in rows:
        exam = emap.get(row.exam_id)
        total_q = int(row.total or 0)
        score_val = int(row.score or 0)
        items.append(
            {
                "attempt_id": row.id,
                "exam_id": row.exam_id,
                "exam_title": exam.title if exam else None,
                "score": score_val,
                "total": total_q,
                "accuracy": normalized_accuracy(score_val, total_q),
                "duration_sec": row.duration_sec,
                "submitted_at": row.submitted_at,
            }
        )
    return v1_ok({"items": items, "total": total, "limit": limit, "offset": offset})


@bp.route("/api/teacher/attempts/<attempt_id>", methods=["GET"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_attempt_detail(attempt_id):
    attempt = g.db.query(Attempt).filter(Attempt.id == attempt_id).first()
    if not attempt:
        return v1_error("not_found", status=404, reason="attempt not found")
    exam = load_exam(attempt.exam_id)
    if not exam:
        return v1_error("not_found", status=404, reason="exam not found")
    class_row = load_class(exam.class_id)
    if not class_row:
        return v1_error("not_found", status=404, reason="class not found")
    if not can_access_class(class_row):
        return v1_error("forbidden", status=403, reason="class not owned")
    student = g.db.query(StudentProfile).filter(StudentProfile.id == attempt.student_profile_id).first()
    if not student:
        return v1_error("not_found", status=404, reason="student not found")
    include_analysis = parse_bool(request.args.get("include_analysis"), default=True)
    answer_rows = g.db.query(Answer).filter(Answer.attempt_id == attempt.id).all()
    question_ids = [x.question_id for x in answer_rows]
    questions = g.db.query(Question).filter(Question.id.in_(question_ids)).all() if question_ids else []
    qmap = {q.id: q for q in questions}
    items = []
    for row in answer_rows:
        question = qmap.get(row.question_id)
        if not question:
            continue
        item = {
            "question_id": row.question_id,
            "stem": question.stem,
            "options": json.loads(question.options or "[]"),
            "your": row.your,
            "correct": row.correct,
            "is_correct": bool(row.is_correct),
            "category": question.category,
        }
        if include_analysis:
            item["analysis"] = question.analysis
        items.append(item)
    total_q = int(attempt.total or 0)
    score_val = int(attempt.score or 0)
    return v1_ok(
        {
            "attempt": {
                "attempt_id": attempt.id,
                "exam_id": exam.id,
                "exam_title": exam.title,
                "student_id": student.id,
                "student_no": student.student_no,
                "student_name": student.name,
                "score": score_val,
                "total": total_q,
                "accuracy": normalized_accuracy(score_val, total_q),
                "started_at": attempt.started_at,
                "submitted_at": attempt.submitted_at,
                "duration_sec": attempt.duration_sec,
            },
            "items": items,
        }
    )


@bp.route("/api/teacher/wrong_training/config", methods=["GET"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_wrong_training_config_get():
    config = load_wrong_training_config(g.db)
    return v1_ok({"config": config, "priority_rule": wrong_training_priority_label()})


@bp.route("/api/teacher/wrong_training/config", methods=["PUT"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_wrong_training_config_update():
    data = request.get_json(silent=True) or {}
    with transactional(g.db):
        config = save_wrong_training_config(g.db, data)
        add_audit_log(
            "UPDATE_WRONG_TRAINING_CONFIG",
            target_type="app_settings",
            target_id=WRONG_TRAINING_CONFIG_KEY,
            detail={
                "daily_total_count": config["daily_total_count"],
                "reinforcement_count": config["reinforcement_count"],
                "regular_count": config["regular_count"],
                "mastery_streak": config["mastery_streak"],
            },
        )
    return v1_ok({"config": config, "priority_rule": wrong_training_priority_label()})


@bp.route("/api/teacher/students/<int:student_id>/wrongs", methods=["GET"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_student_wrongs(student_id):
    student, _class_row, error_resp = ensure_student_access(student_id)
    if error_resp:
        return error_resp
    limit, offset = parse_limit_offset()
    category = (request.args.get("category") or "").strip() or None
    active_only = parse_bool(request.args.get("active_only"), default=True)
    query = g.db.query(WrongQuestion, Question).join(Question, WrongQuestion.question_id == Question.id).filter(WrongQuestion.student_profile_id == student.id)
    if category:
        query = query.filter(Question.category == category)
    if active_only:
        query = query.filter(WrongQuestion.is_active == 1)
    query = query.order_by(WrongQuestion.wrong_count.desc(), WrongQuestion.last_wrong_at.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    items = []
    for wrong, question in rows:
        items.append(
            {
                "question_id": question.id,
                "stem": question.stem,
                "category": question.category,
                "wrong_count": wrong.wrong_count,
                "correct_streak": int(wrong.correct_streak or 0),
                "is_active": bool(wrong.is_active),
                "last_wrong_at": wrong.last_wrong_at,
                "last_correct_at": wrong.last_correct_at,
                "last_seen_at": getattr(wrong, "last_seen_at", None),
                "avg_cost_ms": parse_int(getattr(wrong, "avg_cost_ms", None), None),
                "correct": question.answer,
                "analysis": question.analysis,
            }
        )
    return v1_ok({"student_id": student.id, "items": items, "total": total, "limit": limit, "offset": offset})


@bp.route("/api/teacher/classes/<int:class_id>/wrongs_summary", methods=["GET"])
@login_required(api=True)
@role_required(["teacher", "assistant"])
def teacher_class_wrongs_summary(class_id):
    class_row, error_resp = ensure_class_access(class_id)
    if error_resp:
        return error_resp
    limit, offset = parse_limit_offset()
    exam_id = (request.args.get("exam_id") or "").strip() or None
    student_ids = [
        row[0]
        for row in g.db.query(StudentProfile.id)
        .filter(StudentProfile.class_id == class_row.id, StudentProfile.status == "active")
        .all()
    ]
    if not student_ids:
        return v1_ok({"class_id": class_row.id, "exam_id": exam_id, "items": [], "total": 0, "limit": limit, "offset": offset})
    summary_rows = []
    if exam_id:
        exam = g.db.query(Exam).filter(Exam.id == exam_id, Exam.class_id == class_row.id).first()
        if not exam:
            return v1_error("not_found", status=404, reason="exam not found in class")
        summary_rows = (
            g.db.query(
                Answer.question_id,
                func.count(Answer.id).label("wrong_total"),
                func.count(func.distinct(Attempt.student_profile_id)).label("wrong_students"),
            )
            .join(Attempt, Answer.attempt_id == Attempt.id)
            .filter(
                Attempt.exam_id == exam_id,
                Attempt.student_profile_id.in_(student_ids),
                Answer.is_correct == 0,
            )
            .group_by(Answer.question_id)
            .all()
        )
    else:
        summary_rows = (
            g.db.query(
                WrongQuestion.question_id,
                func.sum(WrongQuestion.wrong_count).label("wrong_total"),
                func.count(func.distinct(WrongQuestion.student_profile_id)).label("wrong_students"),
            )
            .filter(WrongQuestion.student_profile_id.in_(student_ids))
            .group_by(WrongQuestion.question_id)
            .all()
        )
    qids = [row[0] for row in summary_rows]
    questions = g.db.query(Question).filter(Question.id.in_(qids)).all() if qids else []
    qmap = {q.id: q for q in questions}
    merged = []
    for row in summary_rows:
        qid = row[0]
        question = qmap.get(qid)
        merged.append(
            {
                "question_id": qid,
                "stem": question.stem if question else None,
                "category": question.category if question else None,
                "wrong_students": int(row[2] or 0),
                "wrong_total": int(row[1] or 0),
            }
        )
    merged.sort(key=lambda x: (-x["wrong_total"], -x["wrong_students"], x["question_id"]))
    total = len(merged)
    items = merged[offset : offset + limit]
    return v1_ok({"class_id": class_row.id, "exam_id": exam_id, "items": items, "total": total, "limit": limit, "offset": offset})


@bp.route("/api/teacher/students/<int:student_id>/wrongs/practice", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_student_wrongs_practice(student_id):
    student, class_row, error_resp = ensure_student_access(student_id)
    if error_resp:
        return error_resp
    if not student.class_id or not class_row:
        return v1_error("forbidden", status=403, reason="student is not assigned to class")
    data = request.get_json(silent=True) or {}
    config = load_wrong_training_config(g.db)
    total_count = parse_int(data.get("count"), config["daily_total_count"])
    reinforcement_count = parse_int(data.get("reinforcement_count"), config["reinforcement_count"])
    mastery_streak = parse_int(data.get("mastery_streak"), config["mastery_streak"])
    runtime_config = _normalize_wrong_training_config_payload(
        {
            "daily_total_count": total_count,
            "reinforcement_count": reinforcement_count,
            "mastery_streak": mastery_streak,
        }
    )
    total_count = runtime_config["daily_total_count"]
    reinforcement_count = runtime_config["reinforcement_count"]
    regular_count = runtime_config["regular_count"]
    title = (data.get("title") or "").strip()
    category = (data.get("category") or "").strip() or None
    strategy = (data.get("strategy") or "recent_wrong_first").strip() or "recent_wrong_first"
    wrong_training_enabled = int(getattr(student, "wrong_training_enabled", 0) or 0) == 1

    wrong_rows_query = (
        g.db.query(WrongQuestion, Question)
        .join(Question, WrongQuestion.question_id == Question.id)
        .filter(WrongQuestion.student_profile_id == student.id, WrongQuestion.is_active == 1)
    )
    if category:
        wrong_rows_query = wrong_rows_query.filter(Question.category == category)
    wrong_rows = sort_wrong_training_candidates(wrong_rows_query.all())
    active_wrong_ids = [question.id for _wrong, question in wrong_rows]

    selected_wrong_pairs = wrong_rows[:reinforcement_count] if wrong_training_enabled else []
    selected_wrong_ids = [question.id for _wrong, question in selected_wrong_pairs]
    selected_ids = list(selected_wrong_ids)
    selected_set = set(selected_ids)

    all_question_query = g.db.query(Question.id)
    if category:
        all_question_query = all_question_query.filter(Question.category == category)
    all_question_ids = [row[0] for row in all_question_query.all()]
    preferred_regular_ids = [qid for qid in all_question_ids if qid not in selected_set and qid not in set(active_wrong_ids)]
    fallback_regular_ids = [qid for qid in all_question_ids if qid not in selected_set and qid not in preferred_regular_ids]

    fallback_needed = max(0, reinforcement_count - len(selected_wrong_ids))
    regular_target = regular_count + fallback_needed
    regular_selected = _pick_random_question_ids(preferred_regular_ids, regular_target)
    if len(regular_selected) < regular_target:
        more_regular = _pick_random_question_ids(
            [qid for qid in fallback_regular_ids if qid not in set(regular_selected)],
            regular_target - len(regular_selected),
        )
        regular_selected.extend(more_regular)
    selected_ids.extend([qid for qid in regular_selected if qid not in selected_set])
    selected_set = set(selected_ids)

    if len(selected_ids) < total_count:
        fill_ids = _pick_random_question_ids([qid for qid in all_question_ids if qid not in selected_set], total_count - len(selected_ids))
        selected_ids.extend(fill_ids)
        selected_set = set(selected_ids)

    if len(selected_ids) < total_count:
        return v1_error(
            "conflict",
            status=409,
            reason="insufficient questions to satisfy total count",
            data={"required_total_count": total_count, "selected_count": len(selected_ids)},
        )

    selected_ids = selected_ids[:total_count]
    practice_exam_id = "p_" + uuid.uuid4().hex[:8]
    now_str = utc_now_iso()
    if not title:
        title = f"practice-{student.name or student.client_username}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

    with transactional(g.db):
        exam_row = Exam(
            id=practice_exam_id,
            title=title,
            class_id=student.class_id,
            created_by=getattr(g, "current_user", None),
            question_count=len(selected_ids),
            category=category,
            start_at=now_str,
            end_at=None,
            exam_type="practice",
            target_student_profile_id=student.id,
            status="published",
            created_at=now_str,
        )
        g.db.add(exam_row)
        for qid in selected_ids:
            g.db.add(ExamQuestion(exam_id=practice_exam_id, question_id=qid))
        for wrong_row, _question in selected_wrong_pairs:
            wrong_row.last_seen_at = now_str
        add_audit_log(
            "CREATE_WRONG_TRAINING_PRACTICE",
            target_type="exams",
            target_id=practice_exam_id,
            detail={
                "student_id": student.id,
                "wrong_training_enabled": wrong_training_enabled,
                "daily_total_count": total_count,
                "regular_count": regular_count,
                "reinforcement_count": reinforcement_count,
                "selected_wrong_count": len(selected_wrong_ids),
                "selected_regular_count": len(selected_ids) - len(selected_wrong_ids),
                "fallback_count": max(0, reinforcement_count - len(selected_wrong_ids)),
                "mastery_streak": runtime_config["mastery_streak"],
                "strategy": strategy,
            },
        )

    return v1_ok(
        {
            "practice_exam_id": practice_exam_id,
            "type": "practice",
            "student_id": student.id,
            "wrong_training_enabled": wrong_training_enabled,
            "daily_total_count": total_count,
            "regular_count": regular_count,
            "reinforcement_count": reinforcement_count,
            "selected_wrong_count": len(selected_wrong_ids),
            "selected_regular_count": len(selected_ids) - len(selected_wrong_ids),
            "fallback_count": max(0, reinforcement_count - len(selected_wrong_ids)),
            "selected_count": len(selected_ids),
            "strategy": strategy,
            "priority_rule": wrong_training_priority_label(),
            "mastery_streak": runtime_config["mastery_streak"],
            "created_at": now_str,
        }
    )


@bp.route("/api/teacher/practices", methods=["GET"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_list_practices():
    limit, offset = parse_limit_offset()
    student_id = parse_int(request.args.get("student_id"), None)
    status = (request.args.get("status") or "").strip() or None
    query = g.db.query(Exam).filter(Exam.exam_type == "practice")
    query = query.filter(Exam.created_by == getattr(g, "current_user", None))
    if student_id is not None:
        student, _class_row, error_resp = ensure_student_access(student_id)
        if error_resp:
            return error_resp
        query = query.filter(Exam.target_student_profile_id == student.id)
    if status:
        query = query.filter(Exam.status == status)
    query = query.order_by(Exam.created_at.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()

    student_ids = [row.target_student_profile_id for row in rows if row.target_student_profile_id is not None]
    students = g.db.query(StudentProfile).filter(StudentProfile.id.in_(student_ids)).all() if student_ids else []
    student_map = {x.id: x for x in students}

    items = []
    for row in rows:
        latest_attempt = None
        if row.target_student_profile_id is not None:
            latest = (
                g.db.query(Attempt)
                .filter(
                    Attempt.exam_id == row.id,
                    Attempt.student_profile_id == row.target_student_profile_id,
                    Attempt.submitted_at.isnot(None),
                )
                .order_by(Attempt.submitted_at.desc())
                .first()
            )
            if latest:
                total_q = int(latest.total or 0)
                score_val = int(latest.score or 0)
                latest_attempt = {
                    "attempt_id": latest.id,
                    "score": score_val,
                    "total": total_q,
                    "accuracy": normalized_accuracy(score_val, total_q),
                    "submitted_at": latest.submitted_at,
                }
        student = student_map.get(row.target_student_profile_id) if row.target_student_profile_id else None
        items.append(
            {
                "practice_exam_id": row.id,
                "title": row.title,
                "student_id": row.target_student_profile_id,
                "student_name": student.name if student else None,
                "selected_count": int(row.question_count or 0),
                "status": row.status,
                "created_at": row.created_at,
                "latest_attempt": latest_attempt,
            }
        )
    return v1_ok({"items": items, "total": total, "limit": limit, "offset": offset})


@bp.route("/api/teacher/practices/<practice_exam_id>/archive", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_archive_practice(practice_exam_id):
    practice = g.db.query(Exam).filter(Exam.id == practice_exam_id, Exam.exam_type == "practice").first()
    if not practice:
        return v1_error("not_found", status=404, reason="practice not found")
    if practice.created_by != getattr(g, "current_user", None):
        return v1_error("forbidden", status=403, reason="not owner")
    with transactional(g.db):
        practice.status = "archived"
    return v1_ok({"practice_exam_id": practice.id, "status": practice.status})


@bp.route("/api/teacher/students/<int:student_id>/practice_effects", methods=["GET"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_student_practice_effects(student_id):
    student, _class_row, error_resp = ensure_student_access(student_id)
    if error_resp:
        return error_resp
    limit, offset = parse_limit_offset()
    query = (
        g.db.query(Exam)
        .filter(
            Exam.exam_type == "practice",
            Exam.target_student_profile_id == student.id,
            Exam.created_by == getattr(g, "current_user", None),
        )
        .order_by(Exam.created_at.desc())
    )
    total = query.count()
    practices = query.offset(offset).limit(limit).all()
    items = []
    for practice in practices:
        links = g.db.query(ExamQuestion).filter(ExamQuestion.exam_id == practice.id).all()
        qids = [x.question_id for x in links]
        question_count = len(qids)

        known_questions = 0
        unknown_questions = question_count
        baseline_correct = 0
        baseline_total = 0
        if qids:
            baseline_rows = (
                g.db.query(Answer.question_id, Answer.is_correct, Attempt.submitted_at)
                .join(Attempt, Answer.attempt_id == Attempt.id)
                .filter(
                    Attempt.student_profile_id == student.id,
                    Attempt.submitted_at.isnot(None),
                    Attempt.submitted_at < practice.created_at,
                    Answer.question_id.in_(qids),
                )
                .order_by(Attempt.submitted_at.desc())
                .all()
            )
            latest_per_question: Dict[str, int] = {}
            for qid, is_correct, _submitted_at in baseline_rows:
                if qid in latest_per_question:
                    continue
                latest_per_question[qid] = int(is_correct or 0)
            known_questions = len(latest_per_question)
            unknown_questions = max(0, question_count - known_questions)
            baseline_correct = sum(latest_per_question.values())
            baseline_total = known_questions
        baseline_accuracy = normalized_accuracy(baseline_correct, baseline_total)

        latest_attempt = (
            g.db.query(Attempt)
            .filter(
                Attempt.exam_id == practice.id,
                Attempt.student_profile_id == student.id,
                Attempt.submitted_at.isnot(None),
            )
            .order_by(Attempt.submitted_at.desc())
            .first()
        )
        result_payload = None
        delta_accuracy = None
        if latest_attempt:
            result_correct = int(latest_attempt.score or 0)
            result_total = int(latest_attempt.total or 0)
            result_accuracy = normalized_accuracy(result_correct, result_total)
            result_payload = {
                "attempt_id": latest_attempt.id,
                "correct": result_correct,
                "total": result_total,
                "accuracy": result_accuracy,
                "submitted_at": latest_attempt.submitted_at,
            }
            delta_accuracy = round(result_accuracy - baseline_accuracy, 4)

        items.append(
            {
                "practice_exam_id": practice.id,
                "title": practice.title,
                "created_at": practice.created_at,
                "question_count": question_count,
                "baseline": {
                    "known_questions": known_questions,
                    "unknown_questions": unknown_questions,
                    "correct": baseline_correct,
                    "total": baseline_total,
                    "accuracy": baseline_accuracy,
                },
                "result": result_payload,
                "delta_accuracy": delta_accuracy,
            }
        )
    return v1_ok({"student_id": student.id, "items": items, "total": total, "limit": limit, "offset": offset})


@bp.route("/api/teacher/exports/scores", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_export_scores():
    data = request.get_json(silent=True) or {}
    class_id = parse_int(data.get("class_id"), None)
    if class_id is None:
        return v1_error("invalid_params", status=400, reason="class_id is required")
    class_row, error_resp = ensure_class_access(class_id)
    if error_resp:
        return error_resp
    exam_id = (data.get("exam_id") or "").strip() or None
    from_dt = parse_iso_datetime(data.get("from"))
    to_dt = parse_iso_datetime(data.get("to"))
    if data.get("from") and not from_dt:
        return v1_error("invalid_params", status=400, reason="invalid from datetime")
    if data.get("to") and not to_dt:
        return v1_error("invalid_params", status=400, reason="invalid to datetime")
    query = (
        g.db.query(Attempt, Exam, StudentProfile)
        .join(Exam, Attempt.exam_id == Exam.id)
        .join(StudentProfile, Attempt.student_profile_id == StudentProfile.id)
        .filter(Attempt.submitted_at.isnot(None), Exam.class_id == class_row.id)
    )
    exam_title_part = "all"
    if exam_id:
        exam = g.db.query(Exam).filter(Exam.id == exam_id, Exam.class_id == class_row.id).first()
        if not exam:
            return v1_error("not_found", status=404, reason="exam not found in class")
        query = query.filter(Exam.id == exam.id)
        exam_title_part = sanitize_filename_part(exam.title, "exam")
    else:
        if from_dt:
            query = query.filter(Attempt.submitted_at >= to_iso_z(from_dt))
        if to_dt:
            query = query.filter(Attempt.submitted_at <= to_iso_z(to_dt))
    rows = query.order_by(Attempt.submitted_at.desc()).all()
    excel_rows = []
    for attempt, exam, student in rows:
        score_val = int(attempt.score or 0)
        total_val = int(attempt.total or 0)
        excel_rows.append(
            [
                student.student_no,
                student.name,
                score_val,
                total_val,
                normalized_accuracy(score_val, total_val),
                attempt.duration_sec,
                attempt.submitted_at,
                exam.title,
            ]
        )
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    class_part = sanitize_filename_part(class_row.name, "class")
    filename = f"scores_{class_part}_{exam_title_part}_{datetime.utcnow().strftime('%Y%m%d')}.xlsx"
    write_xlsx_file(
        filename=filename,
        sheet_name="scores",
        headers=["学号", "姓名", "分数", "总分", "正确率", "用时(秒)", "提交时间", "考试名称"],
        rows=excel_rows,
    )
    return v1_ok(
        {
            "file_id": f"f_scores_{ts}",
            "filename": filename,
            "download_url": f"/exports/{filename}",
        }
    )


@bp.route("/api/teacher/exports/wrongs", methods=["POST"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_export_wrongs():
    data = request.get_json(silent=True) or {}
    class_id = parse_int(data.get("class_id"), None)
    student_id = parse_int(data.get("student_id"), None)
    exam_id = (data.get("exam_id") or "").strip() or None
    if student_id is None and class_id is None:
        return v1_error("invalid_params", status=400, reason="student_id or class_id is required")
    rows: List[List[object]] = []
    filename_hint = "wrongs"
    if student_id is not None:
        student, _class_row, error_resp = ensure_student_access(student_id)
        if error_resp:
            return error_resp
        wrong_rows = (
            g.db.query(WrongQuestion, Question)
            .join(Question, WrongQuestion.question_id == Question.id)
            .filter(WrongQuestion.student_profile_id == student.id)
            .order_by(WrongQuestion.wrong_count.desc())
            .all()
        )
        filename_hint = sanitize_filename_part(student.name or student.client_username, "student")
        for wrong, question in wrong_rows:
            rows.append(
                [
                    student.student_no,
                    student.name,
                    question.id,
                    question.category,
                    question.stem,
                    question.answer,
                    wrong.wrong_count,
                    wrong.last_wrong_at,
                    question.analysis,
                ]
            )
    else:
        class_row, error_resp = ensure_class_access(class_id)
        if error_resp:
            return error_resp
        filename_hint = sanitize_filename_part(class_row.name, "class")
        if exam_id:
            exam = g.db.query(Exam).filter(Exam.id == exam_id, Exam.class_id == class_row.id).first()
            if not exam:
                return v1_error("not_found", status=404, reason="exam not found in class")
            wrong_rows = (
                g.db.query(
                    StudentProfile.student_no,
                    StudentProfile.name,
                    Question.id,
                    Question.category,
                    Question.stem,
                    Question.answer,
                    func.count(Answer.id).label("wrong_count"),
                    func.max(Attempt.submitted_at).label("last_wrong_at"),
                    Question.analysis,
                )
                .join(Attempt, Attempt.student_profile_id == StudentProfile.id)
                .join(Answer, Answer.attempt_id == Attempt.id)
                .join(Question, Question.id == Answer.question_id)
                .filter(
                    StudentProfile.class_id == class_row.id,
                    StudentProfile.status == "active",
                    Attempt.exam_id == exam.id,
                    Answer.is_correct == 0,
                )
                .group_by(StudentProfile.id, Question.id)
                .order_by(func.count(Answer.id).desc())
                .all()
            )
            rows.extend([list(row) for row in wrong_rows])
        else:
            wrong_rows = (
                g.db.query(
                    StudentProfile.student_no,
                    StudentProfile.name,
                    Question.id,
                    Question.category,
                    Question.stem,
                    Question.answer,
                    WrongQuestion.wrong_count,
                    WrongQuestion.last_wrong_at,
                    Question.analysis,
                )
                .join(WrongQuestion, WrongQuestion.student_profile_id == StudentProfile.id)
                .join(Question, Question.id == WrongQuestion.question_id)
                .filter(StudentProfile.class_id == class_row.id, StudentProfile.status == "active")
                .order_by(WrongQuestion.wrong_count.desc())
                .all()
            )
            rows.extend([list(row) for row in wrong_rows])
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"wrongs_{filename_hint}_{datetime.utcnow().strftime('%Y%m%d')}.xlsx"
    write_xlsx_file(
        filename=filename,
        sheet_name="wrongs",
        headers=["学号", "姓名", "题号", "类别", "题干", "正确答案", "错误次数", "最近错误时间", "解析"],
        rows=rows,
    )
    return v1_ok(
        {
            "file_id": f"f_wrongs_{ts}",
            "filename": filename,
            "download_url": f"/exports/{filename}",
        }
    )


@bp.route("/api/teacher/students/<int:student_id>/analysis", methods=["GET"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_student_analysis(student_id):
    student, _class_row, error_resp = ensure_student_access(student_id)
    if error_resp:
        return error_resp
    range_raw = (request.args.get("range") or "30d").strip().lower()
    top_n = min(parse_int(request.args.get("top_n"), 5) or 5, 20)
    attempts_query = g.db.query(Attempt).filter(Attempt.student_profile_id == student.id, Attempt.submitted_at.isnot(None))
    attempt_ids: Optional[List[str]] = None
    if range_raw.endswith("d"):
        days = parse_int(range_raw[:-1], 30) or 30
        if days <= 0:
            return v1_error("invalid_params", status=400, reason="range days must be positive")
        min_dt = datetime.now(timezone.utc) - timedelta(days=days)
        attempts_query = attempts_query.filter(Attempt.submitted_at >= to_iso_z(min_dt))
    else:
        n = parse_int(range_raw, None)
        if n is None or n <= 0:
            return v1_error("invalid_params", status=400, reason="range must be Nd or positive integer")
        attempt_rows = attempts_query.order_by(Attempt.submitted_at.desc()).limit(n).all()
        attempt_ids = [row.id for row in attempt_rows]
        attempts_query = g.db.query(Attempt).filter(Attempt.id.in_(attempt_ids)) if attempt_ids else g.db.query(Attempt).filter(Attempt.id == "__none__")
    attempts = attempts_query.all()
    attempt_count = len(attempts)
    answer_query = g.db.query(Answer, Question).join(Question, Answer.question_id == Question.id).join(Attempt, Answer.attempt_id == Attempt.id).filter(Attempt.student_profile_id == student.id)
    if attempt_ids is not None:
        answer_query = answer_query.filter(Answer.attempt_id.in_(attempt_ids)) if attempt_ids else answer_query.filter(Answer.id == "__none__")
    elif range_raw.endswith("d"):
        days = parse_int(range_raw[:-1], 30) or 30
        min_dt = datetime.now(timezone.utc) - timedelta(days=days)
        answer_query = answer_query.filter(Attempt.submitted_at >= to_iso_z(min_dt))
    answer_rows = answer_query.all()
    total_questions = len(answer_rows)
    correct_questions = sum(1 for answer, _question in answer_rows if answer.is_correct)
    by_category: Dict[str, Dict[str, int]] = {}
    for answer, question in answer_rows:
        category = question.category or "uncategorized"
        stat = by_category.setdefault(category, {"total": 0, "correct": 0})
        stat["total"] += 1
        if answer.is_correct:
            stat["correct"] += 1
    by_category_payload = []
    for category, stat in by_category.items():
        by_category_payload.append(
            {
                "category": category,
                "total": stat["total"],
                "correct": stat["correct"],
                "accuracy": normalized_accuracy(stat["correct"], stat["total"]),
            }
        )
    by_category_payload.sort(key=lambda x: (x["accuracy"], -x["total"], x["category"]))
    wrong_active_count = (
        g.db.query(func.count(WrongQuestion.question_id))
        .filter(WrongQuestion.student_profile_id == student.id, WrongQuestion.is_active == 1)
        .scalar()
        or 0
    )
    wrong_active_rows = (
        g.db.query(WrongQuestion)
        .filter(WrongQuestion.student_profile_id == student.id, WrongQuestion.is_active == 1)
        .order_by(WrongQuestion.wrong_count.desc(), WrongQuestion.last_wrong_at.desc())
        .limit(top_n)
        .all()
    )
    wrong_active_top = [
        {
            "question_id": row.question_id,
            "wrong_count": int(row.wrong_count or 0),
            "correct_streak": int(row.correct_streak or 0),
            "is_active": bool(row.is_active),
        }
        for row in wrong_active_rows
    ]
    return v1_ok(
        {
            "student_id": student.id,
            "range": range_raw,
            "overall": {
                "attempts": attempt_count,
                "total_questions": total_questions,
                "correct_questions": correct_questions,
                "accuracy": normalized_accuracy(correct_questions, total_questions),
            },
            "by_category": by_category_payload,
            "wrong_active": {
                "count": int(wrong_active_count),
                "top": wrong_active_top,
            },
        }
    )


@bp.route("/api/teacher/classes/<int:class_id>/analysis", methods=["GET"])
@login_required(api=True)
@role_required(["assistant"])
def teacher_class_analysis(class_id):
    return v1_error("not_found", status=404, reason="class analysis is not enabled")
    class_row, error_resp = ensure_class_access(class_id)
    if error_resp:
        return error_resp
    exam_id = (request.args.get("exam_id") or "").strip() or None
    range_raw = (request.args.get("range") or "30d").strip().lower()
    student_ids = [row[0] for row in g.db.query(StudentProfile.id).filter(StudentProfile.class_id == class_row.id, StudentProfile.status == "active").all()]
    if not student_ids:
        return v1_ok(
            {
                "class_id": class_row.id,
                "exam_id": exam_id,
                "score_summary": {"submitted_count": 0, "avg_score": 0, "max_score": 0, "min_score": 0, "bands": []},
                "weak_categories": [],
            }
        )
    attempts_query = g.db.query(Attempt).filter(Attempt.student_profile_id.in_(student_ids), Attempt.submitted_at.isnot(None))
    if exam_id:
        exam = g.db.query(Exam).filter(Exam.id == exam_id, Exam.class_id == class_row.id).first()
        if not exam:
            return v1_error("not_found", status=404, reason="exam not found in class")
        attempts_query = attempts_query.filter(Attempt.exam_id == exam.id)
        band_total = max(1, int(exam.question_count or 1))
    else:
        band_total = 10
        if range_raw.endswith("d"):
            days = parse_int(range_raw[:-1], 30) or 30
            if days <= 0:
                return v1_error("invalid_params", status=400, reason="range days must be positive")
            min_dt = datetime.now(timezone.utc) - timedelta(days=days)
            attempts_query = attempts_query.filter(Attempt.submitted_at >= to_iso_z(min_dt))
        else:
            n = parse_int(range_raw, None)
            if n is None or n <= 0:
                return v1_error("invalid_params", status=400, reason="range must be Nd or positive integer")
            attempts_query = attempts_query.order_by(Attempt.submitted_at.desc()).limit(n)
    attempts = attempts_query.all()
    selected_attempt_ids = [row.id for row in attempts]
    scores = [int(row.score or 0) for row in attempts if row.score is not None]
    submitted_count = len(attempts)
    if band_total <= 0:
        band_total = max((int(row.total or 0) for row in attempts), default=10) or 10
    b1_max = max(0, int(band_total * 0.2))
    b2_max = max(b1_max + 1, int(band_total * 0.5))
    b3_max = max(b2_max + 1, int(band_total * 0.8))
    bands = [
        {"label": f"0-{b1_max}", "count": 0},
        {"label": f"{b1_max + 1}-{b2_max}", "count": 0},
        {"label": f"{b2_max + 1}-{b3_max}", "count": 0},
        {"label": f"{b3_max + 1}-{band_total}", "count": 0},
    ]
    for score in scores:
        if score <= b1_max:
            bands[0]["count"] += 1
        elif score <= b2_max:
            bands[1]["count"] += 1
        elif score <= b3_max:
            bands[2]["count"] += 1
        else:
            bands[3]["count"] += 1
    answer_query = (
        g.db.query(Answer, Question)
        .join(Attempt, Answer.attempt_id == Attempt.id)
        .join(Question, Answer.question_id == Question.id)
        .filter(Attempt.student_profile_id.in_(student_ids))
    )
    if exam_id:
        answer_query = answer_query.filter(Attempt.exam_id == exam_id)
    elif range_raw.endswith("d"):
        days = parse_int(range_raw[:-1], 30) or 30
        min_dt = datetime.now(timezone.utc) - timedelta(days=days)
        answer_query = answer_query.filter(Attempt.submitted_at >= to_iso_z(min_dt))
    else:
        if selected_attempt_ids:
            answer_query = answer_query.filter(Answer.attempt_id.in_(selected_attempt_ids))
        else:
            answer_query = answer_query.filter(Answer.id == "__none__")
    by_category: Dict[str, Dict[str, int]] = {}
    for answer, question in answer_query.all():
        category = question.category or "uncategorized"
        stat = by_category.setdefault(category, {"total": 0, "correct": 0})
        stat["total"] += 1
        if answer.is_correct:
            stat["correct"] += 1
    weak_categories = []
    for category, stat in by_category.items():
        weak_categories.append({"category": category, "accuracy": normalized_accuracy(stat["correct"], stat["total"])})
    weak_categories.sort(key=lambda x: x["accuracy"])
    weak_categories = weak_categories[:5]
    return v1_ok(
        {
            "class_id": class_row.id,
            "exam_id": exam_id,
            "score_summary": {
                "submitted_count": submitted_count,
                "avg_score": round(sum(scores) / len(scores), 2) if scores else 0,
                "max_score": max(scores) if scores else 0,
                "min_score": min(scores) if scores else 0,
                "bands": bands,
            },
            "weak_categories": weak_categories,
        }
    )


@bp.route("/api/exports/<path:filename>", methods=["GET"])
@bp.route("/exports/<path:filename>", methods=["GET"])
@login_required(api=True)
@role_required(["assistant"])
def download_export(filename):
    return send_from_directory(EXPORT_DIR, filename, as_attachment=True)
