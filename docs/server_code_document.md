> Legacy archive note
>
> This document describes an older standalone `backend/quiz_server.py` implementation and legacy routes such as `/submit`, `/records.json`, and `/results.json`. It is kept only as a historical code snapshot and does not reflect the current Flask route behavior in `backend/routes.py`.

## backend/quiz_server.py
```python
"""
Quiz HTTP server that serves minimal APIs + simple test UIs:
- GET /questions      : downlink random questions to clients
- POST /submit        : receive answers, grade, persist record, return record_id
- GET /results?id=... : return a stored result (by record_id) or latest for the requesting IP
Plus lightweight pages for manual testing: /, /submit, /results.
"""

import json
import random
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST = "0.0.0.0"
PORT = 8000
ROOT_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT_DIR / "frontend"
DATA_DIR = ROOT_DIR / "data"
QUESTION_FILE = DATA_DIR / "questions.json"
RECORD_FILE = DATA_DIR / "exam_records.json"
CLIENT_USER_FILE = DATA_DIR / "client_users.json"
DEFAULT_QUESTION_COUNT = 10
DEFAULT_USERS = [{"username": "001", "password": "666"}]

def load_question_bank():
    try:
        loaded = json.loads(QUESTION_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(f"Question file not found: {QUESTION_FILE}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Question file is invalid JSON: {exc}")

    sanitized = []
    for item in loaded:
        if not {"id", "stem", "options", "answer"} <= item.keys():
            continue
        sanitized.append(item)
    if not sanitized:
        raise RuntimeError("Question bank is empty after validation")
    return sanitized

QUESTION_BANK = load_question_bank()
QUESTION_LOOKUP = {q["id"]: q for q in QUESTION_BANK}
ACTIVE_QUIZZES = {}

def ensure_record_file():
    if not RECORD_FILE.exists():
        RECORD_FILE.write_text("[]", encoding="utf-8")

def load_records():
    try:
        return json.loads(RECORD_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def ensure_user_file():
    if not CLIENT_USER_FILE.exists():
        CLIENT_USER_FILE.write_text(json.dumps(DEFAULT_USERS, ensure_ascii=False, indent=2), encoding="utf-8")

def load_users(path=CLIENT_USER_FILE):
    try:
        users = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    sanitized = []
    for item in users:
        if not isinstance(item, dict):
            continue
        name = item.get("username")
        pwd = item.get("password")
        if not name or not pwd:
            continue
        sanitized.append({"username": str(name), "password": str(pwd)})
    return sanitized

def validate_credentials(username, password):
    username = (username or "").strip()
    password = (password or "").strip()
    if not username or not password:
        return False, None
    users = load_users()
    for user in users:
        if user["username"] == username and user["password"] == password:
            return True, username
    return False, None

def pick_questions(count):
    count = DEFAULT_QUESTION_COUNT if count is None else max(1, int(count))
    count = min(count, len(QUESTION_BANK))
    return random.sample(QUESTION_BANK, count)

def grade_submission(answers):
    total = len(answers or [])
    correct = 0
    wrong = []
    for answer in answers or []:
        qid = answer.get("id")
        choice = answer.get("choice")
        question = QUESTION_LOOKUP.get(qid)
        if question is None:
            continue
        if choice == question["answer"]:
            correct += 1
        else:
            wrong.append({"id": qid, "correct": question["answer"], "your": choice})
    return {"score": correct, "total": total, "wrong": wrong}

def store_score_record(user_id, quiz_id, score, total, client_ip, wrong=None):
    record_id = uuid.uuid4().hex
    record = {
        "id": record_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "client_ip": client_ip,
        "user_id": user_id,
        "quiz_id": quiz_id,
        "score": score,
        "total": total,
        "wrong": wrong or [],
    }
    existing = load_records()
    existing.append(record)
    RECORD_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return record_id

def latest_record(record_id=None, client_ip=None):
    records = load_records()
    if record_id:
        for rec in reversed(records):
            if rec.get("id") == record_id:
                return rec
        return None
    if client_ip:
        for rec in reversed(records):
            if rec.get("client_ip") == client_ip:
                return rec
    return records[-1] if records else None

def _read_frontend(filename):
    path = FRONTEND_DIR / filename
    if not path.exists():
        return b"", False
    return path.read_bytes(), True

class QuizHandler(BaseHTTPRequestHandler):
    def _set_headers(self, code, length, content_type="application/json", extra_headers=None):
        self.wfile.write(f"HTTP/1.1 {code} OK\r\n".encode())
        self.wfile.write(f"Content-Type: {content_type}\r\n".encode())
        self.wfile.write(f"Content-Length: {length}\r\n".encode())
        self.wfile.write(b"Access-Control-Allow-Origin: *\r\n")
        self.wfile.write(b"Access-Control-Allow-Headers: Content-Type\r\n")
        self.wfile.write(b"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n")
        if extra_headers:
            for key, value in extra_headers:
                self.wfile.write(f"{key}: {value}\r\n".encode())
        self.wfile.write(b"\r\n")

    def do_OPTIONS(self):
        self._set_headers(200, 0)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            return self._write_json(200, {"status": "ok"})

        if path == "/questions":
            try:
                user_id = query.get("user_id", [None])[0]
                if not user_id:
                    self._write_json(400, {"error": "user_id required"})
                    return

                count_param = query.get("count", [None])[0]
                try:
                    count_int = min(len(QUESTION_BANK), max(1, int(count_param))) if count_param else DEFAULT_QUESTION_COUNT
                except (TypeError, ValueError):
                    count_int = DEFAULT_QUESTION_COUNT
                quiz_id = str(uuid.uuid4())
                ACTIVE_QUIZZES[quiz_id] = {"user_id": user_id, "ts": time.time(), "count": count_int}

                questions = pick_questions(count_int)

                self._write_json(
                    200,
                    {
                        "user_id": user_id,
                        "quiz_id": quiz_id,
                        "questions": questions,
                        "total": len(questions),
                        "bank_size": len(QUESTION_BANK),
                    },
                )
            except Exception as exc:
                self._write_json(400, {"error": str(exc)})
            return

        if path in ("/", "/index.html"):
            body, ok = _read_frontend("home.html")
            if not ok:
                return self._write_json(404, {"error": "homepage not found"})
            self._set_headers(200, len(body), "text/html; charset=utf-8")
            self.wfile.write(body)
        elif path == "/submit":
            body, ok = _read_frontend("submit.html")
            if not ok:
                return self._write_json(404, {"error": "submit page not found"})
            self._set_headers(200, len(body), "text/html; charset=utf-8")
            self.wfile.write(body)
        elif path == "/results":
            body, ok = _read_frontend("records.html")
            if not ok:
                return self._write_json(404, {"error": "results page not found"})
            self._set_headers(200, len(body), "text/html; charset=utf-8")
            self.wfile.write(body)
        elif path == "/records.json":
            limit_param = query.get("limit", [20])[0]
            try:
                limit_val = max(1, int(limit_param))
            except ValueError:
                limit_val = 20
            records = list(reversed(load_records()))[:limit_val]
            self._write_json(200, {"records": records, "total": len(records)})
        elif path == "/results.json":
            rid = query.get("id", [None])[0]
            client_ip = self.client_address[0]
            record = latest_record(rid, client_ip)
            if record:
                self._write_json(200, {"record": record})
            else:
                self._write_json(404, {"error": "no record found"})
        else:
            self._write_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/client/login":
            return self._handle_client_login()

        if parsed.path == "/submit":
            return self._handle_submit()

        return self._write_json(404, {"error": "not found"})

    def _handle_client_login(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length) if length > 0 else b"{}"

        try:
            data = json.loads(payload.decode())
        except json.JSONDecodeError:

            self._set_headers(400, 0)
            return

        username = data.get("username")
        password = data.get("password")

        ok, _ = validate_credentials(username, password)
        client_ip = self.client_address[0] if self.client_address else "unknown"

        if not ok:
            print(f"[client-login] fail user={username!r} ip={client_ip}")

            self._set_headers(401, 0)
            return

        print(f"[client-login] success user={username!r} ip={client_ip}")
        self._set_headers(200, 0)

    def _handle_submit(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length) if length > 0 else b"{}"

        try:
            data = json.loads(payload.decode())
        except json.JSONDecodeError:
            self._write_json(400, {"error": "invalid json"})
            return

        user_id = data.get("user_id")
        quiz_id = data.get("quiz_id")
        score = data.get("score")
        total = data.get("total", len(QUESTION_BANK))
        wrong = data.get("wrong")
        answers = data.get("answers")

        if not user_id or not quiz_id:
            self._write_json(400, {"error": "user_id and quiz_id are required"})
            return

        if not isinstance(score, int) and not isinstance(answers, list):
            self._write_json(400, {"error": "score (int) or answers array required"})
            return

        entry = ACTIVE_QUIZZES.get(quiz_id)
        if not entry:
            self._write_json(400, {"error": "quiz_id not found"})
            return

        if entry["user_id"] != user_id:
            self._write_json(403, {"error": "user_id mismatch"})
            return

        if time.time() - entry["ts"] > 300:
            del ACTIVE_QUIZZES[quiz_id]
            self._write_json(403, {"error": "quiz expired"})
            return

        if isinstance(answers, list):
            graded = grade_submission(answers)
            score_val = graded["score"]
            wrong_list = graded["wrong"]
            total_val = entry.get("count", graded["total"])
        else:
            score_val = score
            try:
                total_val = int(total)
            except (TypeError, ValueError):
                total_val = entry.get("count", len(QUESTION_BANK))
            wrong_list = wrong if isinstance(wrong, list) else []

        if entry.get("count"):
            total_val = entry["count"]

        print(f"[submit] user={user_id}, quiz_id={quiz_id}, score={score_val}")
        del ACTIVE_QUIZZES[quiz_id]

        record_id = store_score_record(user_id, quiz_id, score_val, total_val, self.client_address[0], wrong_list)
        self._write_json(200, {"status": "ok", "record_id": record_id})

    def _write_json(self, code, content, extra_headers=None):
        body = json.dumps(content, ensure_ascii=False).encode()
        self._set_headers(code, len(body), extra_headers=extra_headers)
        self.wfile.write(body)

def run():
    ensure_record_file()
    ensure_user_file()
    server = HTTPServer((HOST, PORT), QuizHandler)
    print(f"Quiz server running on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        server.server_close()

if __name__ == "__main__":
    run()
```

