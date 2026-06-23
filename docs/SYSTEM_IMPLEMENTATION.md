# 整个系统最后是怎么实现的

Room Bot 最终不是一个单一脚本，而是两个面向不同平台的执行器。它们共享同一种工程思路：配置驱动、状态拆分、响应分类、失败可观察。

## 1. 总体结构

```text
room-bot
├── 南山
│   ├── app.py
│   ├── templates/index.html
│   ├── requirements.txt
│   ├── config.example.json
│   └── scripts/
├── 青年驿站
│   ├── run.py
│   ├── web_grab.py
│   ├── requirements.txt
│   └── config.example.json
└── docs/
```

公开仓库只保留样例配置。真实配置文件通过 `.gitignore` 留在本机。

## 2. 南山执行链路

南山项目是 Flask 应用，后端入口是 `app.py`，前端模板是 `templates/index.html`。

核心链路：

```text
Web 控制台
  -> /api/config 读写全局配置
  -> /api/accounts 管理账号
  -> /api/test_cookie 验证登录态
  -> /run_dual 建立 SSE 运行流
  -> iter_booking_events 调度账号和房源
  -> execute_booking_attempt 单次提交
  -> 活动页获取 CSRF
  -> 提交 graduate-apply-check-in/add
```

### 2.1 配置层

`build_default_config()` 定义默认参数，`ensure_config_defaults()` 做类型修正和边界限制。配置包括：

- 基础节奏：`interval`、`max_count`、`start_delay`。
- 调频窗口：keepalive、warmup、peak、cooldown。
- 并发控制：`max_workers`、`per_account_max_inflight`。
- 代理控制：代理接口、白名单接口、TTL、失败退避。
- CSRF 控制：缓存秒数、旧 token 回退。
- 账号列表：Cookie、个人信息、申请信息。

### 2.2 Cookie 与 CSRF

`build_cookie_str()` 负责拼 Cookie。它优先使用 `raw_cookie`，并在必要时补充 `acw_sc__v3`。如果没有完整 Cookie，则从拆分字段按白名单拼装。

`get_csrf_from_page()` 负责拿 `_csrf`。它先查缓存，再请求活动页，从 HTML/JS/JSON 中抽取 token；页面没有 token 时，会从 `_csrf` Cookie 的序列化字符串里还原真实值。遇到 WAF 页面，会在日志中明确提示需要重新过滑块。

### 2.3 代理处理

代理模块分成三步：

1. `get_public_ip()` 检测当前出口 IP。
2. `refresh_proxy_whitelist()` 调代理平台白名单接口。
3. `fetch_proxy_from_api()` 拉取短时代理。

代理按账号缓存，并设置最小拉取间隔、TTL 和失败退避。这样既减少代理平台请求，也能在代理鉴权失败时快速刷新。

### 2.4 提交执行器

`iter_booking_events()` 是运行主循环。它维护账号状态、在途请求、下一次运行时间和停止条件。真正提交由 `execute_booking_attempt()` 完成。

一次提交包括：

1. 解析运行时参数。
2. 获取代理。
3. 获取 CSRF。
4. 组装表单 payload。
5. 发送 POST。
6. 分类响应。
7. 根据结果决定成功、重试、停止或继续。

成功判断不是只看一个字段，而是结合 JSON 响应、业务文案和 HTML 跳转形态。这样做是为了适配接口在高峰期可能出现的不稳定响应。

### 2.5 前端控制台

前端是单页 HTML，承担账号编辑、配置保存、房源选择、手动运行、定时任务和日志展示。运行日志通过 SSE 返回，浏览器端按事件更新统计和日志面板。

## 3. 青年驿站执行链路

青年驿站项目有两个入口：

- `run.py`：命令行后台循环。
- `web_grab.py`：Bottle Web 控制台。

它们使用同一类业务链路：

```text
读取 config.json
  -> 构造 Authorization: Bearer <token>
  -> 遍历目标驿站
  -> POST /api/users/own/orders/check
  -> 预检成功后 POST /api/users/own/orders
  -> 根据 message 判断成功、已满或失败
```

青年驿站比南山简单，因为它是 JSON API，没有页面 CSRF 提取和 WAF Cookie 拼装。但它同样需要处理 token 过期、HTML 网关响应、网络超时和重试。

## 4. 为什么这样设计

最终实现有几个明确取向：

- 配置驱动：账号和时间参数不写死在主流程里。
- 失败分层：认证、CSRF、WAF、代理、接口限流、业务失败分别识别。
- 控制台可观察：日志能解释状态，而不是只有成功/失败。
- 并发有边界：不靠无限线程赌概率。
- 归档可恢复：删除二进制和虚拟环境，保留样例配置和文档。

## 5. 后续可改进方向

如果以后重新启用，优先改进这些点：

1. 把南山账号配置迁移到本地 SQLite 或加密文件，避免手写大型 JSON。
2. 给青年驿站也增加独立的配置校验和脱敏日志。
3. 把成功/失败响应样本保存为可测试 fixture。
4. 增加 dry-run 模式，只验证 Cookie、CSRF、代理和 payload，不真正提交。
5. 用统一的 `scripts/doctor.sh` 检查环境、配置和端口占用。
