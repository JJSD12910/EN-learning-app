# EN Learning App（英语学习与测评系统）

一个基于 **Flask + SQLite + 原生前端页面** 的英语学习与测评项目，支持：

- 管理端/教师端/学生端多角色登录
- 题库管理、随机抽题、在线答题与成绩统计
- 班级与学生档案管理
- 错题记录、训练与导出
- 审计日志与基础运维接口

该项目适合校园内网部署、课堂测验、英语练习与教学数据留痕场景。

---

## 1. 项目结构

```text
EN-learning-app/
├─ backend/                 # Flask 后端（应用入口、路由、鉴权、数据库模型）
├─ frontend/                # 前端页面（登录、管理、教师、记录等）
├─ data/                    # SQLite 数据库、题库与导出文件
├─ docs/                    # 使用手册与设计文档
├─ start_server.bat         # Windows 启动脚本（cmd）
├─ start_server.ps1         # Windows 启动脚本（PowerShell）
└─ README.md
```

核心入口：

- 服务入口：`backend/quiz_server.py`
- 应用工厂：`backend/app.py`
- 主要路由：`backend/routes.py`

---

## 2. 功能概览

### 2.1 账号与权限

- **管理员（admin）**：系统配置、账号管理、全局数据查看与维护
- **教师（teacher）**：班级/学生管理、测验组织、教学追踪
- **客户端用户（client）**：学生端登录、参与测验与训练

系统支持 Cookie 会话与 Bearer Token 两种鉴权方式。

### 2.2 测评能力

- 从题库按数量随机抽题
- 交卷自动判分并记录错题
- 支持成绩记录查询与统计
- 支持错题导出（导出目录默认在 `data/exports/`）

### 2.3 数据持久化

- 使用 SQLite（默认文件：`data/quiz.db`）
- 首次启动可自动从 JSON 初始化基础数据（账号、题库、历史记录）

---

## 3. 运行环境

- Python 3.13（项目脚本按 3.13 创建虚拟环境）
- 依赖：`flask`、`sqlalchemy`
- 操作系统：
  - Windows：可直接使用提供的 `.bat` / `.ps1` 脚本
  - Linux/macOS：可手动创建虚拟环境并启动

---

## 4. 快速启动

### 4.1 Windows（推荐）

```bat
start_server.bat
```

或 PowerShell：

```powershell
.\start_server.ps1
```

脚本行为：

1. 在项目根目录创建 `.venv`（若不存在）
2. 检查 `flask` 和 `sqlalchemy` 是否可导入
3. 启动 `backend/quiz_server.py`

### 4.2 Linux / macOS（手动）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install flask sqlalchemy
python backend/quiz_server.py
```

### 4.3 访问地址

服务默认监听：

- `http://127.0.0.1:8000`
- 局域网访问：`http://<你的机器IP>:8000`

---

## 5. 初始化数据与默认账号

项目在数据库为空时会尝试从以下文件导入数据：

- `data/server_users.json`（管理端/教师端账号）
- `data/client_users.json`（学生端账号）
- `data/questions.json`（题库）
- `data/exam_records.json`（历史记录）

若对应文件缺失，会写入最小默认数据（例如默认管理员/教师/客户端测试账号），便于快速启动验证。

> 建议在生产环境首次部署后，立即修改默认密码并备份 `data/quiz.db`。

---

## 6. 常用页面与接口（示例）

### 页面

- `/login`：登录页
- `/`：主页面（根据权限呈现相应能力）
- `/records`：记录查看
- 其他页面位于 `frontend/`（如 `admin.html`、`teacher.html`、`submit.html`）

### 接口（部分）

- `POST /login`：登录（浏览器流程）
- `POST /api/login`：登录（API 流程）
- `POST /client/login`：客户端登录
- `GET /questions?count=10`：获取随机题目
- `POST /submit`：提交答卷并判分
- `GET /records.json?limit=20`：获取最近记录

> 完整字段与业务规则请结合 `backend/routes.py` 与 `docs/` 文档阅读。

---

## 7. 开发说明

### 7.1 目录约定

- 后端逻辑优先放在 `backend/`
- 静态资源放在 `frontend/static/`
- 测试数据与导出文件放在 `data/`

### 7.2 常见开发任务

- **修改监听端口**：编辑 `backend/quiz_server.py` 中 `HOST` / `PORT`
- **调整默认抽题数**：编辑 `backend/routes.py` 中 `DEFAULT_QUESTION_COUNT`
- **扩充题库**：编辑 `data/questions.json`

### 7.3 数据安全建议

- 定期备份 `data/quiz.db`
- 避免将真实生产数据直接提交到版本库
- 导出文件目录（`data/exports/`）建议设置清理策略

---

## 8. 故障排查

1. **启动报依赖缺失**
   - 确认当前解释器为虚拟环境
   - 重新安装依赖：`pip install flask sqlalchemy`

2. **端口被占用**
   - 修改 `backend/quiz_server.py` 中端口后重启

3. **登录失败**
   - 检查账号来源文件（`data/server_users.json` / `data/client_users.json`）
   - 检查数据库中账号是否被禁用

4. **页面可打开但接口 401**
   - 确认是否已登录且会话有效
   - API 调用时带上 `Authorization: Bearer <token>`

---

## 9. 文档索引

- `docs/README.md`：原有服务说明与接口示例
- `docs/用户说明手册.md`：中文用户向说明
- `docs/server_code_document.md`：后端代码说明

---

## 10. 许可证与使用说明

当前仓库未单独声明许可证文件。若用于教学机构或二次分发，建议在组织内补充 License、运维规范与数据合规说明。