## frontend/home.html
```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Question Bank Console - Quiz Server</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0a1222;
      --panel: rgba(255, 255, 255, 0.07);
      --panel-strong: rgba(255, 255, 255, 0.12);
      --accent: #f2b705;
      --accent-2: #1de9b6;
      --text: #e9edf5;
      --muted: #9aa6bf;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: radial-gradient(circle at 12% 18%, rgba(29, 233, 182, 0.18), transparent 40%),
                  radial-gradient(circle at 80% 10%, rgba(242, 183, 5, 0.16), transparent 38%),
                  radial-gradient(circle at 20% 80%, rgba(255, 255, 255, 0.06), transparent 30%),
                  #0a1222;
      font-family: 'Space Grotesk', 'Segoe UI', system-ui, -apple-system, sans-serif;
      color: var(--text);
      padding: 32px 18px 48px;
    }
    .shell {
      max-width: 1080px;
      margin: 0 auto;
      background: linear-gradient(135deg, rgba(255,255,255,0.08), rgba(255,255,255,0.02));
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 20px;
      padding: 28px 28px 32px;
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
      position: relative;
      overflow: hidden;
    }
    .shell::after {
      content: '';
      position: absolute;
      inset: 0;
      background: radial-gradient(circle at 25% 20%, rgba(29, 233, 182, 0.08), transparent 45%);
      pointer-events: none;
      z-index: 0;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 14px;
      border-radius: 999px;
      background: var(--panel);
      color: var(--muted);
      font-size: 13px;
      letter-spacing: 0.3px;
      text-transform: uppercase;
      position: relative;
      z-index: 1;
    }
    h1 {
      margin: 10px 0 6px;
      font-size: 32px;
      letter-spacing: 0.3px;
      position: relative;
      z-index: 1;
    }
    p.lead {
      margin: 0 0 18px;
      color: var(--muted);
      line-height: 1.6;
      max-width: 820px;
      position: relative;
      z-index: 1;
    }
    .grid {
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 14px;
      margin-top: 18px;
    }
    .card {
      padding: 16px 18px;
      border-radius: 14px;
      background: var(--panel);
      border: 1px solid rgba(255, 255, 255, 0.12);
      text-decoration: none;
      color: inherit;
      transition: transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .card h3 { margin: 0; font-size: 18px; }
    .card p { margin: 0; color: var(--muted); line-height: 1.5; font-size: 14px; }
    .card:hover {
      transform: translateY(-4px);
      border-color: rgba(29, 233, 182, 0.6);
      box-shadow: 0 16px 32px rgba(0, 0, 0, 0.28);
    }
    .panel {
      margin-top: 18px;
      background: var(--panel);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 14px;
      padding: 18px 18px 16px;
      position: relative;
      z-index: 1;
    }
    .panel h2 { margin: 0 0 6px; font-size: 18px; }
    .panel p { margin: 0 0 10px; color: var(--muted); }
    .controls { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
    label { color: var(--text); font-weight: 700; }
    input[type="number"] {
      width: 92px;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid rgba(255, 255, 255, 0.16);
      background: #0e182d;
      color: var(--text);
      font-family: 'Space Grotesk', sans-serif;
    }
    button {
      padding: 12px 16px;
      border-radius: 12px;
      border: none;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #0b141f;
      font-weight: 700;
      cursor: pointer;
      transition: transform 0.16s ease, filter 0.16s ease;
    }
    button:hover { transform: translateY(-2px); filter: brightness(1.05); }
    button.ghost {
      background: transparent;
      color: var(--text);
      border: 1px solid rgba(255, 255, 255, 0.18);
    }
    .live-preview {
      display: grid;
      grid-template-columns: minmax(280px, 1fr) 1.2fr;
      gap: 14px;
      align-items: start;
    }
    .list {
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 12px;
      padding: 12px;
    }
    .list-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; font-weight: 700; }
    .badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 28px;
      height: 24px;
      padding: 0 8px;
      border-radius: 12px;
      background: rgba(29, 233, 182, 0.16);
      color: var(--text);
      font-weight: 700;
    }
    .question-list { display: flex; flex-direction: column; gap: 10px; }
    .question-card {
      padding: 10px 12px;
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
    }
    .q-head { font-weight: 700; margin-bottom: 4px; color: var(--text); }
    .q-body { color: var(--muted); font-size: 14px; line-height: 1.5; }
    .muted { color: var(--muted); margin: 0; }
    pre {
      margin: 0;
      background: #0b1426;
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 12px;
      padding: 12px;
      overflow-x: auto;
      font-family: 'JetBrains Mono', 'Courier New', monospace;
      font-size: 13px;
      line-height: 1.5;
      color: #d2ddff;
      min-height: 220px;
    }
    footer {
      margin-top: 22px;
      color: var(--muted);
      font-size: 13px;
      position: relative;
      z-index: 1;
    }
    .pill .dot {
      display: inline-block;
      width: 8px; height: 8px;
      background: var(--accent-2);
      border-radius: 50%;
      box-shadow: 0 0 0 6px rgba(29, 233, 182, 0.12);
    }
  </style>
</head>
<body>
  <main class="shell">
    <div class="pill"><span class="dot"></span><span id="pill-text">Quiz Delivery - API Console</span></div>
    <h1 id="title-text">English Question Bank Center</h1>
    <p class="lead" id="lead-text">All questions live in <code>data/questions.json</code>. The API samples from that file and ships the payload to any client (ESP32, browser, or others). Use the shortcuts below to test `/questions` and `/submit` instantly.</p>

    <div class="grid">
      <a class="card" href="/questions?user_id=001" target="_blank" rel="noopener">
        <h3>/questions</h3>
        <p id="card-questions">Fetch a random question list (default 10). Add <code>?count=5</code> to customize.</p>
      </a>
      <a class="card" href="/submit">
        <h3>/submit</h3>
        <p id="card-submit">Submission demo with ready-to-send JSON and live response display.</p>
      </a>
      <a class="card" href="/results">
        <h3>/results</h3>
        <p id="card-records">View recent submissions stored in exam_records.json (newest first).</p>
      </a>
      <div class="card" style="cursor: default;">
        <h3>Bank Status</h3>
        <p id="card-status">Entries: <span id="bank-size">--</span> - Default sample: <span id="default-count">10</span> questions</p>
      </div>
    </div>

    <section class="panel">
      <h2 id="panel-title">Quick sample & preview</h2>
      <p id="panel-desc">Set the quantity, request the API, and inspect the exact JSON that will be delivered to devices.</p>
      <div class="controls">
        <label for="count-input" id="count-label">Question count</label>
        <input id="count-input" type="number" min="1" max="20" value="10" />
        <button id="fetch-btn">Fetch questions JSON</button>
        <button id="lang-toggle" class="ghost"> / EN</button>
      </div>
      <div class="live-preview">
        <div class="list">
          <div class="list-header">
            <span id="list-title">Sampled questions</span>
            <span class="badge" id="list-count">0</span>
          </div>
          <div id="question-list" class="question-list"></div>
        </div>
        <pre id="questions-preview">Waiting for request...</pre>
      </div>
    </section>

    <footer id="footer-text">Run the server: <code>python backend/quiz_server.py</code>. Keep the device on the same LAN to access it.</footer>
  </main>

  <script>
    const defaultUserId = '001';
    const preview = document.getElementById('questions-preview');
    const bankSizeText = document.getElementById('bank-size');
    const defaultCountText = document.getElementById('default-count');
    const questionList = document.getElementById('question-list');
    const listCount = document.getElementById('list-count');

    async function fetchQuestions() {
      const countInput = document.getElementById('count-input');
      const count = countInput.value || 10;
      preview.textContent = 'Loading...';
      try {
        const res = await fetch(`/questions?user_id=${encodeURIComponent(defaultUserId)}&count=${count}`);
        const data = await res.json();
        bankSizeText.textContent = data.bank_size ?? '--';
        preview.textContent = JSON.stringify(data, null, 2);
        renderList(data.questions || []);
      } catch (err) {
        preview.textContent = 'Request failed: ' + err;
        renderList([]);
      }
    }

    function renderList(questions) {
      questionList.innerHTML = '';
      listCount.textContent = questions.length;
      if (!questions.length) {
        questionList.innerHTML = '<p class="muted" id="empty-text">No questions sampled yet.</p>';
        return;
      }
      questions.forEach((q, idx) => {
        const card = document.createElement('div');
        card.className = 'question-card';
        card.innerHTML = `<div class="q-head">#${idx + 1}  ${q.id || ''}</div>
                          <div class="q-body">${q.stem || ''}</div>`;
        questionList.appendChild(card);
      });
    }

    const lang = {
      en: {
        pill: 'Quiz Delivery - API Console',
        title: 'English Question Bank Center',
        lead: 'All questions live in data/questions.json. The API samples from that file (requires user_id, default 001) and ships the payload to any client.',
        cardQuestions: 'Fetch a random question list (default 10, requires user_id). Add ?count=5 to customize.',
        cardSubmit: 'Submission demo with ready-to-send JSON and live response display.',
        cardStatus: (bank, defCount) => `Entries: ${bank} - Default sample: ${defCount} questions`,
        cardRecords: 'View recent submissions stored in exam_records.json (newest first).',
        panelTitle: 'Quick sample & preview',
        panelDesc: 'Set the quantity, request the API, and inspect the exact JSON that will be delivered to devices.',
        countLabel: 'Question count',
        fetchBtn: 'Fetch questions JSON',
        listTitle: 'Sampled questions',
        empty: 'No questions sampled yet.',
        footer: 'Run the server: python backend/quiz_server.py. Keep the device on the same LAN to access it.'
      },
      zh: {
        pill: '  ',
        title: '',
        lead: ' data/questions.json user_id 001 ESP32 ',
        cardQuestions: ' 10  user_id ?count=5 ',
        cardSubmit: ' JSON ',
        cardStatus: (bank, defCount) => `${bank}  ${defCount} `,
        cardRecords: ' exam_records.json ',
        panelTitle: '',
        panelDesc: ' JSON',
        countLabel: '',
        fetchBtn: ' JSON',
        listTitle: '',
        empty: '',
        footer: 'python backend/quiz_server.py  '
      }
    };

    let currentLang = 'en';
    function applyLang() {
      const dict = lang[currentLang];
      document.getElementById('pill-text').textContent = dict.pill;
      document.getElementById('title-text').textContent = dict.title;
      document.getElementById('lead-text').textContent = dict.lead;
      document.getElementById('card-questions').textContent = dict.cardQuestions;
      document.getElementById('card-submit').textContent = dict.cardSubmit;
      document.getElementById('card-records').textContent = dict.cardRecords;
      document.getElementById('card-status').innerHTML = dict.cardStatus(bankSizeText.textContent || '--', defaultCountText.textContent || '10');
      document.getElementById('panel-title').textContent = dict.panelTitle;
      document.getElementById('panel-desc').textContent = dict.panelDesc;
      document.getElementById('count-label').textContent = dict.countLabel;
      document.getElementById('fetch-btn').textContent = dict.fetchBtn;
      document.getElementById('list-title').textContent = dict.listTitle;
      document.getElementById('empty-text')?.remove();
      if (!questionList.children.length) {
        const p = document.createElement('p');
        p.className = 'muted';
        p.id = 'empty-text';
        p.textContent = dict.empty;
        questionList.appendChild(p);
      }
      document.getElementById('footer-text').textContent = dict.footer;
    }

    document.getElementById('lang-toggle').addEventListener('click', () => {
      currentLang = currentLang === 'en' ? 'zh' : 'en';
      document.getElementById('lang-toggle').textContent = currentLang === 'en' ? ' / EN' : 'EN / ';
      applyLang();
    });

    document.getElementById('fetch-btn').addEventListener('click', fetchQuestions);
    applyLang();
    fetchQuestions();
  </script>
</body>
</html>
```

