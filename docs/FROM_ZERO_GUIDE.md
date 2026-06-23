# 从零开始教程

本文按“完全重新来一遍”的方式写，不假设读者知道抢房脚本、抓包、Cookie、CSRF 或接口调用。目标不是鼓励高频请求，而是把这个项目的技术链路拆开，方便以后复盘：当时为什么这么做，哪些信息必须抓，哪些失败现象分别代表什么。

## 1. 先理解人工流程

在写脚本前，先把人工流程走通：

1. 打开目标小程序或网页活动页。
2. 登录账号。
3. 如果出现滑块或安全验证，先人工通过。
4. 进入活动页，看能否看到房源、个人信息或申请入口。
5. 到开放时间前后，点击申请或提交。
6. 观察页面是成功、已满、审核中、跳登录页，还是直接报错。

脚本要自动化的不是“神秘操作”，而是把上面这些机械步骤拆成 HTTP 请求：身份状态、页面访问、表单字段、提交接口、结果判断。

## 2. 准备本地环境

两个项目都用 Python，建议用虚拟环境隔离依赖：

```bash
cd /Users/Apple/Developer/art/qiangfang
python3 --version
```

南山：

```bash
cd /Users/Apple/Developer/art/qiangfang/南山
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
python3 app.py 5001
```

青年驿站：

```bash
cd /Users/Apple/Developer/art/qiangfang/青年驿站
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
python3 web_grab.py
```

真实 `config.json` 只放本机，不提交 Git。

## 3. 从零判断南山接口

南山不是纯 JSON API，它先经过活动页，再提交表单。因此判断顺序是：

1. 活动页是否能访问：`https://sz.inboyu.com/activity/graduate`
2. 登录态是否有效：Cookie 里是否有 `PHPSESSID`、`_identity` 等身份字段。
3. 安全校验是否通过：是否有 `_csrf`，遇到阿里云 WAF 时是否有 `acw_sc__v3`。
4. 提交接口在哪里：最终提交落到 `graduate-apply-check-in/add`。
5. 表单字段是什么：个人资料、房源项目、入住日期、意向企业、意向地区和 `_csrf`。
6. 响应怎么判断：JSON 成功码、审核中文案、HTML 登录页、WAF 页面、限流或代理异常。

### 3.1 为什么先抓活动页

活动页提供三类信息：

- 当前登录态是否还能打开活动页。
- 页面里是否暴露 `_csrf`。
- 被拦截时返回的 HTML 是否带 WAF 或登录页特征。

如果活动页都打不开，直接提交接口通常只是在浪费请求。脚本里对应的是先通过 `get_csrf_from_page()` 请求活动页，再把 token 放到提交表单。

### 3.2 为什么 Cookie 要尽量完整

一开始容易误以为只要 `PHPSESSID` 就够了。实际不是这样：

- `PHPSESSID`：服务端会话。
- `_identity`：身份态，缺了可能页面能访问但接口不认。
- `_csrf`：Yii 风格 CSRF Cookie，里面序列化了真实 token。
- `acw_tc`：阿里云流量/安全相关 Cookie。
- `acw_sc__v3`：滑块通过后的 WAF 凭证。
- 统计和会话 Cookie：不一定每次都必需，但完整保留更接近真实浏览器环境。

所以配置里优先保存 `raw_cookie`，脚本按原样传给服务端。只有没有完整 Cookie 时，才退回到拆分字段拼装。

### 3.3 如何判断 CSRF

提交接口要求表单 `_csrf`。token 可能来自：

- 活动页里的 JS 变量。
- `<meta name="csrf-token">`。
- 隐藏 input。
- JSON 片段。
- `_csrf` Cookie 反序列化后的值。

因此不能只写一个正则。脚本现在的策略是：

