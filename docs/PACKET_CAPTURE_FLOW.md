# 抓包流程与判断方法

这份文档记录当时是怎么从抓包一步步判断出接口流程的。核心方法是：先复现人工操作，再观察请求顺序，最后把请求拆成身份、参数、安全校验和响应判断四层。

## 1. 抓包前的假设

抢房类系统通常有两种形态：

1. 页面表单型：先访问活动页，再带 Cookie、Referer、CSRF 提交表单。
2. JSON API 型：用 token 鉴权，直接 POST JSON。

南山属于第一种，青年驿站属于第二种。判断它们属于哪一种，主要看请求头和请求体：

- 如果请求体是 `application/x-www-form-urlencoded`，又有 `_csrf` 字段，大概率是页面表单型。
- 如果请求头是 `Authorization: Bearer ...`，请求体是 JSON，大概率是 JSON API 型。

## 2. 南山抓包判断

### 2.1 先找关键请求

人工操作时重点看 Network 里这些请求：

- 活动页：`GET /activity/graduate`
- 身份接口：例如 `/customer-info/...`
- 提交接口：`POST /graduate-apply-check-in/add`

不要只看最后一个 POST。最后一个 POST 告诉我们“怎么提交”，但前面的 GET/身份接口告诉我们“为什么这个提交被允许”。

### 2.2 判断 Cookie 是否完整

抓到请求后，先看 `Cookie` 请求头。优先复制整段 Cookie，而不是逐个字段挑。

关键字段判断：

| 字段 | 用途 | 缺失时的现象 |
| --- | --- | --- |
| `PHPSESSID` | 服务端会话 | 常见跳登录、身份接口失败 |
| `_identity` | 用户身份态 | 页面可开但提交不认身份 |
| `_csrf` | CSRF 来源 | 表单提交缺 token |
| `acw_tc` | 阿里云安全 Cookie | 页面或接口不稳定 |
| `acw_sc__v3` | 滑块通过凭证 | 活动页返回 WAF/滑块页 |
| `gr_user_id` 等 | 统计/会话辅助 | 不一定必需，但完整保留更稳 |

脚本里的结论是：`raw_cookie` 优先。只要抓包能复制完整 Cookie，就不要拆散。

### 2.3 判断 CSRF 来源

南山提交接口需要 `_csrf`。抓包时有两种判断方式：

1. 看提交 POST 的 form data 是否带 `_csrf`。
2. 看活动页 HTML 或 Cookie 里是否能找到 `_csrf`。

实际项目里发现，页面不总是稳定暴露 token。有时 token 在 Cookie 里，是 Yii 序列化格式。脚本因此增加了 `extract_csrf_from_cookie()`，从 `_csrf` Cookie 中提取最后一个有效字符串作为表单 token。

### 2.4 判断 WAF/滑块

如果访问活动页返回的不是正常页面，而是包含 `aliyun_waf`、`nocaptcha`、`acw_sc__v3` 等标记，就不是普通接口失败，而是安全层拦截。

这时正确动作不是改 payload，而是：

1. 用真实浏览器打开活动页。
2. 人工通过滑块。
3. 重新抓 Cookie。
4. 确认 Cookie 里多了或更新了 `acw_sc__v3`。
5. 再运行脚本。

### 2.5 判断 payload

提交接口的 payload 可以拆成三类：

- 安全字段：`_csrf`
- 房源字段：活动 ID、项目 ID、房型、入住日期
- 用户字段：姓名、身份证、手机号、学校、专业、毕业日期、户籍类型、学信网验证码、意向企业、意向地区

判断 payload 是否正确的方法：

1. 用抓包里的人工提交请求作样本。
2. 把固定字段和账号字段分开。
3. 固定字段进入代码或默认配置。
4. 账号字段进入 `config.json`。
5. 每次新增账号只追加配置，不改提交逻辑。

## 3. 青年驿站抓包判断

青年驿站的链路更直：

```text
Authorization: Bearer <token>
POST /api/users/own/orders/check
POST /api/users/own/orders
```

### 3.1 先找鉴权

Network 里看到 `Authorization: Bearer ...`，说明主要登录态不是 Cookie，而是 JWT token。脚本只需要把 token 放到请求头。

### 3.2 先预检再下单

抓包能看到正式下单前有一个 check 接口。这个接口的意义是提前告诉你：

- 当前日期是否可申请。
- 当前驿站是否还有名额。
- 当前用户是否满足条件。
- 是否已有订单或不允许重复提交。

因此脚本最终不是直接狂打 order，而是先打 check。只有 check 返回成功样式文案时，才打正式 order。

### 3.3 响应分类

青年驿站正常返回 JSON，但异常时可能返回 HTML 网关页、登录页、空响应或非 JSON 文本。脚本因此统一用 `classify_response()`：

- JSON：读取 `message`。
- 空响应：标记错误。
- HTML：提示更像网关/登录页拦截。
- 401/403：提示重新获取 token。
- 429/5xx：交给 requests retry 做有限重试。

## 4. 如何从抓包走到代码

从抓包到代码不是一步完成，而是按这个顺序收敛：

1. 抓一次人工成功或接近成功的请求。
2. 复制 URL、method、headers、payload、response。
3. 把身份字段抽成配置。
4. 把房源字段抽成可选目标。
5. 把安全字段做自动提取或缓存。
6. 把响应判断写成分类函数。
7. 加日志，先低频验证。
8. 再加并发、定时、代理和调频。

真正重要的是第 6 步。没有响应分类，就会把 Cookie 过期、WAF 拦截、CSRF 缺失、代理失败、房源已满全部混成“失败”，后面越调越乱。

## 5. 抓包时的复盘清单

每次重新抓包，都按这张清单核对：

- URL 是不是目标接口。
- Method 是 GET 还是 POST。
- Content-Type 是 form 还是 JSON。
- Cookie 或 Authorization 是否完整。
- Referer 和 User-Agent 是否接近真实环境。
- POST payload 有没有 `_csrf`。
- 日期和房源 ID 有没有填对。
- 返回是 JSON 还是 HTML。
- HTML 是否是登录页、WAF 页或网关错误页。
- 成功文案是否发生变化。

如果平台改版，先更新这张清单里的事实，再改代码。