## frontend/login.html
```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Login  Quiz Server</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #050a1b;
      --panel: rgba(255, 255, 255, 0.06);
      --border: rgba(255, 255, 255, 0.12);
      --accent: #1de9b6;
      --accent-2: #f2b705;
      --text: #e9edf5;
      --muted: #9aa6bf;
      --danger: #ff6b6b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: radial-gradient(circle at 18% 20%, rgba(29, 233, 182, 0.16), transparent 40%),
                  radial-gradient(circle at 80% 10%, rgba(242, 183, 5, 0.16), transparent 38%),
                  radial-gradient(circle at 25% 80%, rgba(255, 255, 255, 0.04), transparent 35%),
                  var(--bg);
      font-family: 'Space Grotesk', 'Segoe UI', system-ui, sans-serif;
      color: var(--text);
      padding: 32px 16px 48px;
      display: grid;
      place-items: center;
    }
    .shell {
      width: min(480px, 100%);
      background: linear-gradient(145deg, rgba(255,255,255,0.08), rgba(255,255,255,0.03));
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 26px 24px 24px;
      box-shadow: 0 24px 72px rgba(0, 0, 0, 0.35);
      position: relative;
      overflow: hidden;
    }
    .shell::after {
      content: '';
      position: absolute;
      width: 240px; height: 240px;
      background: radial-gradient(circle, rgba(29, 233, 182, 0.14), transparent 60%);
      top: -60px; right: -80px;
      filter: blur(4px);
      z-index: 0;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 14px;
      border-radius: 999px;
      background: var(--panel);
      color: var(--muted);
      font-size: 13px;
      letter-spacing: 0.3px;
      text-transform: uppercase;
      position: relative;
      z-index: 1;
    }
    .pill .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 6px rgba(29, 233, 182, 0.14); }
    h1 { margin: 12px 0 6px; font-size: 28px; position: relative; z-index: 1; }
    p.lead { margin: 0 0 18px; color: var(--muted); line-height: 1.5; position: relative; z-index: 1; }
    label { display: block; margin: 10px 0 6px; font-weight: 700; color: var(--text); position: relative; z-index: 1; }
    input[type="text"], input[type="password"] {
      width: 100%;
      padding: 12px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: #0a152b;
      color: var(--text);
      font-size: 15px;
      font-family: 'Space Grotesk', 'Segoe UI', system-ui, sans-serif;
      position: relative;
      z-index: 1;
    }
    .field {
      position: relative;
    }
    .toggle {
      position: absolute;
      right: 12px;
      top: 50%;
      transform: translateY(-50%);
      font-size: 12px;
      color: var(--muted);
      cursor: pointer;
      user-select: none;
    }
    button {
      width: 100%;
      margin-top: 16px;
      padding: 12px 14px;
      border-radius: 12px;
      border: none;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #0c1522;
      font-weight: 700;
      cursor: pointer;
      transition: transform 0.16s ease, filter 0.16s ease;
      position: relative;
      z-index: 1;
    }
    button:hover { transform: translateY(-2px); filter: brightness(1.05); }
    .status {
      margin-top: 12px;
      font-size: 14px;
      color: var(--muted);
      min-height: 22px;
      position: relative;
      z-index: 1;
    }
    .status.error { color: var(--danger); }
    .hint {
      margin-top: 10px;
      font-size: 13px;
      color: var(--muted);
      position: relative;
      z-index: 1;
    }
    code { font-family: 'JetBrains Mono', 'Courier New', monospace; background: rgba(255,255,255,0.06); padding: 2px 6px; border-radius: 8px; color: #d2ddff; }
  </style>
</head>
<body>
  <main class="shell">
    <div class="pill"><span class="dot"></span><span>Access gate</span></div>
    <h1>Sign in to Quiz Server</h1>
    <p class="lead">Enter your account to unlock the question delivery, submission test, and records console.</p>

    <form id="login-form">
      <label for="username">Username</label>
      <input id="username" name="username" type="text" autocomplete="username" required placeholder="admin" />

      <label for="password">Password</label>
      <div class="field">
        <input id="password" name="password" type="password" autocomplete="current-password" required placeholder="" />
        <span id="toggle" class="toggle" aria-label="toggle password visibility">Show</span>
      </div>

      <button type="submit" id="login-btn">Login</button>
      <div id="status" class="status"></div>
      <div class="hint">Default test account: <code>admin / admin123</code> (defined in <code>data/users.json</code>).</div>
    </form>
  </main>

  <script>
    const form = document.getElementById('login-form');
    const statusEl = document.getElementById('status');
    const passwordInput = document.getElementById('password');
    const toggleBtn = document.getElementById('toggle');

    toggleBtn.addEventListener('click', () => {
      const isPwd = passwordInput.type === 'password';
      passwordInput.type = isPwd ? 'text' : 'password';
      toggleBtn.textContent = isPwd ? 'Hide' : 'Show';
    });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      statusEl.textContent = 'Validating...';
      statusEl.classList.remove('error');
      const username = document.getElementById('username').value.trim();
      const password = passwordInput.value;
      try {
        const res = await fetch('/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password })
        });
        const data = await res.json();
        if (!res.ok) {
          statusEl.textContent = data.error || 'Login failed';
          statusEl.classList.add('error');
          return;
        }
        statusEl.textContent = 'Login success, redirecting...';
        setTimeout(() => { window.location.href = '/'; }, 500);
      } catch (err) {
        statusEl.textContent = 'Request failed: ' + err;
        statusEl.classList.add('error');
      }
    });

    async function checkSession() {
      try {
        const res = await fetch('/auth/status');
        if (!res.ok) return;
        const data = await res.json();
        if (data.authenticated) {
          window.location.href = '/';
        }
      } catch (err) {
        // ignore
      }
    }
    checkSession();
  </script>
</body>
</html>
```

