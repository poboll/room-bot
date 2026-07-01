# 交接文档

最后核对日期：2026-07-01

本文给下一次接手 Room Bot 的人使用。目标是不用依赖聊天上下文，也能快速判断这个仓库现在是什么状态、哪些事已经做完、哪些操作仍需人工确认。

## 1. 当前结论

Room Bot 已经整理成一个只收纳两个抢房脚本的公开归档仓库：

- `南山/`：深圳南山青年公寓抢房控制台。
- `青年驿站/`：深圳青年驿站预约脚本。

仓库远端是 `https://github.com/poboll/room-bot.git`。本轮核对时，本地 `main` 与 `origin/main` 在基线提交 `97dd5a9 docs: 完善抢房流程复盘文档` 上一致；如果本文档已经被提交，请以 `git log --oneline --decorate --max-count=5` 的当前输出为准。GitHub 上 `poboll/room-bot` 已是 Public，并已发布 `v1.1.0 技术沉淀文档增强版`。

仍未完成的一项历史要求：`poboll/nanshan-qiangfang-archive` 这个临时仓库仍存在，当前是 Private。删除 GitHub 仓库是不可逆远端操作，下次执行前应再次取得明确确认。

## 2. 已完成事项核对

### 2.1 空间与文件整理

当前仓库体积约 1.1 MB。已确认仓库内没有 `.venv`、`venv`、`__pycache__`、`dist`、`build`、日志、压缩包、sha256、PyInstaller 二进制等运行产物。

顶层只保留：

- `南山/`
- `青年驿站/`
- `docs/`
- `README.md`
- `LICENSE`
- `.gitignore`
- `private_history_backup_DO_NOT_PUBLISH/`

`private_history_backup_DO_NOT_PUBLISH/` 是本地私有旧历史 bundle，被根 `.gitignore` 忽略，不应提交、上传或放进 Release。

### 2.2 配置归档

真实配置保留在本地，但不进入 Git：

- `南山/config.json`
- `南山/JSON/*.json`
- `青年驿站/config.json`

公开仓库只跟踪：

- `南山/config.example.json`
- `青年驿站/config.example.json`

`南山/JSON/` 存放归档前从多端口副本收拢来的重复配置快照，符合“重复 config.json 移入 JSON 文件夹”的要求。该目录可能包含真实账号、Cookie、代理接口和个人资料，禁止提交。

### 2.3 文档体系

已有文档覆盖上次提出的几个主题：

- `docs/USER_GUIDE.md`：用户教程。
- `docs/FROM_ZERO_GUIDE.md`：从零开始教程。
- `docs/PACKET_CAPTURE_FLOW.md`：抓包流程与判断方法。
- `docs/PROJECT_INTENT.md`：我的想法，为什么做这个项目。
- `docs/CHALLENGES.md`：遇到的困难。
- `docs/SYSTEM_IMPLEMENTATION.md`：整个系统最后是怎么实现的。
- `docs/PROJECT_EVOLUTION.md`：项目全过程复盘。
- `docs/GIT_HISTORY_RECOVERY.md`：Git 历史清洗与恢复说明。
- `docs/RELEASE.md`：发布说明。
- `docs/HANDOFF.md`：本交接文档。

根 `README.md` 和两个子项目 README 都已经说明如何运行和技术原理。

### 2.4 仓库与发布

已完成：

- `origin` 指向 `poboll/room-bot`。
- `poboll/room-bot` 是 Public。
- `main` 本地和远端一致。
- GitHub Release `v1.1.0` 已发布。
- 公开历史是干净历史，避免把旧私有提交里的敏感信息公开。

待确认：

- 是否删除临时仓库 `poboll/nanshan-qiangfang-archive`。
- 是否因为新增本交接文档再发布一个补丁 Release。

## 3. 两个子项目速览

### 3.1 南山

入口文件：

- `南山/app.py`
- `南山/templates/index.html`
- `南山/scripts/run.sh`

运行方式：

```bash
cd /Users/Apple/Developer/art/qiangfang/南山
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
python3 app.py 5001
```

核心技术链路：

```text
Web 控制台
  -> 保存账号和运行配置
  -> 测试 Cookie
  -> 通过 /run_dual 建立 SSE 运行流
  -> 访问活动页提取或还原 CSRF
  -> 按账号、房源、节奏和代理配置并发提交表单
  -> 分类 JSON/HTML/WAF/登录页/限流/成功样式响应
```

