from sqlalchemy import CheckConstraint, Column, Float, Index, Integer, String, Text, UniqueConstraint

from .db import Base


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version = Column(Integer, primary_key=True)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(Text, primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=True)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin','assistant','teacher')", name="ck_users_role"),
        Index("idx_users_role", "role"),
        Index("idx_users_is_active", "is_active"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(Text, unique=True, nullable=False)
    password = Column(Text, nullable=False)
    role = Column(String(20), nullable=False, default="admin", server_default="admin")
    display_name = Column(Text, nullable=True)
    is_active = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(Text, nullable=True)
    updated_at = Column(Text, nullable=True)
    valid_from = Column(Text, nullable=True)
    valid_to = Column(Text, nullable=True)
    last_login_at = Column(Text, nullable=True)


class ClientUser(Base):
    __tablename__ = "client_users"
    __table_args__ = (Index("idx_client_users_is_active", "is_active"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(Text, unique=True, nullable=False)
    password = Column(Text, nullable=False)
    is_active = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(Text, nullable=True)
    updated_at = Column(Text, nullable=True)
    last_login_at = Column(Text, nullable=True)


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (Index("idx_questions_category", "category"),)

    id = Column(Text, primary_key=True)
    stem = Column(Text, nullable=False)
    options = Column(Text, nullable=False)  # JSON string
    answer = Column(Integer, nullable=False)
    category = Column(Text, nullable=True)
    analysis = Column(Text, nullable=True)
    created_at = Column(Text, nullable=True)
    updated_at = Column(Text, nullable=True)


class Record(Base):
    __tablename__ = "records"
    id = Column(String(64), primary_key=True)
    timestamp = Column(String(64), nullable=False)
    client_ip = Column(String(64), nullable=True)
    user_id = Column(String(100), nullable=False)
    quiz_id = Column(String(100), nullable=False)
    score = Column(Integer, nullable=False)
    total = Column(Integer, nullable=False)
    wrong = Column(Text, nullable=False)  # JSON string


class SessionToken(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("idx_sessions_user", "user"), Index("idx_sessions_ts", "ts"))

    token = Column(Text, primary_key=True)
    user = Column(Text, nullable=False)
    ts = Column(Float, nullable=False)


class School(Base):
    __tablename__ = "schools"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(Text, unique=True, nullable=False)
    created_at = Column(Text, nullable=True)


class SchoolClass(Base):
    __tablename__ = "classes"
    __table_args__ = (
        CheckConstraint("status IN ('active','dismissed')", name="ck_classes_status"),
        UniqueConstraint("school_id", "grade", "name", name="uq_classes_school_grade_name"),
        Index("idx_classes_school_grade", "school_id", "grade"),
        Index("idx_classes_teacher_username", "teacher_username"),
        Index("idx_classes_status", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    school_id = Column(Integer, nullable=False)
    grade = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    teacher_username = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="active", server_default="active")
    created_at = Column(Text, nullable=True)


class StudentProfile(Base):
    __tablename__ = "student_profiles"
    __table_args__ = (
        CheckConstraint("gender IN ('M','F','U') OR gender IS NULL", name="ck_student_profiles_gender"),
        CheckConstraint("status IN ('active','inactive')", name="ck_student_profiles_status"),
        Index("idx_student_profiles_class_id", "class_id"),
        Index("idx_student_profiles_school_grade", "school_id", "grade"),
        Index("idx_student_profiles_status", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_username = Column(Text, unique=True, nullable=False)
    school_id = Column(Integer, nullable=False)
    grade = Column(Text, nullable=False)
    class_id = Column(Integer, nullable=True)
    student_no = Column(Text, nullable=True)
    name = Column(Text, nullable=False)
    gender = Column(String(20), nullable=True)
    status = Column(String(20), nullable=False, default="active", server_default="active")
    wrong_training_enabled = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(Text, nullable=True)
    updated_at = Column(Text, nullable=True)


class Exam(Base):
    __tablename__ = "exams"
    __table_args__ = (
        CheckConstraint("type IN ('exam','practice')", name="ck_exams_type"),
        CheckConstraint("status IN ('draft','published','active','ended','archived')", name="ck_exams_status"),
        Index("idx_exams_type", "type"),
        Index("idx_exams_class_id", "class_id"),
        Index("idx_exams_target_student_id", "target_student_id"),
        Index("idx_exams_created_by", "created_by"),
        Index("idx_exams_status", "status"),
        Index("idx_exams_created_at", "created_at"),
    )

    id = Column(Text, primary_key=True)
    exam_type = Column("type", String(20), nullable=False, default="exam", server_default="exam")
    title = Column(Text, nullable=False)
    class_id = Column(Integer, nullable=True)
    target_student_profile_id = Column("target_student_id", Integer, nullable=True)
    created_by = Column(Text, nullable=False)
    question_count = Column(Integer, nullable=False)
    category = Column(Text, nullable=True)
    start_at = Column(Text, nullable=True)
    end_at = Column(Text, nullable=True)
    allow_multiple_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    status = Column(String(20), nullable=False, default="draft", server_default="draft")
    created_at = Column(Text, nullable=True)
    updated_at = Column(Text, nullable=True)


class ExamQuestion(Base):
    __tablename__ = "exam_questions"
    __table_args__ = (Index("idx_exam_questions_exam_id", "exam_id"), Index("idx_exam_questions_question_id", "question_id"))

    exam_id = Column(Text, primary_key=True)
    question_id = Column(Text, primary_key=True)


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        Index("idx_attempts_exam_id", "exam_id"),
        Index("idx_attempts_student_profile_id", "student_profile_id"),
        Index("idx_attempts_submitted_at", "submitted_at"),
    )

    id = Column(Text, primary_key=True)
    exam_id = Column(Text, nullable=False)
    student_profile_id = Column(Integer, nullable=False)
    started_at = Column(Text, nullable=True)
    submitted_at = Column(Text, nullable=True)
    score = Column(Integer, nullable=False, default=0, server_default="0")
    total = Column(Integer, nullable=False, default=0, server_default="0")
    progress_count = Column(Integer, nullable=False, default=0, server_default="0")
    duration_sec = Column(Integer, nullable=True)


class Answer(Base):
    __tablename__ = "answers"
    __table_args__ = (Index("idx_answers_attempt_id", "attempt_id"), Index("idx_answers_question_id", "question_id"))

    id = Column(Text, primary_key=True)
    attempt_id = Column(Text, nullable=False)
    question_id = Column(Text, nullable=False)
    your = Column(Integer, nullable=False)
    correct = Column(Integer, nullable=False)
    is_correct = Column(Integer, nullable=False)


class WrongQuestion(Base):
    __tablename__ = "wrong_questions"
    __table_args__ = (
        Index("idx_wrong_questions_student_active", "student_id", "is_active"),
        Index("idx_wrong_questions_wrong_count", "wrong_count"),
        Index("idx_wrong_questions_last_wrong_at", "last_wrong_at"),
    )

    student_profile_id = Column("student_id", Integer, primary_key=True)
    question_id = Column(Text, primary_key=True)
    wrong_count = Column(Integer, nullable=False, default=0)
    correct_streak = Column(Integer, nullable=False, default=0)
    is_active = Column(Integer, nullable=False, default=1)
    last_wrong_at = Column(Text, nullable=True)
    last_correct_at = Column(Text, nullable=True)
    last_seen_at = Column(Text, nullable=True)
    avg_cost_ms = Column(Integer, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("idx_audit_logs_created_at", "created_at"),
        Index("idx_audit_logs_actor_username", "actor_username"),
        Index("idx_audit_logs_action", "action"),
    )

    id = Column(Text, primary_key=True)
    actor_username = Column(Text, nullable=False)
    actor_role = Column(Text, nullable=False)
    action = Column(Text, nullable=False)
    target_type = Column(Text, nullable=True)
    target_id = Column(Text, nullable=True)
    detail_json = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False)