## frontend/records.html
```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Records  Quiz Server</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #050a18;
      --panel: rgba(255, 255, 255, 0.07);
      --border: rgba(255, 255, 255, 0.12);
      --accent: #1de9b6;
      --accent-2: #f2b705;
      --text: #e9edf5;
      --muted: #9aa6bf;
      --danger: #ff6b6b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: radial-gradient(circle at 16% 18%, rgba(29, 233, 182, 0.14), transparent 40%),
                  radial-gradient(circle at 80% 12%, rgba(242, 183, 5, 0.15), transparent 45%),
                  #050a18;
      font-family: 'Space Grotesk', 'Segoe UI', system-ui, sans-serif;
      color: var(--text);
      padding: 30px 16px 42px;
    }
    .shell {
      max-width: 1180px;
      margin: 0 auto;
      background: linear-gradient(150deg, rgba(255,255,255,0.08), rgba(255,255,255,0.02));
      border: 1px solid var(--border);
      border-radius: 22px;
      padding: 26px 26px 30px;
      box-shadow: 0 22px 76px rgba(0, 0, 0, 0.35);
      position: relative;
      overflow: hidden;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 14px;
      border-radius: 999px;
      background: var(--panel);
      color: var(--muted);
      font-size: 13px;
      letter-spacing: 0.3px;
      text-transform: uppercase;
      position: relative;
      z-index: 1;
    }
    .pill .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 6px rgba(29, 233, 182, 0.14); }
    h1 { margin: 10px 0 6px; font-size: 30px; letter-spacing: 0.2px; position: relative; z-index: 1; }
    p.lead { margin: 0 0 18px; color: var(--muted); line-height: 1.6; position: relative; z-index: 1; }
    .controls { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 12px 0 14px; }
    input[type="number"], input[type="text"] {
      width: 120px;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid rgba(255, 255, 255, 0.16);
      background: #0b1426;
      color: var(--text);
      font-family: 'Space Grotesk', sans-serif;
    }
    input[type="text"] { width: 180px; }
    button {
      padding: 12px 16px;
      border-radius: 12px;
      border: none;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #0c1522;
      font-weight: 700;
      cursor: pointer;
      transition: transform 0.16s ease, filter 0.16s ease;
    }
    button:hover { transform: translateY(-2px); filter: brightness(1.05); }
    button.secondary {
      background: transparent;
      border: 1px solid rgba(255, 255, 255, 0.18);
      color: var(--text);
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 10px;
      margin: 12px 0 8px;
    }
    .stat {
      padding: 12px 14px;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.12);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .stat .label { color: var(--muted); font-size: 13px; }
    .stat .value { font-size: 22px; font-weight: 700; }
    .grid {
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 14px;
      align-items: start;
    }
    .list {
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 14px;
      padding: 12px;
      min-height: 260px;
    }
    .list-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; font-weight: 700; }
    .badge { display: inline-flex; align-items: center; justify-content: center; min-width: 28px; height: 24px; padding: 0 8px; border-radius: 12px; background: rgba(29, 233, 182, 0.16); color: var(--text); font-weight: 700; }
    .record-card {
      padding: 12px 14px;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
      margin-bottom: 10px;
    }
    .record-card:last-child { margin-bottom: 0; }
    .row { display: flex; gap: 8px; flex-wrap: wrap; font-size: 13px; color: var(--muted); }
    .score { color: #1de9b6; font-weight: 700; }
    .timestamp { color: #e9edf5; font-weight: 700; }
    .tag { display: inline-flex; align-items: center; gap: 4px; padding: 4px 8px; border-radius: 999px; background: rgba(255,255,255,0.07); color: var(--text); font-size: 12px; }
    .muted { color: var(--muted); margin: 0; }
    .wrong-list { margin: 6px 0 0; padding-left: 14px; color: var(--danger); font-size: 13px; }
    pre {
      margin: 0;
      background: #0b1426;
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 14px;
      padding: 12px;
      overflow-x: auto;
      font-family: 'JetBrains Mono', 'Courier New', monospace;
      font-size: 13px;
      line-height: 1.5;
      color: #d2ddff;
      min-height: 260px;
    }
    footer { margin-top: 18px; color: var(--muted); font-size: 13px; }
  </style>
</head>
<body>
  <main class="shell">
    <div class="pill"><span class="dot"></span><span id="pill-text">Recent submissions</span></div>
    <h1 id="title-text">Student answer records</h1>
    <p class="lead" id="lead-text">Fetch latest records from data/exam_records.json. You can limit the count or query a specific record_id, and inspect both the list and raw JSON.</p>

    <div class="controls">
      <label for="limit-input" id="limit-label">Show latest</label>
      <input id="limit-input" type="number" min="1" max="200" value="20" />
      <span id="limit-unit">records</span>
      <label for="id-input" id="id-label">record_id</label>
      <input id="id-input" type="text" placeholder="optional" />
      <button id="fetch-btn">Fetch</button>
      <button class="secondary" id="lang-toggle"> / EN</button>
      <a class="secondary" href="/" style="display:inline-flex;align-items:center;gap:6px;padding:12px 14px;text-decoration:none;">Home</a>
      <a class="secondary" href="/submit" style="display:inline-flex;align-items:center;gap:6px;padding:12px 14px;text-decoration:none;">Submit</a>
    </div>

    <div class="stats">
      <div class="stat">
        <span class="label" id="stat-count-label">Loaded records</span>
        <span class="value" id="stat-count">0</span>
      </div>
      <div class="stat">
        <span class="label" id="stat-avg-label">Average score</span>
        <span class="value" id="stat-avg">-</span>
      </div>
      <div class="stat">
        <span class="label" id="stat-last-label">Latest client IP</span>
        <span class="value" id="stat-ip">-</span>
      </div>
    </div>

    <div class="grid">
      <div class="list">
        <div class="list-header">
          <span id="list-title">Latest records</span>
          <span class="badge" id="list-count">0</span>
        </div>
        <div id="records-container">
          <p class="muted" id="empty-text">No records yet.</p>
        </div>
      </div>
      <pre id="json-preview">Waiting for request...</pre>
    </div>

    <footer id="footer-text">Data source: data/exam_records.json  newest first  supports record_id lookup</footer>
  </main>

  <script>
    const listEl = document.getElementById('records-container');
    const listCount = document.getElementById('list-count');
    const preview = document.getElementById('json-preview');
    const statCount = document.getElementById('stat-count');
    const statAvg = document.getElementById('stat-avg');
    const statIp = document.getElementById('stat-ip');

    const lang = {
      en: {
        pill: 'Recent submissions',
        title: 'Student answer records',
        lead: 'Fetch latest records from data/exam_records.json. You can limit the count or query a specific record_id, and inspect both the list and raw JSON.',
        limitLabel: 'Show latest',
        limitUnit: 'records',
        idLabel: 'record_id',
        fetch: 'Fetch',
        listTitle: 'Latest records',
        empty: 'No records yet.',
        footer: 'Data source: data/exam_records.json  newest first  supports record_id lookup',
        statCount: 'Loaded records',
        statAvg: 'Average score',
        statLast: 'Latest client IP',
        wrongText: (n) => `${n} wrong`
      },
      zh: {
        pill: '',
        title: '',
        lead: ' data/exam_records.json  record_id  JSON',
        limitLabel: '',
        limitUnit: '',
        idLabel: 'record_id',
        fetch: '',
        listTitle: '',
        empty: '',
        footer: 'data/exam_records.json     record_id ',
        statCount: '',
        statAvg: '',
        statLast: ' IP',
        wrongText: (n) => ` ${n} `
      }
    };

    let currentLang = 'en';

    function renderStats(records) {
      const total = records.length;
      statCount.textContent = total;
      if (!total) {
        statAvg.textContent = '-';
        statIp.textContent = '-';
        return;
      }
      const sum = records.reduce((acc, r) => acc + (r.score || 0), 0);
      statAvg.textContent = ((sum / total) || 0).toFixed(2);
      statIp.textContent = records[0].client_ip || '-';
    }

    function renderList(records) {
      listEl.innerHTML = '';
      listCount.textContent = records.length;
      if (!records.length) {
        const p = document.createElement('p');
        p.className = 'muted';
        p.id = 'empty-text';
        p.textContent = lang[currentLang].empty;
        listEl.appendChild(p);
        renderStats(records);
        return;
      }
      records.forEach((r) => {
        const card = document.createElement('div');
        card.className = 'record-card';
        const wrongCount = Array.isArray(r.wrong) ? r.wrong.length : 0;
        const wrongItems = (r.wrong || []).map(w => `<li>Q: ${w.id ?? '-'}  correct: ${w.correct ?? '-'}  yours: ${w.your ?? '-'}</li>`).join('');
        card.innerHTML = `
          <div class="row"><span class="timestamp">${r.timestamp || ''}</span>  <span>${r.client_ip || '-'}</span>  <span class="tag">id: ${r.id || '-'}</span></div>
          <div class="row"><span class="tag">user: ${r.user_id || '-'}</span>  <span class="tag">quiz: ${r.quiz_id || '-'}</span></div>
          <div class="row"><span class="score">${r.score ?? '-'} / ${r.total ?? '-'}</span>  <span class="tag">${lang[currentLang].wrongText(wrongCount)}</span></div>
          ${wrongCount ? `<ul class="wrong-list">${wrongItems}</ul>` : ''}
        `;
        listEl.appendChild(card);
      });
      renderStats(records);
    }

    async function fetchRecords() {
      const limit = document.getElementById('limit-input').value || 20;
      const rid = document.getElementById('id-input').value.trim();
      preview.textContent = currentLang === 'en' ? 'Loading...' : '...';
      try {
        const url = rid ? `/results.json?id=${encodeURIComponent(rid)}` : `/records.json?limit=${limit}`;
        const res = await fetch(url);
        const data = await res.json();
        if (data.record) {
          renderList([data.record]);
          preview.textContent = JSON.stringify(data, null, 2);
        } else {
          renderList(data.records || []);
          preview.textContent = JSON.stringify(data, null, 2);
        }
      } catch (err) {
        preview.textContent = (currentLang === 'en' ? 'Request failed: ' : ': ') + err;
        renderList([]);
      }
    }

    function applyLang() {
      const dict = lang[currentLang];
      document.getElementById('pill-text').textContent = dict.pill;
      document.getElementById('title-text').textContent = dict.title;
      document.getElementById('lead-text').textContent = dict.lead;
      document.getElementById('limit-label').textContent = dict.limitLabel;
      document.getElementById('limit-unit').textContent = dict.limitUnit;
      document.getElementById('id-label').textContent = dict.idLabel;
      document.getElementById('fetch-btn').textContent = dict.fetch;
      document.getElementById('list-title').textContent = dict.listTitle;
      document.getElementById('footer-text').textContent = dict.footer;
      document.getElementById('stat-count-label').textContent = dict.statCount;
      document.getElementById('stat-avg-label').textContent = dict.statAvg;
      document.getElementById('stat-last-label').textContent = dict.statLast;
      document.getElementById('lang-toggle').textContent = currentLang === 'en' ? ' / EN' : 'EN / ';
      if (!listEl.children.length) {
        renderList([]);
      }
    }

    document.getElementById('fetch-btn').addEventListener('click', fetchRecords);
    document.getElementById('lang-toggle').addEventListener('click', () => {
      currentLang = currentLang === 'en' ? 'zh' : 'en';
      applyLang();
    });

    applyLang();
    fetchRecords();
  </script>
</body>
</html>
```

