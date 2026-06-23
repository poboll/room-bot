# Room Bot

Room Bot 是一次抢房自动化项目的公开归档仓库，只收纳两个与房源预约相关的子项目：

- `南山/`：深圳南山青年公寓抢房控制台，核心是 Flask Web 控制台、多账号配置、Cookie/CSRF 处理、代理与调频执行器。
- `青年驿站/`：深圳青年驿站预约脚本，包含命令行循环版和 Bottle Web 控制台版。

本仓库不收纳博客、站点改写、图片素材、实验脚本等无关内容。公开版本只保留脱敏源码、样例配置和复盘文档；真实账号、Cookie、JWT、代理接口、通知密钥等只应存在于本地被 `.gitignore` 忽略的 `config.json` 中，不进入 Git 历史、GitHub Release 或源码压缩包。

## 快速开始

### 南山青年公寓

```bash
cd 南山
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
python3 app.py 5001
```

打开 `http://127.0.0.1:5001/`，在页面中录入账号 Cookie、个人资料、房源和定时参数。

### 青年驿站

```bash
cd 青年驿站
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
python3 web_grab.py
```

打开 `http://127.0.0.1:5011/` 使用 Web 控制台。也可以运行后台循环版：

```bash
python3 run.py
```

## 文档索引

- [用户教程](docs/USER_GUIDE.md)
- [从零开始教程](docs/FROM_ZERO_GUIDE.md)
- [抓包流程与判断方法](docs/PACKET_CAPTURE_FLOW.md)
- [我的想法：为什么做这个项目](docs/PROJECT_INTENT.md)
- [遇到的困难](docs/CHALLENGES.md)
- [系统最终如何实现](docs/SYSTEM_IMPLEMENTATION.md)
- [项目全过程复盘](docs/PROJECT_EVOLUTION.md)
- [Git 历史清洗与恢复说明](docs/GIT_HISTORY_RECOVERY.md)
- [南山子项目说明](南山/README.md)
- [南山部署备忘](南山/SERVERS.md)
- [青年驿站子项目说明](青年驿站/README.md)
- [发布说明](docs/RELEASE.md)

## 归档原则

1. 只保留可解释、可恢复、可复盘的源码与文档。
2. 删除二进制、虚拟环境、日志、缓存、重复代码副本等可再生成产物。
3. 公开仓库不包含真实凭据；本地真实配置不提交。
4. 多端口部署不再复制整份代码，统一用一份源码加多份配置恢复。
5. README 和 docs 负责解释“如何运行、为什么做、难点在哪里、系统怎样工作”。

## 安全说明

抢房自动化涉及第三方平台登录态、个人身份信息、预约权益和接口规则。本仓库仅用于个人学习和复盘。运行前请确认目标平台规则、账号权限和自动化风险，不要滥用请求频率，不要公开真实配置。

本次公开归档采用干净历史发布：旧的私有开发历史不随 Public 仓库公开，避免历史提交中的临时 token、代理参数或测试配置被二次暴露。
