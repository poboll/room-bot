# 用户教程

本文面向后续恢复运行的人，目标是让项目从归档状态回到可本地验证、可手动触发、可观察日志的状态。

## 1. 环境准备

两个子项目都使用 Python。建议使用本地虚拟环境，不建议把依赖装进 Conda base。

```bash
cd /Users/Apple/Developer/art/qiangfang
python3 --version
```

推荐 Python 3.10 及以上。若必须使用旧环境，南山项目里已经兼容 Python 3.7 的 `ThreadPoolExecutor.shutdown(cancel_futures=...)` 差异。

## 2. 南山项目运行

```bash
cd /Users/Apple/Developer/art/qiangfang/南山
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
python3 app.py 5001
```

浏览器打开：

```text
http://127.0.0.1:5001/
```

### 2.1 配置账号

在 Web 页面中新增账号，填写：

- 账号名称和启用状态。
- Cookie 信息：`raw_cookie` 最完整；拆分字段至少要有 `PHPSESSID`、`_csrf`、`_identity`、`acw_tc`，遇到 WAF 时需要 `acw_sc__v3`。
- 个人信息：姓名、身份证号、手机号、毕业院校、专业、毕业时间、户籍类型、学信网验证码。
- 申请信息：入住日期、意向企业、意向地区。

Cookie 建议在活动开始前较短时间重新抓取。若页面提示登录态失效、CSRF 缺失或 WAF 拦截，优先重新过滑块并复制完整 Cookie。

### 2.2 手动运行

1. 勾选要启用的账号。
2. 勾选目标房源。
3. 设置运行间隔、最大次数、成功停止阈值。
4. 点击开始，观察 SSE 日志。

日志里重点看四类信息：

- 登录态：是否跳转登录页、是否缺少 `PHPSESSID`。
- CSRF：是否从页面或 Cookie 中拿到 `_csrf`。
- 代理：是否白名单成功、代理是否鉴权失败。
- 提交结果：是否出现审核中、审批中、已提交等成功样式响应。

### 2.3 定时运行

南山项目支持 APScheduler 定时任务。常见恢复方式是提前启动服务，再在 Web 页面中保存定时任务。系统会按开放窗口做节奏切换：

- keepalive：低频保活。
- warmup：开放前预热。
- peak：开放后高频提交。
- cooldown：峰值后观察和降频。

## 3. 青年驿站项目运行

```bash
cd /Users/Apple/Developer/art/qiangfang/青年驿站
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
python3 web_grab.py
```

浏览器打开：

```text
http://127.0.0.1:5011/
```

### 3.1 Web 控制台

页面中可以编辑 token、个人资料、入住/离店日期、轮询间隔和目标驿站。保存后会写入本地 `config.json`。

### 3.2 命令行循环

```bash
python3 run.py
```

命令行版读取同一个 `config.json`，也支持环境变量覆盖：

```bash
YOUTH_TOKEN='...' YOUTH_NAME='张三' YOUTH_PHONE='13800000000' python3 run.py
```

## 4. 配置备份与恢复

公开仓库只提交 `config.example.json`。真实配置请保存在：

- `南山/config.json`
- `南山/JSON/*.json`
- `青年驿站/config.json`

这些路径已经被 `.gitignore` 忽略。迁移到新机器时，只需要复制真实配置文件到对应目录，然后安装依赖启动即可。

## 5. 常见问题

`401/403`：通常是 token、Cookie 或登录态过期。

返回 HTML：通常不是业务接口返回，可能是登录页、网关页、WAF 滑块页或代理异常页。

CSRF 获取失败：先检查完整 Cookie 中是否有 `_csrf`，再检查活动页是否被 WAF 拦截。

提交很多但没有结果：不要只提高频率，先确认有效请求比例。无效请求越多，越容易浪费窗口期。
