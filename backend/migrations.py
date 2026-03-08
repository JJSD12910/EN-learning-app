import sqlite3

from .db import DB_FILE


LATEST_VERSION = 15


def _table_exists(cur, table_name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    )
    return cur.fetchone() is not None


def _column_exists(cur, table_name: str, column_name: str) -> bool:
    if not _table_exists(cur, table_name):
        return False
    cur.execute(f"PRAGMA table_info({table_name})")
    return any(row[1] == column_name for row in cur.fetchall())


def _table_columns(cur, table_name: str):
    if not _table_exists(cur, table_name):
        return set()
    cur.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cur.fetchall()}


def _col_expr(columns, name: str, default_sql: str = "NULL") -> str:
    return name if name in columns else default_sql


def _coalesce_expr(columns, name: str, default_sql: str, fallback_sql: str = None) -> str:
    source_sql = _col_expr(columns, name, default_sql)
    use_fallback = default_sql if fallback_sql is None else fallback_sql
    return f"COALESCE({source_sql}, {use_fallback})"


def _rebuild_table(cur, table_name: str, create_sql_template: str, target_columns, select_expressions):
    if not _table_exists(cur, table_name):
        cur.execute(create_sql_template.format(table=table_name))
        return

    tmp_table = f"__tmp_{table_name}_v9"
    cur.execute(f"DROP TABLE IF EXISTS {tmp_table}")
    cur.execute(create_sql_template.format(table=tmp_table))
    insert_columns = ", ".join(target_columns)
    select_columns = ", ".join(select_expressions)
    cur.execute(f"INSERT INTO {tmp_table} ({insert_columns}) SELECT {select_columns} FROM {table_name}")
    cur.execute(f"DROP TABLE {table_name}")
    cur.execute(f"ALTER TABLE {tmp_table} RENAME TO {table_name}")


def _migration_1_schema_version(cur):
    cur.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL PRIMARY KEY)")


def _migration_2_questions_category(cur):
    if _table_exists(cur, "questions") and not _column_exists(cur, "questions", "category"):
        cur.execute("ALTER TABLE questions ADD COLUMN category VARCHAR(100)")


def _migration_3_questions_analysis(cur):
    if _table_exists(cur, "questions") and not _column_exists(cur, "questions", "analysis"):
        cur.execute("ALTER TABLE questions ADD COLUMN analysis TEXT")


