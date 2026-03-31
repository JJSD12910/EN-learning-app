# Quiz Server 教学测评系统

一个基于 Flask + SQLite + 原生 HTML/JS 的教学测评系统。
面向校内或局域网测试环境，覆盖题库、班级、学生、考试、成绩、错题和导出流程。

## 1. 当前版本说明

- 浏览器登录角色：`admin`、`assistant`、`teacher`
- 学生通过 ESP32 或其他设备调用 `/client/login`，不提供学生网页登录入口
- 默认数据库文件：`data/quiz.db`
- 服务入口：`backend/quiz_server.py`
- 应用工厂：`backend/app.py`
- 主要业务路由：`backend/routes.py`

## 2. 角色与入口

- 管理员：入口 `/admin`，负责教师账号和学校管理
- 助教：入口 `/assistant`，负责题库、学生设备账号、班级、学生、考试、错题、导出、审计和测试数据重置
- 教师：入口 `/teacher`，负责查看班级、考试、成绩、题目统计和学生分析
- 学生设备端：通过 API 登录和作答，不使用网页登录页

## 3. 页面与接口

### 页面

- `/login`
- `/admin`
- `/assistant`
- `/teacher`

### 常用接口

- `GET /auth/status`
- `POST /login`
- `POST /client/login`
- `GET /api/client/exams`
- `POST /api/client/exams/<exam_id>/start`
- `POST /api/client/attempts/<attempt_id>/progress`
- `POST /api/client/attempts/<attempt_id>/submit`

## 4. 启动方式

### Windows PowerShell

```powershell
.\start_server.ps1
```

### Windows CMD

```bat
start_server.bat
```

### 手动启动

```bash
python -m venv .venv
.venv\Scripts\python -m pip install flask sqlalchemy waitress
.venv\Scripts\python backend/quiz_server.py
```

### 默认访问地址

- `http://127.0.0.1:8000`
- `http://<局域网IP>:8000`

## 5. 测试账号

### 浏览器端

- `2025 / 2025`
- `admin / admin123`
- `assistant / assistant123`
- `teacher / teacher123`

### 学生设备端

- `001 / 666`

### 说明

- 登录页点击对应角色会自动填充测试账号和密码
- 系统启动时会同步种子账号到当前数据库
- 数据库中保存的是密码哈希，不是明文

## 6. 数据目录

- `data/server_users.json`：浏览器端种子账号
- `data/client_users.json`：学生设备端种子账号
- `data/questions.json`：题库
- `data/exam_records.json`：历史记录初始化文件
- `data/exports/`：导出目录
- `data/quiz.db`：SQLite 数据库

## 7. 开发提示

- 后端模型：`backend/models.py`
- 数据库迁移：`backend/migrations.py`
- 鉴权与会话：`backend/auth.py`
- 前端页面：`frontend/*.html`

## 8. 故障排查

- 当前服务端会优先使用 `waitress` 运行；如果环境中未安装 `waitress`，会自动退回 Flask 自带线程模式
- SQLite 连接已启用 `WAL`、`busy_timeout` 等参数，适合局域网小规模并发设备作答
- 启动报导入错误时，先确认使用 `.venv` 解释器，并执行：

```powershell
.\.venv\Scripts\python.exe -m py_compile backend\auth.py backend\app.py backend\routes.py
```

- 能打开页面但无法登录时，检查 `data/server_users.json` 或 `data/client_users.json` 是否被修改，并重启服务
- 学生设备端无法作答时，确认先调用 `/client/login` 获取 token，并在后续请求中带上 `Authorization: Bearer <token>`

## 9. 相关文档

- `docs/README.md`
- `docs/用户说明手册.md`
- `docs/server_code_document.md`（旧版实现归档，不代表当前运行逻辑）