## frontend/submit.html
```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>/submit - Quiz Server</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #08101f;
      --panel: rgba(255, 255, 255, 0.08);
      --border: rgba(255, 255, 255, 0.15);
      --accent: #1de9b6;
      --accent-2: #f2b705;
      --text: #eaf0ff;
      --muted: #93a0bc;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: radial-gradient(circle at 18% 22%, rgba(242, 183, 5, 0.16), transparent 40%),
                  radial-gradient(circle at 82% 12%, rgba(29, 233, 182, 0.16), transparent 35%),
                  #08101f;
      font-family: 'Space Grotesk', 'Segoe UI', system-ui, sans-serif;
      color: var(--text);
      padding: 28px 16px 40px;
    }
    .shell {
      max-width: 960px;
      margin: 0 auto;
      background: linear-gradient(145deg, rgba(255,255,255,0.08), rgba(255,255,255,0.02));
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 24px 24px 28px;
      box-shadow: 0 20px 70px rgba(0, 0, 0, 0.35);
      position: relative;
      overflow: hidden;
    }
    .shell::before {
      content: '';
      position: absolute;
      width: 180px; height: 180px;
      background: radial-gradient(circle, rgba(29, 233, 182, 0.2), transparent 60%);
      top: -60px; right: -40px;
      filter: blur(4px);
      z-index: 0;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 14px;
      border-radius: 999px;
      background: var(--panel);
      color: var(--muted);
      font-size: 13px;
      letter-spacing: 0.3px;
      text-transform: uppercase;
      position: relative;
      z-index: 1;
    }
    .pill .dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 0 6px rgba(29, 233, 182, 0.14);
    }
    h1 { margin: 10px 0 6px; font-size: 30px; letter-spacing: 0.2px; position: relative; z-index: 1; }
    p.lead { margin: 0 0 18px; color: var(--muted); line-height: 1.6; max-width: 780px; position: relative; z-index: 1; }
    textarea {
      width: 100%;
      min-height: 200px;
      border-radius: 14px;
      border: 1px solid var(--border);
      background: #0a182e;
      color: #d9e3ff;
      padding: 12px;
      font-family: 'JetBrains Mono', 'Courier New', monospace;
      font-size: 14px;
      box-sizing: border-box;
      position: relative;
      z-index: 1;
    }
    .actions { margin: 12px 0 8px; display: flex; gap: 12px; flex-wrap: wrap; position: relative; z-index: 1; }
    button {
      padding: 12px 16px;
      border-radius: 12px;
      border: none;
      background: linear-gradient(135deg, var(--accent-2), var(--accent));
      color: #0c1522;
      font-weight: 700;
      cursor: pointer;
      transition: transform 0.16s ease, filter 0.16s ease;
    }
    button.secondary {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--text);
    }
    button:hover { transform: translateY(-2px); filter: brightness(1.05); }
    pre {
      margin: 0;
      background: #0b1426;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px;
      overflow-x: auto;
      font-family: 'JetBrains Mono', 'Courier New', monospace;
      font-size: 13px;
      color: #d2ddff;
      position: relative;
      z-index: 1;
      min-height: 220px;
    }
    a { color: var(--accent); text-decoration: none; font-weight: 600; }
    a:hover { color: #7ef3d8; }
    footer { margin-top: 16px; color: var(--muted); font-size: 13px; position: relative; z-index: 1; }
    .split {
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 14px;
      align-items: start;
    }
    .list {
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 12px;
      padding: 12px;
    }
    .list-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 8px;
      font-weight: 700;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 28px;
      height: 24px;
      padding: 0 8px;
      border-radius: 12px;
      background: rgba(29, 233, 182, 0.16);
      color: var(--text);
      font-weight: 700;
    }
    .question-list { display: flex; flex-direction: column; gap: 10px; }
    .question-card {
      padding: 10px 12px;
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
    }
    .q-head { font-weight: 700; margin-bottom: 4px; color: var(--text); }
    .q-body { color: var(--muted); font-size: 14px; line-height: 1.5; }
    .muted { color: var(--muted); margin: 0; }
  </style>
</head>
<body>
  <main class="shell">
    <div class="pill"><span class="dot"></span><span id="pill-text">POST /submit - Answer return demo</span></div>
    <h1 id="title-text">Send quiz result with user & quiz binding</h1>
    <p class="lead" id="lead-text">Send JSON to <code>/submit</code> with <code>{ user_id, quiz_id, score, total, wrong }</code>. Use Load sample to fetch a new quiz_id from /questions (requires user_id).</p>

    <textarea id="payload">{
  "user_id": "001",
  "quiz_id": "REPLACE_WITH_QUIZ_ID",
  "score": 0,
  "total": 10,
  "wrong": []
}</textarea>
    <div class="actions">
      <button id="send-btn" onclick="send()">Send to /submit</button>
      <button class="secondary" id="load-btn" onclick="loadQuestions()">Load sample from bank</button>
      <button class="secondary" id="lang-toggle"> / EN</button>
      <a href="/" class="secondary" style="display:inline-flex;align-items:center;gap:6px;padding:12px 14px;">Back to home</a>
    </div>
    <div class="split">
      <pre id="result">Waiting for request...</pre>
      <div class="list">
        <div class="list-header">
          <span id="list-title">Sampled questions</span>
          <span class="badge" id="list-count">0</span>
        </div>
        <div id="question-list" class="question-list">
          <p class="muted" id="empty-text">No questions sampled yet.</p>
        </div>
      </div>
    </div>
    <footer id="footer-text">Each submission is appended to <code>data/exam_records.json</code> for later stats.</footer>
  </main>

  <script>
    const defaultUserId = '001';
    const listEl = document.getElementById('question-list');
    const listCount = document.getElementById('list-count');

    async function send() {
      const payloadBox = document.getElementById('payload');
      const resultBox = document.getElementById('result');
      resultBox.textContent = 'Sending...';
      try {
        const response = await fetch('/submit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: payloadBox.value
        });
        const text = await response.text();
        resultBox.textContent = 'Status: ' + response.status + '\n' + text;
      } catch (err) {
        resultBox.textContent = 'Request failed: ' + err;
      }
    }

    async function loadQuestions() {
      const resultBox = document.getElementById('result');
      resultBox.textContent = currentLang === 'en' ? 'Pulling sample questions...' : '...';
      try {
        const res = await fetch(`/questions?user_id=${encodeURIComponent(defaultUserId)}&count=3`);
        const data = await res.json();
        const template = {
          user_id: data.user_id,
          quiz_id: data.quiz_id,
          score: 0,
          total: data.total ?? data.questions.length,
          wrong: [],
          answers: data.questions.slice(0, 3).map(q => ({ id: q.id, choice: 0 })) // reference only
        };
        document.getElementById('payload').value = JSON.stringify(template, null, 2);
        renderList(data.questions || []);
        resultBox.textContent = currentLang === 'en'
          ? 'Sample filled. Adjust score/wrong as needed, then send.'
          : '/';
      } catch (err) {
        resultBox.textContent = (currentLang === 'en' ? 'Unable to load questions: ' : ': ') + err;
        renderList([]);
      }
    }

    function renderList(questions) {
      listEl.innerHTML = '';
      listCount.textContent = questions.length;
      if (!questions.length) {
        const p = document.createElement('p');
        p.className = 'muted';
        p.id = 'empty-text';
        p.textContent = currentLang === 'en' ? 'No questions sampled yet.' : '';
        listEl.appendChild(p);
        return;
      }
      questions.forEach((q, idx) => {
        const card = document.createElement('div');
        card.className = 'question-card';
        card.innerHTML = `<div class="q-head">#${idx + 1}  ${q.id || ''}</div>
                          <div class="q-body">${q.stem || ''}</div>`;
        listEl.appendChild(card);
      });
    }

    const lang = {
      en: {
        pill: 'POST /submit - Score submit',
        title: 'Send quiz result with user & quiz binding',
        lead: 'Send JSON to /submit with { user_id, quiz_id, score, total, wrong }. Use Load sample to fetch a quiz_id first.',
        send: 'Send to /submit',
        load: 'Load sample from bank',
        listTitle: 'Sampled questions',
        empty: 'No questions sampled yet.',
        footer: 'Each submission is appended to data/exam_records.json for later stats.'
      },
      zh: {
        pill: 'POST /submit  ',
        title: ' user_id / quiz_id',
        lead: ' /submit  { user_id, quiz_id, score, total, wrong }    quiz_id',
        send: ' /submit',
        load: '',
        listTitle: '',
        empty: '',
        footer: ' data/exam_records.json'
      }
    };

    let currentLang = 'en';
    function applyLang() {
      const dict = lang[currentLang];
      document.getElementById('pill-text').textContent = dict.pill;
      document.getElementById('title-text').textContent = dict.title;
      document.getElementById('lead-text').textContent = dict.lead;
      document.getElementById('send-btn').textContent = dict.send;
      document.getElementById('load-btn').textContent = dict.load;
      document.getElementById('list-title').textContent = dict.listTitle;
      document.getElementById('footer-text').textContent = dict.footer;
      document.getElementById('lang-toggle').textContent = currentLang === 'en' ? ' / EN' : 'EN / ';
      if (!listEl.children.length) {
        renderList([]);
      }
    }

    document.getElementById('lang-toggle').addEventListener('click', () => {
      currentLang = currentLang === 'en' ? 'zh' : 'en';
      applyLang();
    });

    applyLang();
  </script>
</body>
</html>
```