def _migration_4_org_tables(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS schools (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(200) NOT NULL UNIQUE
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS classes (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            grade VARCHAR(50) NOT NULL,
            name VARCHAR(100) NOT NULL,
            teacher_username VARCHAR(100) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            created_at VARCHAR(64) NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS student_profiles (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            client_username VARCHAR(100) NOT NULL UNIQUE,
            school_id INTEGER,
            grade VARCHAR(50),
            class_id INTEGER,
            student_no VARCHAR(100),
            name VARCHAR(100),
            gender VARCHAR(20),
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            created_at VARCHAR(64) NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_classes_teacher ON classes(teacher_username)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_classes_status ON classes(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_student_profiles_class ON student_profiles(class_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_student_profiles_school ON student_profiles(school_id)")


def _migration_5_exam_tables(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS exams (
            id VARCHAR(64) NOT NULL PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            class_id INTEGER NOT NULL,
            created_by VARCHAR(100) NOT NULL,
            question_count INTEGER NOT NULL,
            category VARCHAR(100),
            start_at VARCHAR(64),
            end_at VARCHAR(64),
            allow_multiple_attempts INTEGER NOT NULL DEFAULT 0,
            exam_type VARCHAR(20) NOT NULL DEFAULT 'exam',
            target_student_profile_id INTEGER,
            status VARCHAR(20) NOT NULL DEFAULT 'published',
            created_at VARCHAR(64) NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS exam_questions (
            exam_id VARCHAR(64) NOT NULL,
            question_id VARCHAR(100) NOT NULL,
            PRIMARY KEY (exam_id, question_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS attempts (
            id VARCHAR(64) NOT NULL PRIMARY KEY,
            exam_id VARCHAR(64) NOT NULL,
            student_profile_id INTEGER NOT NULL,
            started_at VARCHAR(64) NOT NULL,
            submitted_at VARCHAR(64),
            score INTEGER,
            total INTEGER,
            duration_sec INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS answers (
            id VARCHAR(64) NOT NULL PRIMARY KEY,
            attempt_id VARCHAR(64) NOT NULL,
            question_id VARCHAR(100) NOT NULL,
            your INTEGER,
            correct INTEGER NOT NULL,
            is_correct INTEGER NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS wrong_questions (
            student_profile_id INTEGER NOT NULL,
            question_id VARCHAR(100) NOT NULL,
            wrong_count INTEGER NOT NULL DEFAULT 0,
            correct_streak INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            last_wrong_at VARCHAR(64) NOT NULL,
            last_correct_at VARCHAR(64),
            PRIMARY KEY (student_profile_id, question_id)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exams_class ON exams(class_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attempts_exam ON attempts(exam_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attempts_student ON attempts(student_profile_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_answers_attempt ON answers(attempt_id)")


def _migration_6_exam_flags(cur):
    if _table_exists(cur, "exams") and not _column_exists(cur, "exams", "allow_multiple_attempts"):
        cur.execute("ALTER TABLE exams ADD COLUMN allow_multiple_attempts INTEGER NOT NULL DEFAULT 0")
    if _table_exists(cur, "exams") and not _column_exists(cur, "exams", "exam_type"):
        cur.execute("ALTER TABLE exams ADD COLUMN exam_type VARCHAR(20) NOT NULL DEFAULT 'exam'")


def _migration_7_wrong_questions_last_correct(cur):
    if _table_exists(cur, "wrong_questions") and not _column_exists(cur, "wrong_questions", "last_correct_at"):
        cur.execute("ALTER TABLE wrong_questions ADD COLUMN last_correct_at VARCHAR(64)")


def _migration_8_wrong_lifecycle_and_practice_target(cur):
    if _table_exists(cur, "wrong_questions") and not _column_exists(cur, "wrong_questions", "correct_streak"):
        cur.execute("ALTER TABLE wrong_questions ADD COLUMN correct_streak INTEGER NOT NULL DEFAULT 0")
    if _table_exists(cur, "wrong_questions") and not _column_exists(cur, "wrong_questions", "is_active"):
        cur.execute("ALTER TABLE wrong_questions ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if _table_exists(cur, "wrong_questions"):
        cur.execute("UPDATE wrong_questions SET correct_streak=0 WHERE correct_streak IS NULL")
        cur.execute("UPDATE wrong_questions SET is_active=1 WHERE is_active IS NULL")
    if _table_exists(cur, "exams") and not _column_exists(cur, "exams", "target_student_profile_id"):
        if not _column_exists(cur, "exams", "target_student_id"):
            cur.execute("ALTER TABLE exams ADD COLUMN target_student_profile_id INTEGER")
    wrong_student_col = None
    if _table_exists(cur, "wrong_questions"):
        if _column_exists(cur, "wrong_questions", "student_profile_id"):
            wrong_student_col = "student_profile_id"
        elif _column_exists(cur, "wrong_questions", "student_id"):
            wrong_student_col = "student_id"
    if wrong_student_col:
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_wrong_questions_active ON wrong_questions({wrong_student_col}, is_active)")
    exam_target_col = None
    if _table_exists(cur, "exams"):
        if _column_exists(cur, "exams", "target_student_profile_id"):
            exam_target_col = "target_student_profile_id"
        elif _column_exists(cur, "exams", "target_student_id"):
            exam_target_col = "target_student_id"
    if exam_target_col:
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_exams_target_student ON exams({exam_target_col})")


def _migration_9_normalize_target_schema(cur):
    users_cols = _table_columns(cur, "users")
    users_role_col = _col_expr(users_cols, "role", "'admin'")
    users_role_expr = f"CASE WHEN {users_role_col} IN ('admin','assistant','teacher') THEN {users_role_col} ELSE 'admin' END"
    users_is_active_col = _col_expr(users_cols, "is_active", "1")
    users_is_active_expr = f"CASE WHEN {users_is_active_col} IS NULL THEN 1 ELSE {users_is_active_col} END"
    _rebuild_table(
        cur,
        "users",
        """
        CREATE TABLE {table} (
            id INTEGER NOT NULL PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','assistant','teacher')),
            display_name TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            updated_at TEXT,
            valid_from TEXT,
            valid_to TEXT,
            last_login_at TEXT
        )
        """,
        ["id", "username", "password", "role", "display_name", "is_active", "created_at", "updated_at", "valid_from", "valid_to", "last_login_at"],
        [
            _coalesce_expr(users_cols, "id", "rowid"),
            _coalesce_expr(users_cols, "username", "''"),
            _coalesce_expr(users_cols, "password", "''"),
            users_role_expr,
            _col_expr(users_cols, "display_name", "NULL"),
            users_is_active_expr,
            _col_expr(users_cols, "created_at", "NULL"),
            _col_expr(users_cols, "updated_at", "NULL"),
            _col_expr(users_cols, "valid_from", "NULL"),
            _col_expr(users_cols, "valid_to", "NULL"),
            _col_expr(users_cols, "last_login_at", "NULL"),
        ],
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active)")

    client_users_cols = _table_columns(cur, "client_users")
    client_users_is_active_col = _col_expr(client_users_cols, "is_active", "1")
    client_users_is_active_expr = f"CASE WHEN {client_users_is_active_col} IS NULL THEN 1 ELSE {client_users_is_active_col} END"
    _rebuild_table(
        cur,
        "client_users",
        """
        CREATE TABLE {table} (
            id INTEGER NOT NULL PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            updated_at TEXT,
            last_login_at TEXT
        )
        """,
        ["id", "username", "password", "is_active", "created_at", "updated_at", "last_login_at"],
        [
            _coalesce_expr(client_users_cols, "id", "rowid"),
            _coalesce_expr(client_users_cols, "username", "''"),
            _coalesce_expr(client_users_cols, "password", "''"),
            client_users_is_active_expr,
            _col_expr(client_users_cols, "created_at", "NULL"),
            _col_expr(client_users_cols, "updated_at", "NULL"),
            _col_expr(client_users_cols, "last_login_at", "NULL"),
        ],
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_client_users_is_active ON client_users(is_active)")

    sessions_cols = _table_columns(cur, "sessions")
    _rebuild_table(
        cur,
        "sessions",
        """
        CREATE TABLE {table} (
            token TEXT NOT NULL PRIMARY KEY,
            user TEXT NOT NULL,
            ts REAL NOT NULL
        )
        """,
        ["token", "user", "ts"],
        [
            _coalesce_expr(sessions_cols, "token", "''"),
            _coalesce_expr(sessions_cols, "user", "''"),
            _coalesce_expr(sessions_cols, "ts", "0"),
        ],
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_ts ON sessions(ts)")

    schools_cols = _table_columns(cur, "schools")
    _rebuild_table(
        cur,
        "schools",
        """
        CREATE TABLE {table} (
            id INTEGER NOT NULL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT
        )
        """,
        ["id", "name", "created_at"],
        [
            _coalesce_expr(schools_cols, "id", "rowid"),
            _coalesce_expr(schools_cols, "name", "''"),
            _col_expr(schools_cols, "created_at", "NULL"),
        ],
    )

    classes_cols = _table_columns(cur, "classes")
    classes_status_col = _col_expr(classes_cols, "status", "'active'")
    classes_status_expr = f"CASE WHEN {classes_status_col} IN ('active','dismissed') THEN {classes_status_col} ELSE 'active' END"
    _rebuild_table(
        cur,
        "classes",
        """
        CREATE TABLE {table} (
            id INTEGER NOT NULL PRIMARY KEY,
            school_id INTEGER NOT NULL,
            grade TEXT NOT NULL,
            name TEXT NOT NULL,
            teacher_username TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','dismissed')),
            created_at TEXT,
            UNIQUE (school_id, grade, name)
        )
        """,
        ["id", "school_id", "grade", "name", "teacher_username", "status", "created_at"],
        [
            _coalesce_expr(classes_cols, "id", "rowid"),
            _coalesce_expr(classes_cols, "school_id", "0"),
            _coalesce_expr(classes_cols, "grade", "''"),
            _coalesce_expr(classes_cols, "name", "''"),
            _col_expr(classes_cols, "teacher_username", "NULL"),
            classes_status_expr,
            _col_expr(classes_cols, "created_at", "NULL"),
        ],
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_classes_school_grade ON classes(school_id, grade)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_classes_teacher_username ON classes(teacher_username)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_classes_status ON classes(status)")

    student_cols = _table_columns(cur, "student_profiles")
    student_gender_col = _col_expr(student_cols, "gender", "NULL")
    student_gender_expr = (
        f"CASE WHEN {student_gender_col} IN ('M','F','U') THEN {student_gender_col} "
        "WHEN "
        f"{student_gender_col} IS NULL THEN NULL ELSE 'U' END"
    )
    student_status_col = _col_expr(student_cols, "status", "'active'")
    student_status_expr = f"CASE WHEN {student_status_col} IN ('active','inactive') THEN {student_status_col} ELSE 'active' END"
    _rebuild_table(
        cur,
        "student_profiles",
        """
        CREATE TABLE {table} (
            id INTEGER NOT NULL PRIMARY KEY,
            client_username TEXT NOT NULL UNIQUE,
            school_id INTEGER NOT NULL,
            grade TEXT NOT NULL,
            class_id INTEGER,
            student_no TEXT,
            name TEXT NOT NULL,
            gender TEXT CHECK(gender IN ('M','F','U') OR gender IS NULL),
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','inactive')),
            created_at TEXT,
            updated_at TEXT
        )
        """,
        ["id", "client_username", "school_id", "grade", "class_id", "student_no", "name", "gender", "status", "created_at", "updated_at"],
        [
            _coalesce_expr(student_cols, "id", "rowid"),
            _coalesce_expr(student_cols, "client_username", "''"),
            _coalesce_expr(student_cols, "school_id", "0"),
            _coalesce_expr(student_cols, "grade", "''"),
            _col_expr(student_cols, "class_id", "NULL"),
            _col_expr(student_cols, "student_no", "NULL"),
            _coalesce_expr(student_cols, "name", "NULL", _coalesce_expr(student_cols, "client_username", "''")),
            student_gender_expr,
            student_status_expr,
            _col_expr(student_cols, "created_at", "NULL"),
            _col_expr(student_cols, "updated_at", "NULL"),
        ],
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_student_profiles_class_id ON student_profiles(class_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_student_profiles_school_grade ON student_profiles(school_id, grade)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_student_profiles_status ON student_profiles(status)")

    questions_cols = _table_columns(cur, "questions")
    _rebuild_table(
        cur,
        "questions",
        """
        CREATE TABLE {table} (
            id TEXT NOT NULL PRIMARY KEY,
            stem TEXT NOT NULL,
            options TEXT NOT NULL,
            answer INTEGER NOT NULL,
            category TEXT,
            analysis TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """,
        ["id", "stem", "options", "answer", "category", "analysis", "created_at", "updated_at"],
        [
            _coalesce_expr(questions_cols, "id", "''"),
            _coalesce_expr(questions_cols, "stem", "''"),
            _coalesce_expr(questions_cols, "options", "'[]'"),
            _coalesce_expr(questions_cols, "answer", "0"),
            _col_expr(questions_cols, "category", "NULL"),
            _col_expr(questions_cols, "analysis", "NULL"),
            _col_expr(questions_cols, "created_at", "NULL"),
            _col_expr(questions_cols, "updated_at", "NULL"),
        ],
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_questions_category ON questions(category)")

    exams_cols = _table_columns(cur, "exams")
    exams_type_col = "exam_type" if "exam_type" in exams_cols else ("type" if "type" in exams_cols else None)
    exams_type_expr = f"CASE WHEN {exams_type_col} IN ('exam','practice') THEN {exams_type_col} ELSE 'exam' END" if exams_type_col else "'exam'"
    exams_status_col = _col_expr(exams_cols, "status", "'published'")
    exams_status_expr = f"CASE WHEN {exams_status_col} IN ('published','archived') THEN {exams_status_col} ELSE 'published' END"
    exams_target_col = "target_student_profile_id" if "target_student_profile_id" in exams_cols else (
        "target_student_id" if "target_student_id" in exams_cols else None
    )
    exams_target_expr = exams_target_col if exams_target_col else "NULL"
    _rebuild_table(
        cur,
        "exams",
        """
        CREATE TABLE {table} (
            id TEXT NOT NULL PRIMARY KEY,
            type TEXT NOT NULL CHECK(type IN ('exam','practice')),
            title TEXT NOT NULL,
            class_id INTEGER,
            target_student_id INTEGER,
            created_by TEXT NOT NULL,
            question_count INTEGER NOT NULL,
            category TEXT,
            start_at TEXT,
            end_at TEXT,
            status TEXT NOT NULL DEFAULT 'published' CHECK(status IN ('published','archived')),
            created_at TEXT
        )
        """,
        [
            "id",
            "type",
            "title",
            "class_id",
            "target_student_id",
            "created_by",
            "question_count",
            "category",
            "start_at",
            "end_at",
            "status",
            "created_at",
        ],
        [
            _coalesce_expr(exams_cols, "id", "''"),
            exams_type_expr,
            _coalesce_expr(exams_cols, "title", "''"),
            _col_expr(exams_cols, "class_id", "NULL"),
            exams_target_expr,
            _coalesce_expr(exams_cols, "created_by", "''"),
            _coalesce_expr(exams_cols, "question_count", "0"),
            _col_expr(exams_cols, "category", "NULL"),
            _col_expr(exams_cols, "start_at", "NULL"),
            _col_expr(exams_cols, "end_at", "NULL"),
            exams_status_expr,
            _col_expr(exams_cols, "created_at", "NULL"),
        ],
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exams_type ON exams(type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exams_class_id ON exams(class_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exams_target_student_id ON exams(target_student_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exams_created_by ON exams(created_by)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exams_status ON exams(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exams_created_at ON exams(created_at)")

    exam_questions_cols = _table_columns(cur, "exam_questions")
    _rebuild_table(
        cur,
        "exam_questions",
        """
        CREATE TABLE {table} (
            exam_id TEXT NOT NULL,
            question_id TEXT NOT NULL,
            PRIMARY KEY (exam_id, question_id)
        )
        """,
        ["exam_id", "question_id"],
        [
            _coalesce_expr(exam_questions_cols, "exam_id", "''"),
            _coalesce_expr(exam_questions_cols, "question_id", "''"),
        ],
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exam_questions_exam_id ON exam_questions(exam_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exam_questions_question_id ON exam_questions(question_id)")

    attempts_cols = _table_columns(cur, "attempts")
    _rebuild_table(
        cur,
        "attempts",
        """
        CREATE TABLE {table} (
            id TEXT NOT NULL PRIMARY KEY,
            exam_id TEXT NOT NULL,
            student_profile_id INTEGER NOT NULL,
            started_at TEXT,
            submitted_at TEXT,
            score INTEGER NOT NULL,
            total INTEGER NOT NULL,
            duration_sec INTEGER
        )
        """,
        ["id", "exam_id", "student_profile_id", "started_at", "submitted_at", "score", "total", "duration_sec"],
        [
            _coalesce_expr(attempts_cols, "id", "''"),
            _coalesce_expr(attempts_cols, "exam_id", "''"),
            _coalesce_expr(attempts_cols, "student_profile_id", "0"),
            _col_expr(attempts_cols, "started_at", "NULL"),
            _col_expr(attempts_cols, "submitted_at", "NULL"),
            _coalesce_expr(attempts_cols, "score", "0"),
            _coalesce_expr(attempts_cols, "total", "0"),
            _col_expr(attempts_cols, "duration_sec", "NULL"),
        ],
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attempts_exam_id ON attempts(exam_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attempts_student_profile_id ON attempts(student_profile_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attempts_submitted_at ON attempts(submitted_at)")

    answers_cols = _table_columns(cur, "answers")
    _rebuild_table(
        cur,
        "answers",
        """
        CREATE TABLE {table} (
            id TEXT NOT NULL PRIMARY KEY,
            attempt_id TEXT NOT NULL,
            question_id TEXT NOT NULL,
            your INTEGER NOT NULL,
            correct INTEGER NOT NULL,
            is_correct INTEGER NOT NULL
        )
        """,
        ["id", "attempt_id", "question_id", "your", "correct", "is_correct"],
        [
            _coalesce_expr(answers_cols, "id", "''"),
            _coalesce_expr(answers_cols, "attempt_id", "''"),
            _coalesce_expr(answers_cols, "question_id", "''"),
            _coalesce_expr(answers_cols, "your", "-1"),
            _coalesce_expr(answers_cols, "correct", "0"),
            _coalesce_expr(answers_cols, "is_correct", "0"),
        ],
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_answers_attempt_id ON answers(attempt_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_answers_question_id ON answers(question_id)")

    wrong_cols = _table_columns(cur, "wrong_questions")
    wrong_student_col = "student_profile_id" if "student_profile_id" in wrong_cols else ("student_id" if "student_id" in wrong_cols else None)
    wrong_student_expr = wrong_student_col if wrong_student_col else "0"
    _rebuild_table(
        cur,
        "wrong_questions",
        """
        CREATE TABLE {table} (
            student_id INTEGER NOT NULL,
            question_id TEXT NOT NULL,
            wrong_count INTEGER NOT NULL DEFAULT 0,
            correct_streak INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            last_wrong_at TEXT,
            last_correct_at TEXT,
            PRIMARY KEY (student_id, question_id)
        )
        """,
        ["student_id", "question_id", "wrong_count", "correct_streak", "is_active", "last_wrong_at", "last_correct_at"],
        [
            f"COALESCE({wrong_student_expr}, 0)",
            _coalesce_expr(wrong_cols, "question_id", "''"),
            _coalesce_expr(wrong_cols, "wrong_count", "0"),
            _coalesce_expr(wrong_cols, "correct_streak", "0"),
            _coalesce_expr(wrong_cols, "is_active", "1"),
            _col_expr(wrong_cols, "last_wrong_at", "NULL"),
            _col_expr(wrong_cols, "last_correct_at", "NULL"),
        ],
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_wrong_questions_student_active ON wrong_questions(student_id, is_active)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_wrong_questions_wrong_count ON wrong_questions(wrong_count)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_wrong_questions_last_wrong_at ON wrong_questions(last_wrong_at)")

    audit_cols = _table_columns(cur, "audit_logs")
    _rebuild_table(
        cur,
        "audit_logs",
        """
        CREATE TABLE {table} (
            id TEXT NOT NULL PRIMARY KEY,
            actor_username TEXT NOT NULL,
            actor_role TEXT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            detail_json TEXT,
            created_at TEXT NOT NULL
        )
        """,
        ["id", "actor_username", "actor_role", "action", "target_type", "target_id", "detail_json", "created_at"],
        [
            _coalesce_expr(audit_cols, "id", "''"),
            _coalesce_expr(audit_cols, "actor_username", "''"),
            _coalesce_expr(audit_cols, "actor_role", "''"),
            _coalesce_expr(audit_cols, "action", "''"),
            _col_expr(audit_cols, "target_type", "NULL"),
            _col_expr(audit_cols, "target_id", "NULL"),
            _col_expr(audit_cols, "detail_json", "NULL"),
            _coalesce_expr(audit_cols, "created_at", "''"),
        ],
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_actor_username ON audit_logs(actor_username)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action)")


def _migration_10_exam_lifecycle(cur):
    exams_cols = _table_columns(cur, "exams")
    exams_type_col = "type" if "type" in exams_cols else ("exam_type" if "exam_type" in exams_cols else None)
    exams_type_expr = f"CASE WHEN {exams_type_col} IN ('exam','practice') THEN {exams_type_col} ELSE 'exam' END" if exams_type_col else "'exam'"
    exams_status_col = _col_expr(exams_cols, "status", "'draft'")
    exams_status_expr = (
        f"CASE WHEN {exams_status_col} IN ('draft','published','active','ended','archived') "
        f"THEN {exams_status_col} ELSE 'draft' END"
    )
    exams_target_col = "target_student_id" if "target_student_id" in exams_cols else (
        "target_student_profile_id" if "target_student_profile_id" in exams_cols else None
    )
    exams_target_expr = exams_target_col if exams_target_col else "NULL"
    exams_allow_multiple_expr = _coalesce_expr(exams_cols, "allow_multiple_attempts", "0")

    _rebuild_table(
        cur,
        "exams",
        """
        CREATE TABLE {table} (
            id TEXT NOT NULL PRIMARY KEY,
            type TEXT NOT NULL CHECK(type IN ('exam','practice')),
            title TEXT NOT NULL,
            class_id INTEGER,
            target_student_id INTEGER,
            created_by TEXT NOT NULL,
            question_count INTEGER NOT NULL,
            category TEXT,
            start_at TEXT,
            end_at TEXT,
            allow_multiple_attempts INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','active','ended','archived')),
            created_at TEXT,
            updated_at TEXT
        )
        """,
        [
            "id",
            "type",
            "title",
            "class_id",
            "target_student_id",
            "created_by",
            "question_count",
            "category",
            "start_at",
            "end_at",
            "allow_multiple_attempts",
            "status",
            "created_at",
            "updated_at",
        ],
        [
            _coalesce_expr(exams_cols, "id", "''"),
            exams_type_expr,
            _coalesce_expr(exams_cols, "title", "''"),
            _col_expr(exams_cols, "class_id", "NULL"),
            exams_target_expr,
            _coalesce_expr(exams_cols, "created_by", "''"),
            _coalesce_expr(exams_cols, "question_count", "0"),
            _col_expr(exams_cols, "category", "NULL"),
            _col_expr(exams_cols, "start_at", "NULL"),
            _col_expr(exams_cols, "end_at", "NULL"),
            exams_allow_multiple_expr,
            exams_status_expr,
            _col_expr(exams_cols, "created_at", "NULL"),
            _col_expr(exams_cols, "updated_at", "NULL"),
        ],
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exams_type ON exams(type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exams_class_id ON exams(class_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exams_target_student_id ON exams(target_student_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exams_created_by ON exams(created_by)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exams_status ON exams(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exams_created_at ON exams(created_at)")


def _migration_11_student_wrong_training_switch(cur):
    if _table_exists(cur, "student_profiles") and not _column_exists(cur, "student_profiles", "wrong_training_enabled"):
        cur.execute("ALTER TABLE student_profiles ADD COLUMN wrong_training_enabled INTEGER NOT NULL DEFAULT 0")
    if _table_exists(cur, "student_profiles"):
        cur.execute("UPDATE student_profiles SET wrong_training_enabled=0 WHERE wrong_training_enabled IS NULL")


def _migration_12_roles_and_teacher_validity(cur):
    users_cols = _table_columns(cur, "users")
    users_role_col = _col_expr(users_cols, "role", "'admin'")
    users_role_expr = f"CASE WHEN {users_role_col} IN ('admin','assistant','teacher') THEN {users_role_col} ELSE 'admin' END"
    users_is_active_col = _col_expr(users_cols, "is_active", "1")
    users_is_active_expr = f"CASE WHEN {users_is_active_col} IS NULL THEN 1 ELSE {users_is_active_col} END"
    _rebuild_table(
        cur,
        "users",
        """
        CREATE TABLE {table} (
            id INTEGER NOT NULL PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','assistant','teacher')),
            display_name TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            updated_at TEXT,
            valid_from TEXT,
            valid_to TEXT,
            last_login_at TEXT
        )
        """,
        ["id", "username", "password", "role", "display_name", "is_active", "created_at", "updated_at", "valid_from", "valid_to", "last_login_at"],
        [
            _coalesce_expr(users_cols, "id", "rowid"),
            _coalesce_expr(users_cols, "username", "''"),
            _coalesce_expr(users_cols, "password", "''"),
            users_role_expr,
            _col_expr(users_cols, "display_name", "NULL"),
            users_is_active_expr,
            _col_expr(users_cols, "created_at", "NULL"),
            _col_expr(users_cols, "updated_at", "NULL"),
            _col_expr(users_cols, "valid_from", "NULL"),
            _col_expr(users_cols, "valid_to", "NULL"),
            _col_expr(users_cols, "last_login_at", "NULL"),
        ],
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active)")


def _migration_14_wrong_training_config(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT NOT NULL PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT
        )
        """
    )
    if _table_exists(cur, "wrong_questions") and not _column_exists(cur, "wrong_questions", "last_seen_at"):
        cur.execute("ALTER TABLE wrong_questions ADD COLUMN last_seen_at TEXT")
    if _table_exists(cur, "wrong_questions") and not _column_exists(cur, "wrong_questions", "avg_cost_ms"):
        cur.execute("ALTER TABLE wrong_questions ADD COLUMN avg_cost_ms INTEGER")

def _migration_13_attempt_progress(cur):
    if _table_exists(cur, "attempts") and not _column_exists(cur, "attempts", "progress_count"):
        cur.execute("ALTER TABLE attempts ADD COLUMN progress_count INTEGER NOT NULL DEFAULT 0")
    if _table_exists(cur, "attempts"):
        cur.execute("UPDATE attempts SET progress_count=0 WHERE progress_count IS NULL")


def _migration_15_attempt_answers(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS attempt_answers (
            id TEXT NOT NULL PRIMARY KEY,
            attempt_id TEXT NOT NULL,
            exam_id TEXT NOT NULL,
            question_id TEXT NOT NULL,
            choice INTEGER NOT NULL,
            progress_count INTEGER NOT NULL DEFAULT 0,
            duration_sec INTEGER,
            first_answered_at TEXT,
            last_answered_at TEXT,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(attempt_id, question_id)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attempt_answers_exam_id ON attempt_answers(exam_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attempt_answers_attempt_id ON attempt_answers(attempt_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attempt_answers_question_id ON attempt_answers(question_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attempt_answers_updated_at ON attempt_answers(updated_at)")
    if _table_exists(cur, "answers") and _table_exists(cur, "attempts"):
        cur.execute(
            """
            INSERT OR IGNORE INTO attempt_answers (
                id,
                attempt_id,
                exam_id,
                question_id,
                choice,
                progress_count,
                duration_sec,
                first_answered_at,
                last_answered_at,
                created_at,
                updated_at
            )
            SELECT
                answers.id,
                answers.attempt_id,
                attempts.exam_id,
                answers.question_id,
                COALESCE(answers.your, -1),
                COALESCE(attempts.progress_count, 0),
                attempts.duration_sec,
                COALESCE(attempts.submitted_at, attempts.started_at),
                COALESCE(attempts.submitted_at, attempts.started_at),
                COALESCE(attempts.started_at, attempts.submitted_at),
                COALESCE(attempts.submitted_at, attempts.started_at)
            FROM answers
            JOIN attempts ON attempts.id = answers.attempt_id
            """
        )


MIGRATIONS = [
    (1, _migration_1_schema_version),
    (2, _migration_2_questions_category),
    (3, _migration_3_questions_analysis),
    (4, _migration_4_org_tables),
    (5, _migration_5_exam_tables),
    (6, _migration_6_exam_flags),
    (7, _migration_7_wrong_questions_last_correct),
    (8, _migration_8_wrong_lifecycle_and_practice_target),
    (9, _migration_9_normalize_target_schema),
    (10, _migration_10_exam_lifecycle),
    (11, _migration_11_student_wrong_training_switch),
    (12, _migration_12_roles_and_teacher_validity),
    (13, _migration_13_attempt_progress),
    (14, _migration_14_wrong_training_config),
    (15, _migration_15_attempt_answers),
]


def run_migrations():
    con = sqlite3.connect(DB_FILE)
    try:
        cur = con.cursor()
        _migration_1_schema_version(cur)
        con.commit()

        cur.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
        row = cur.fetchone()
        current_version = int(row[0] or 0)

        for version, migration_fn in MIGRATIONS:
            if current_version >= version:
                continue
            migration_fn(cur)
            cur.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (version,))
            con.commit()
            current_version = version
    finally:
        con.close()