1. 先查缓存，避免高峰期每次都访问活动页。
2. 请求活动页，从 HTML/JS/JSON 多模式提取。
3. 页面没有暴露 token 时，从 `_csrf` Cookie 中还原。
4. 页面请求失败但 Cookie 里有 token 时，短时间回退使用 Cookie token。
5. 如果识别到 WAF 页面，提示重新过滑块并复制 `acw_sc__v3`。

## 4. 从零判断青年驿站接口

青年驿站更像标准 JSON API：

```text
Authorization: Bearer <token>
POST /api/users/own/orders/check
POST /api/users/own/orders
```

判断方式：

1. 抓请求头，看是否是 `Authorization: Bearer ...`。
2. 找到预检接口：`/api/users/own/orders/check`。
3. 找到下单接口：`/api/users/own/orders`。
4. 看请求体字段：入住日期、离店日期、驿站 ID、姓名、手机号、性别。
5. 看响应字段：通常是 JSON 里的 `message`。
6. 先预检，预检成功再正式下单。

这也是为什么 `青年驿站/run.py` 和 `青年驿站/web_grab.py` 都先调用 check，再调用 order。

## 5. 配置怎么填

南山账号的最小结构：

```json
{
  "id": "example-account",
  "name": "示例账号",
  "enabled": true,
  "cookies": {
    "raw_cookie": "PHPSESSID=xxx; _csrf=xxx; _identity=xxx; acw_tc=xxx; acw_sc__v3=xxx"
  },
  "user": {
    "user_name": "张三",
    "card_num": "440300199001011234",
    "phone": "13800000000",
    "school": "示例大学",
    "major": "示例专业",
    "graduation_time": "2026-06-30",
    "household_type": "非深户籍",
    "checkin_date": "2026-04-29",
    "xuexin_code": "XXXXXXXXXXXXXXX",
    "enterprise": "示例企业",
    "district": "南山区"
  }
}
```

青年驿站配置的最小结构：

```json
{
  "token": "YOUR_SZYOUTH_JWT",
  "name": "张三",
  "phone": "13800000000",
  "gender": 2000,
  "come_date": "2026-04-04",
  "leave_date": "2026-04-18"
}
```

## 6. 第一次运行只做验证

不要一上来就开高频。先做这些验证：

1. 启动服务。
2. 打开 Web 控制台。
3. 保存配置。
4. 用“测试 Cookie”或低频手动运行看登录态。
5. 确认日志里能拿到 CSRF。
6. 确认返回不是 HTML 登录页、WAF 页或代理错误页。
7. 再开启定时或高峰调频。

第一次验证的目标不是成功提交，而是证明“请求有效”。只有有效请求多了，开放窗口里的提交才有意义。

## 7. 运行时怎么读日志

常见日志判断：

- `Cookie缺少PHPSESSID`：Cookie 不完整，重新抓。
- `Cookie已过期`：登录态失效，重新登录。
- `CSRF 获取失败`：活动页没拿到 token，检查 `_csrf` Cookie 和 WAF。
- `页面被 WAF 劫持`：重新过滑块，补 `acw_sc__v3`。
- `返回HTML页面`：可能是登录页、网关页或安全拦截页。
- `429/405/502/503`：可恢复状态码，脚本会尝试刷新代理或等待下一轮。
- `审批中/审核中/已提交报名`：成功样式响应。

## 8. 从零恢复到可复盘状态

完整恢复顺序：

1. 拉取公开仓库。
2. 建 Python 虚拟环境。
3. 安装依赖。
4. 复制本地真实配置到 `config.json`。
5. 重新抓 Cookie/token。
6. 低频验证登录态和 CSRF。
7. 配置房源、账号、节奏。
8. 开放前启动服务，观察 keepalive 和 warmup 日志。
9. 开放后观察 peak 阶段结果。
10. 结束后保存响应样本和日志摘要，方便下一次复盘。

归档版本保留的是工程骨架和技术路径。真实运行前，最重要的是刷新登录态和确认平台规则。
