# 南山抢房脚本归档

> 本目录是深圳南山青年公寓场景的抢房脚本归档版。公开仓库只保留脱敏源码、样例配置和复盘文档；真实配置备份只留在本机被 `.gitignore` 忽略的位置，目标是让后续复盘、恢复和二次开发都有一个干净入口。

## 当前状态

- 核心入口：`app.py`
- Web 控制台：`templates/index.html`
- 本地主配置：`config.json`，不提交公开仓库
- 本地历史配置快照：`JSON/`，不提交公开仓库
- 启动脚本：`scripts/run.sh`
- 依赖声明：`requirements.txt`
- 服务器拓扑：`SERVERS.md`

归档时已删除多端口 `dist_50xx` 代码副本、PyInstaller 二进制、压缩包、日志和 `__pycache__`。这些文件都属于可再生成或历史运行产物，不再作为源码保存。

## 如何运行

### 1. 创建环境

```bash
cd /Users/Apple/Developer/art/qiangfang/南山
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置可选环境变量

`config.json` 中如果已经包含代理 API 与白名单 API，可以直接使用。归档版的代码默认值已脱敏；新建空配置或希望用环境变量注入时，可设置：

```bash
export QIANGFANG_PROXY_API_URL='https://proxy.example.com/get'
export QIANGFANG_PROXY_WHITELIST_URL='https://proxy.example.com/white/add?ip=xxx'
export QIANGFANG_BARK_URL='https://api.day.app/<token>'
```

如遇本机 CA 证书问题，可通过启动脚本注入：

```bash
export QIANGFANG_CA_BUNDLE='/path/to/cacert.pem'
```

### 3. 启动服务

```bash
./scripts/run.sh 5001
```

或直接运行：

```bash
python3 app.py 5001
```

浏览器打开：

```text
http://127.0.0.1:5001/
```

### 4. 使用流程

1. 在小程序中进入活动页，完成登录和滑块验证。
2. 抓包复制账号 Cookie，重点确认 `PHPSESSID`、`_csrf`、`acw_tc`，遇到阿里云 WAF 时还需要 `acw_sc__v3`。
3. 在控制台中新增或编辑账号，填写姓名、身份证、手机号、学校、专业、毕业时间、户籍类型、学信网验证码、意向企业与意向地区。
4. 勾选账号和房源，可手动开始，也可开启定时任务。
5. 运行时观察日志面板：成功样式响应、登录态失效、CSRF 缺失、代理异常都会以事件流形式返回。

## 技术原理

### 1. 总体链路

系统本质上是一个本地 Web 控制台加抢购执行器：

```text
浏览器控制台
  -> /api/config, /api/accounts 保存运行参数和账号配置
  -> /api/test_cookie 验证登录态与活动页可访问性
  -> /run_dual 建立 SSE 长连接
  -> app.py 并发执行提交
  -> sz.inboyu.com/activity/graduate 获取活动页与 CSRF
  -> sz.inboyu.com/graduate-apply-check-in/add 提交入住申请
