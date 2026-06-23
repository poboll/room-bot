# 青年驿站抢房脚本归档

青年驿站项目是面向 `api-home.szyouth.cn` 的预约自动化脚本。它保留两个入口：

- `run.py`：后台循环版，适合终端运行。
- `web_grab.py`：Bottle Web 控制台，适合可视化选择驿站、修改配置和观察日志。

真实 token 和个人资料读取自本地 `config.json`，公开仓库只提交 `config.example.json`。

## 如何运行

```bash
cd /Users/Apple/Developer/art/qiangfang/青年驿站
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
python3 web_grab.py
```

打开：

```text
http://127.0.0.1:5011/
```

命令行版：

```bash
python3 run.py
```

## 配置

`config.json` 字段包括：

- `token`：青年驿站 API JWT。
- `name`、`phone`、`gender`：下单接口需要的基础用户信息。
- `id_card`、`school`、`major`、`xuexin_code`、`graduation_date`、`is_shenzhen`：Web 控制台保存的复盘资料。
- `come_date`、`leave_date`：固定申请区间。

命令行版也支持环境变量覆盖：

```bash
YOUTH_CONFIG=/path/to/config.json python3 run.py
YOUTH_TOKEN='...' YOUTH_NAME='张三' YOUTH_PHONE='13800000000' python3 run.py
```

## 技术原理

青年驿站链路是典型 JSON API：

```text
Authorization: Bearer <token>
  -> /api/users/own/orders/check 预检
  -> /api/users/own/orders 下单
```

脚本会按通勤时间排序遍历候选驿站。每个驿站先走预检接口，只有预检返回成功样式文案时才发正式下单请求。响应统一进入 `classify_response()`，用来区分 JSON 业务响应、HTML 网关/登录页、鉴权失败和空响应。

网络层使用 `requests.Session` 和 `urllib3.Retry`，对 429、5xx 和连接波动做有限重试。这样能缓解短时网络问题，但不会无限重试。

## 归档说明

归档时删除了本地 `venv`、日志和 `__pycache__`。这些文件都可以重新生成，不属于源码资产。真实 `config.json` 被 `.gitignore` 忽略，不随公开仓库发布。
