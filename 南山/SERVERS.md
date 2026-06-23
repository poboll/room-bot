# 南山部署备忘

本文只保留部署结构和恢复方法，不保存服务器密码、代理平台密钥、通知密钥或可复用登录凭据。真实线上信息应存放在密码管理器或私有运维记录中。

## 本地项目

- 项目目录：`/Users/Apple/Developer/art/qiangfang/南山`
- 源码入口：`app.py`
- 前端模板：`templates/index.html`
- 主配置：`config.json`，本地保留，不提交公开仓库。
- 样例配置：`config.example.json`
- 启动脚本：`scripts/run.sh`

## 推荐部署形态

一份源码可以服务多个端口，但不要复制整套代码。推荐结构：

```text
/opt/room-bot/nanshan/
├── app.py
├── templates/
├── requirements.txt
├── configs/
│   ├── config.5001.json
│   ├── config.5002.json
│   └── config.5003.json
└── venv/
```

每个进程启动前把对应配置链接或复制为运行目录下的 `config.json`，端口通过命令行参数传入。

## 手工启动

```bash
cd /opt/room-bot/nanshan
cp configs/config.5001.json config.json
nohup ./venv/bin/python app.py 5001 >/tmp/qiangfang-5001.log 2>&1 &
```

## systemd 思路

可以为每个端口建立一个 service，核心命令保持一致：

```bash
ExecStart=/opt/room-bot/nanshan/venv/bin/python /opt/room-bot/nanshan/app.py 5001
WorkingDirectory=/opt/room-bot/nanshan
```

配置切换建议通过独立目录或启动前脚本完成，避免在同一目录里同时运行多个进程争抢同一个 `config.json`。

## 检查命令

```bash
ss -ltnp | grep -E ':500[1-9]\b'
curl -I --max-time 10 http://127.0.0.1:5001/
tail -f /tmp/qiangfang-5001.log
```

## 历史经验

- 多端口目录副本曾经让补丁分散，归档后已收敛为一份源码。
- 线上恢复前必须重新确认 Cookie、代理白名单、服务器时间和目标房源开放时间。
- 日志不要长期保留真实 Cookie、身份证号或代理 API 响应。