```

前端只负责配置、选择账号、选择房源、发起任务和展示事件。真正的请求编排都在 `app.py`。

### 2. Cookie 与 CSRF

提交接口需要两层状态：

- Cookie：`PHPSESSID` 代表服务端会话，`_identity` 代表身份状态，`acw_tc` 和 `acw_sc__v3` 用来穿过 WAF 或滑块校验。
- CSRF：提交表单时必须带 `_csrf` 字段。脚本会先访问活动页，尝试从页面里的 meta、input、JS 变量或 JSON 片段提取 token；如果页面没有暴露 token，则从 `_csrf` Cookie 中反序列化出真实值。

为了减少高峰期请求浪费，CSRF 会按账号和活动 ID 缓存。新 token 获取失败时，如果配置允许，会短时间回退到旧缓存，避免因为瞬时代理或页面波动直接放弃提交。

### 3. 表单提交模型

每次提交会组装 `application/x-www-form-urlencoded` 表单，关键字段包括：

- 个人信息：姓名、身份证、手机号、户籍类型、学校、专业、毕业日期、学信网验证码。
- 申请信息：活动 ID、房源项目 ID、房型、入住日期、意向企业、意向地区。
- 安全字段：请求头 Cookie、Referer、User-Agent、表单 `_csrf`。

提交响应优先按 JSON 解析。`errcode == 0` 或返回文案包含“审批中”“审核中”“已提交报名”时，会标记为成功样式响应。若返回 HTML，则会判断是否跳转登录页、是否 Cookie 过期、是否接口结构变化。

### 4. 并发与节奏控制

脚本不是简单死循环，而是按账号维护状态机：

- 每个账号有自己的轮次、下一次执行时间、成功计数、停止原因。
- 每个账号可以同时对多个房源发起提交。
- 全局线程池控制总并发，`per_account_max_inflight` 控制单账号在途请求。
- 遇到 405、429、502、503 等可恢复状态码，会刷新代理和 CSRF，并按当前阶段决定是否立即重试。

手动模式和定时模式共享同一套执行器。区别在于手动模式通过 `/run_dual` 把事件实时推给浏览器，定时模式由 APScheduler 后台触发并写日志。

### 5. 定时调频

南山两个重点房源历史上分别按固定时间开放：

- 西丽湖国际科教城平山公寓：每日 `12:15`
- 南头古城青年驿站：每日 `14:00`

定时模式会提前触发，不是卡点才启动。运行节奏分为：

- keepalive：低频保活，尽量维持会话和代理状态。
- warmup：开放前预热，提高访问频率，提前刷新 CSRF。
- peak：开放后高频提交。
- cooldown：峰值后降频观察，避免过早停掉。

这套曲线通过 `keepalive_interval_seconds`、`warmup_interval_seconds`、`peak_interval_seconds`、`cooldown_interval_seconds`、`warmup_before_seconds`、`peak_after_seconds`、`cooldown_after_seconds` 控制。

### 6. 代理与白名单

代理模块分三层：

1. 检测本机公网 IP。
2. 调用代理平台白名单接口，把当前出口 IP 加白。
3. 调用代理提取接口，为账号拿到短时 HTTP 代理。

代理按账号缓存，并设置 TTL、最小提取间隔和失败退避。这样做是为了避免高峰期每次提交都去请求代理平台，同时又能在代理失效、鉴权失败、限流或连接异常时快速切换。

### 7. 停止条件

任务会在以下情况收束：

- 用户点击停止。
- 某账号 Cookie 明确过期，该账号停止后续请求。
- 成功样式响应累计达到阈值。
- 手动任务被新的手动任务替换。
- 定时任务被停止或重建。

默认策略是首次成功达到阈值后尽快停止，避免重复提交和无意义消耗。

## 配置归档

`config.json` 是当前本地主配置。它可能包含真实账号资料、Cookie、代理 API、通知地址和申请信息，因此只在本机保留，不提交公开仓库。

`JSON/` 中保存归档前的配置快照，同样只在本机保留：

- `config.main-20-accounts.before-archive.json`：原 `南山/config.json`。
- `config.dist_*.json`：原多端口目录中的配置副本。
- `config.release_build.json`：原 release 构建目录配置。
- `remote_8.218_5001_backup_before_sync.json`：远端同步前备份。

这些 JSON 可能包含真实 Cookie、个人信息、代理 API 地址和申请资料。公开发布时不会进入 Git 历史、GitHub Release 或源码压缩包；迁移到新机器时需要另行通过安全渠道复制。

## 项目反思

这个脚本的有效性来自三件事：会话状态足够真实、提交节奏足够贴近开放窗口、失败恢复足够快。前期把 Cookie、CSRF、WAF、代理、并发全部揉在一起，确实能跑起来，但也带来了明显的维护成本。

主要经验：

- 配置和代码必须分离。真实 Cookie 与个人信息只能待在配置里，不应进入默认代码或文档。
- 多端口部署不要复制整套代码。应该一份代码，多份配置，多端口由启动参数或进程管理器承担。
- 抢购类脚本最容易“越调越复杂”。真正核心的指标不是请求次数，而是有效请求比例：登录态是否新鲜、CSRF 是否可用、代理是否可用、提交时间是否在窗口内。
- 高峰期失败往往不是单一原因。需要把 Cookie 过期、WAF 拦截、CSRF 缺失、代理鉴权、接口限流、HTML 跳转登录页分开观察，不能只写一个“请求失败”。
- 归档版本要优先可恢复、可解释、可审计。二进制和日志可以删除，配置快照和流程文档必须留下。

## 安全边界

- 非配置文件中的代理 API、通知 URL、服务器密码已脱敏。
- `config.json` 与 `JSON/` 保留在本机，不进入公开仓库。
- 发布 Public 仓库时需要使用干净历史，不能把旧私有提交直接公开。
- 恢复线上运行前，应重新抓取 Cookie，并确认目标平台规则、账号权限和使用风险。