关键模块：

- Flask 路由：`/api/config`、`/api/accounts`、`/api/test_cookie`、`/run_dual`、`/api/schedule`。
- 调度：APScheduler，支持定时任务。
- 运行流：SSE，把后端事件持续推给浏览器。
- 安全状态：`raw_cookie`、`PHPSESSID`、`_identity`、`_csrf`、`acw_tc`、`acw_sc__v3`。
- CSRF：从活动页 HTML/JS/meta/input/JSON 中提取，必要时从 `_csrf` Cookie 反序列化。
- 代理：支持代理 API、白名单 API、TTL、失败退避、按账号缓存。

### 3.2 青年驿站

入口文件：

- `青年驿站/run.py`
- `青年驿站/web_grab.py`

运行方式：

```bash
cd /Users/Apple/Developer/art/qiangfang/青年驿站
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
python3 web_grab.py
```

命令行版：

```bash
python3 run.py
```

核心技术链路：

```text
读取 config.json
  -> Authorization: Bearer <token>
  -> 遍历目标驿站
  -> POST /api/users/own/orders/check
  -> 预检成功后 POST /api/users/own/orders
  -> 统一分类 JSON、HTML、鉴权失败、网关错误和空响应
```

青年驿站比南山更轻，主要是 JWT + JSON API，没有活动页 CSRF 和 WAF Cookie 组合。

## 4. 敏感信息边界

下次接手时默认不要把这些内容复制到聊天、文档、Issue、PR 或 Release：

- `config.json`
- `南山/JSON/*.json`
- Cookie、JWT、CSRF、代理 API、白名单 API、Bark URL。
- 身份证号、手机号、真实姓名、学校/专业等个人资料。
- `private_history_backup_DO_NOT_PUBLISH/` 下的旧历史 bundle。

可以安全阅读和引用的文件：

- `config.example.json`
- `requirements.txt`
- `README.md`
- `docs/*.md`
- 源码中的脱敏默认值和结构性字段。

## 5. Conda 与环境清理状态

当前仓库内部没有虚拟环境或构建产物残留。

系统级 Miniconda 仍有多个环境和包缓存，2026-07-01 检查到的大致空间为：

- `/opt/homebrew/Caskroom/miniconda/base/pkgs`：约 2.3 GB。
- `OculiChatDA`：约 636 MB。
- `geo_env`：约 414 MB。
- `qaq`：约 1.1 GB。
- `uu`：约 1.4 GB。

这些环境不属于本仓库，不能仅凭 Room Bot 归档任务删除。若用户明确要清理，可以先执行安全级别较低的包缓存清理：

```bash
conda clean --all
```

删除具体环境前必须确认它不被其他项目使用，例如：

```bash
conda env remove -n uu
```

## 6. 下次接手的推荐顺序

1. 先运行 `git status --short --branch`，确认本地是否有未提交变更。
2. 运行 `git remote -v`，确认仍指向 `poboll/room-bot`。
3. 运行 `gh repo view poboll/room-bot --json visibility,latestRelease`，确认 Public 和 Release 状态。
4. 不读取真实配置内容；只用 `ls`、`file`、`du` 判断存在性和体积。
5. 若要恢复运行，按 `docs/USER_GUIDE.md`，先复制真实配置，再低频验证登录态和 CSRF/token。
6. 若要公开发布，先检查 `.gitignore` 和 `git ls-files`，确认没有真实配置进入 Git。
7. 若要删除 `poboll/nanshan-qiangfang-archive`，先取得明确确认，再执行远端删除。

## 7. 当前可复核命令

```bash
cd /Users/Apple/Developer/art/qiangfang
git status --short --branch
git log --oneline --decorate --all --max-count=5
git ls-files
du -sh . ./*
gh repo view poboll/room-bot --json visibility,latestRelease,url
gh repo view poboll/nanshan-qiangfang-archive --json visibility,url
```

预期结果：

- 工作区干净，或只有后续新增文档改动。
- `room-bot` 为 Public。
- `v1.1.0` Release 存在。
- `nanshan-qiangfang-archive` 如果还存在，应标记为待确认删除。
