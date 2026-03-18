# Quiz Server Guide

## Layout
- backend/quiz_server.py : HTTP server entry; serves API + frontend
- frontend/login.html : login gate before accessing other pages (route `/login`).
- frontend/home.html : dashboard for fetching question sets (route `/`).
- frontend/admin.html : admin console (route `/admin`).
- frontend/assistant.html : assistant console (route `/assistant`).
- frontend/teacher.html : teacher/assistant classroom console (route `/teacher`).
- data/questions.json : unified question bank.
- data/exam_records.json : historical legacy record seed file.
- data/server_users.json : account list for 服务器端登录页 `/login`。
- data/client_users.json : account list for 客户端登录接口 `/client/login`。

## Run
```
start_server.bat
```
or in PowerShell:
```powershell
.\start_server.ps1
```
Both scripts pin runtime to `.venv` created from Python 3.13.

Listens on `0.0.0.0:8000`; open `http://<ip>:8000/` in a browser.

## Question format
```json
{
  "id": "Q1",
  "stem": "Which sentence uses the present perfect tense correctly?",
  "options": ["...", "...", "...", "..."],
  "answer": 0
}
```
`answer` is the index of the correct option (zero-based).

## Authentication
- Login page: `GET /login` (serves `frontend/login.html`).
- Login API (browser flow): `POST /login` with body `{"username": "...", "password": "..."}`.
  - On success: `200 {"status":"ok","user":"...","token":"<session_token>"}` + cookie `session=<token>; Path=/; HttpOnly; SameSite=Lax`.
  - On failure: `401 {"error":"invalid credentials"}`.
- Client login API (for devices/apps): `POST /api/login` with the same body. Returns only JSON (no cookie). Success response matches above and includes `token` for clients to store. 认证数据来源：`data/server_users.json`。
- Client login API (minimal shape，独立账号源): `POST /client/login` with the same body，使用 `data/client_users.json` 校验，返回 `200 {"ok": true, "user": "...", "token": "<session_token>"}`；失败返回 `401 {"ok": false, "error": "invalid credentials"}`。
- Session check: `GET /auth/status` returns `{"authenticated": true|false, "user": "<name|null>"}`.
- All other HTML pages and APIs now require a valid session (cookie or bearer token). Unauthenticated requests:
  - HTML routes redirect to `/login`.
  - JSON APIs return `401 {"error": "unauthorized"}`.
- Non-cookie clients can send `Authorization: Bearer <token>` (token returned by `/api/login` or `/login`) with API requests to stay authenticated.
- 账号分流：
  - 服务器端登录页 `/login` 使用 `data/server_users.json`。
  - 客户端登录 `/client/login` 使用 `data/client_users.json`。
  - 默认会在缺失时创建包含 `{"username":"001","password":"666"}` 的文件。格式统一：`[{"username":"...","password":"..."}]`。

## APIs
- GET `/api/client/exams`
  - Returns available exams for the authenticated client account.
- POST `/api/client/exams/<exam_id>/start`
  - Starts or resumes an attempt and returns attempt context plus questions.
- POST `/api/client/attempts/<attempt_id>/progress`
  - Saves per-question progress during answering.
- POST `/api/client/attempts/<attempt_id>/submit`
  - Final submission endpoint for client attempts.

## Frontend tips
- `/` lets you set a question count and instantly preview the JSON response.
- `/assistant` is the main operational console for assistants.
- `/teacher` is the classroom live board and results console for teachers and assistants.

## Customization
- Add/edit questions in `data/questions.json` (keep the same keys/types).
- Change default sample size via `DEFAULT_QUESTION_COUNT` in `backend/quiz_server.py`.
- Adjust host/port by editing `HOST` and `PORT` constants if needed.
