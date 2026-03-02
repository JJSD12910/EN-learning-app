📘 Git 日常使用指南（前端项目）

本文件用于记录本项目的 Git 日常操作流程，避免遗忘步骤。

🚀 一、每日开发标准流程（推荐顺序）
1️⃣ 开发前：先同步远程代码
git pull

确保本地代码是最新的，避免冲突。

2️⃣ 安装依赖（如有更新）
npm install

如果远程更新了依赖文件（package.json / package-lock.json），必须执行。

3️⃣ 开发完成后，检查是否能构建
npm run build

确保项目可以正常构建再提交。

4️⃣ 提交并推送
git add .
git commit -m "feat: 简要描述本次修改"
git push
📝 二、提交信息规范（推荐）

建议使用以下格式：

feat: 新增功能
fix: 修复问题
style: 样式调整
refactor: 重构代码
docs: 修改文档
chore: 其他杂项修改

示例：

git commit -m "feat: 添加登录功能"
git commit -m "fix: 修复按钮点击失效问题"
🔎 三、常用检查命令
查看当前状态
git status
查看最近提交记录
git log --oneline
⚠️ 四、常见问题处理
1️⃣ push 被拒绝（远程有更新）
git pull
git push
2️⃣ 出现合并冲突

打开冲突文件，找到：

<<<<<<< HEAD
本地代码
=======
远程代码
>>>>>>> main

手动修改为正确内容后：

git add .
git commit -m "resolve conflict"
3️⃣ 网络错误（Connection reset）

确保代理已开启（端口 7890）。

如有需要：

git config --global http.proxy http://127.0.0.1:7890
git config --global https.proxy http://127.0.0.1:7890
📦 五、推荐 .gitignore（前端项目）

确保以下文件不要提交：

node_modules/
dist/
.env
.env.local
🎯 六、开发口诀

先 pull，再改；能 build，再 push。

📌 七、完整标准流程总结
git pull
npm install
npm run build
git add .
git commit -m "feat: xxx"
git push