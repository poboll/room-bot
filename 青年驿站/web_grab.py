#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""驿站抢房系统 - 完整版"""

from bottle import route, run, response, request
import threading
import time
import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, RequestException, Timeout
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any
from urllib3.util.retry import Retry

# 全局配置（可通过 API 修改，真实配置存放在被 git 忽略的 config.json 中）
DEFAULT_CONFIG: dict[str, Any] = {
    "token": "",
    "name": "",
    "phone": "",
    "gender": 2000,
    "id_card": "",
    "school": "",
    "major": "",
    "xuexin_code": "",
    "graduation_date": "",
    "is_shenzhen": False,
    "come_date": "2026-04-04",
    "leave_date": "2026-04-18",
}
CONFIG_PATH = Path(os.environ.get("YOUTH_CONFIG", Path(__file__).with_name("config.json")))


def load_config() -> dict[str, Any]:
    loaded = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            loaded.update(json.load(f))
    return loaded


def save_config() -> None:
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


config: dict[str, Any] = load_config()

HOSTELS = [
    (117, "坂田岗头站", "35分钟"),
    (87, "龙岗布吉站", "32分钟"),
    (84, "坂田雪象站", "37分钟"),
    (79, "坂田星河WORLD站", "39分钟"),
    (75, "宝安西乡站", "54分钟"),
    (86, "龙岗平湖站", "55分钟"),
    (89, "南湾华润站", "55分钟"),
    (88, "吉华甘坑站", "60分钟"),
    (76, "南山南头古城站", "1小时3分钟"),
    (90, "坂田大运AI小镇站", "1小时30分钟"),
    (85, "龙城CC公寓站", "1小时33分钟"),
]

grabber_state: dict[str, Any] = {
    "running": False,
    "attempt": 0,
    "logs": [],
    "success": False,
    "success_hostel": None,
    "last_update": None,
    "selected_hostels": [h[0] for h in HOSTELS],
    "hostel_status": {},
    "available_hostels": [],
    "interval": 3,
    "last_request": None,
    "last_response": None,
}

REQUEST_TIMEOUT = (5, 12)
CHECK_URL = "https://api-home.szyouth.cn/api/users/own/orders/check"
ORDER_URL = "https://api-home.szyouth.cn/api/users/own/orders"


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["POST"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


session = build_session()


def add_log(msg: str, level: str = "info") -> None:
    grabber_state["logs"].append(
        {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    )
    if len(grabber_state["logs"]) > 500:
        grabber_state["logs"] = grabber_state["logs"][-500:]
    grabber_state["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def build_booking_candidates() -> list[dict[str, int | str]]:
    come_date = datetime.strptime(config["come_date"], "%Y-%m-%d")
    leave_date = datetime.strptime(config["leave_date"], "%Y-%m-%d")
    stay_days = (leave_date - come_date).days
    if stay_days <= 0:
        raise ValueError("离店日期必须晚于入住日期")

    return [
        {
            "come_date": come_date.strftime("%Y-%m-%d"),
            "leave_date": leave_date.strftime("%Y-%m-%d"),
            "stay_days": stay_days,
        }
    ]


def classify_response(r: requests.Response) -> tuple[str, str]:
    body = r.text.strip()
    content_type = (r.headers.get("Content-Type") or "").lower()

    if not body:
        return "空响应", "error"

    try:
        result = r.json()
        message = result.get("message", str(result))
        return str(message), "info"
    except json.JSONDecodeError:
        pass

    preview = body[:120].replace("\n", " ")
    looks_like_html = "<html" in body.lower() or "<!doctype html>" in body.lower()

    if r.status_code in (401, 403):
        return f"⚠️ 鉴权失败(HTTP {r.status_code})，请重新登录获取 token", "error"

    if looks_like_html or "text/html" in content_type:
        return (
            f"⚠️ 返回HTML页面(HTTP {r.status_code})，更像网关/登录页拦截，不一定是 token 过期: {preview}",
            "warning",
        )

    return f"⚠️ 非JSON响应(HTTP {r.status_code}): {preview}", "warning"


def classify_exception(exc: Exception) -> str:
    detail = str(exc).split("\n", 1)[0]
    if isinstance(exc, Timeout):
        return f"网络超时: {detail[:120]}"
    if isinstance(exc, ConnectionError):
        return f"连接异常: {detail[:120]}"
    if isinstance(exc, RequestException):
        return f"请求异常: {detail[:120]}"
    return f"未知异常: {detail[:120]}"


def request_booking_step(
    url: str, data: dict[str, int | str], headers: dict[str, str], step: str
) -> tuple[requests.Response, str, str]:
    grabber_state["last_request"] = {
        "url": url,
        "method": "POST",
        "headers": headers,
        "data": data,
        "step": step,
        "time": datetime.now().strftime("%H:%M:%S"),
    }

    r = session.post(
        url,
        json=data,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )

    grabber_state["last_response"] = {
        "step": step,
        "status_code": r.status_code,
        "text": r.text[:500],
        "time": datetime.now().strftime("%H:%M:%S"),
    }
    msg, msg_level = classify_response(r)
    return r, msg, msg_level


def grabber_thread():
    attempt = 0
    while grabber_state["running"]:
        attempt += 1
        grabber_state["attempt"] = attempt
        add_log(f"第 {attempt} 次尝试...")
        available = []
        candidates = build_booking_candidates()
        headers = {
            "Authorization": f"Bearer {config['token']}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        }

        if candidates:
            first_candidate = candidates[0]
            add_log(
                f"固定申请区间: {first_candidate['come_date']} ~ {first_candidate['leave_date']} "
                f"({first_candidate['stay_days']} 晚)，先预检再下单",
                "warning",
            )

        for candidate in candidates:
            for hostel_id, hostel_name, _commute in HOSTELS:
                if (
                    not grabber_state["running"]
                    or hostel_id not in grabber_state["selected_hostels"]
                ):
                    continue

                data = {
                    "come_date": candidate["come_date"],
                    "leave_date": candidate["leave_date"],
                    "hotel_id": hostel_id,
                    "user_name": config["name"],
                    "user_phone": config["phone"],
                    "user_gender": config["gender"],
                }

                try:
                    check_response, check_msg, check_level = request_booking_step(
                        CHECK_URL, data, headers, "check"
                    )

                    grabber_state["hostel_status"][hostel_id] = {
                        "name": hostel_name,
                        "status": f"{candidate['come_date']} ~ {candidate['leave_date']} | 预检: {check_msg}",
                        "time": datetime.now().strftime("%H:%M:%S"),
                    }

                    if not (
                        check_response.status_code == 200
                        and ("成功" in check_msg or "操作成功" in check_msg)
                    ):
                        add_log(
                            f"⚡ {hostel_name} {candidate['come_date']} 住 {candidate['stay_days']} 天: 预检未通过 - {check_msg[:60]}",
                            check_level,
                        )
                        available.append(
                            {
                                "id": hostel_id,
                                "name": hostel_name,
                                "msg": f"预检未通过: {check_msg}",
                                "come_date": candidate["come_date"],
                                "leave_date": candidate["leave_date"],
                                "stay_days": candidate["stay_days"],
                            }
                        )
                        continue

                    r, msg, msg_level = request_booking_step(
                        ORDER_URL, data, headers, "order"
                    )
                    grabber_state["hostel_status"][hostel_id] = {
                        "name": hostel_name,
                        "status": f"{candidate['come_date']} ~ {candidate['leave_date']} | 下单: {msg}",
                        "time": datetime.now().strftime("%H:%M:%S"),
                    }

                    if r.status_code == 200 and "成功" in msg:
                        grabber_state["success"] = True
                        grabber_state["success_hostel"] = (
                            f"{hostel_name} ({candidate['come_date']} ~ {candidate['leave_date']})"
                        )
                        add_log(
                            f"🎉 抢房成功! {hostel_name} {candidate['come_date']} ~ {candidate['leave_date']}",
                            "success",
                        )
                        grabber_state["running"] = False
                        return
                    elif "已满" in msg:
                        add_log(
                            f"❌ {hostel_name} {candidate['come_date']} 住 {candidate['stay_days']} 天已满",
                            "error",
                        )
                    else:
                        add_log(
                            f"⚡ {hostel_name} {candidate['come_date']} 住 {candidate['stay_days']} 天: {msg[:60]}",
                            msg_level,
                        )
                        available.append(
                            {
                                "id": hostel_id,
                                "name": hostel_name,
                                "msg": msg,
                                "come_date": candidate["come_date"],
                                "leave_date": candidate["leave_date"],
                                "stay_days": candidate["stay_days"],
                            }
                        )
                except Exception as e:
                    error_msg = classify_exception(e)
                    add_log(
                        f"❌ {hostel_name} {candidate['come_date']} 住 {candidate['stay_days']} 天: {error_msg}",
                        "error",
                    )
                    grabber_state["last_response"] = {
                        "error": error_msg,
                        "time": datetime.now().strftime("%H:%M:%S"),
                    }

        grabber_state["available_hostels"] = available
        if attempt % 10 == 0:
            add_log(f"⏳ 第 {attempt} 次完成", "warning")
        time.sleep(grabber_state["interval"])
    add_log("抢房已停止", "warning")


@route("/")
def index():
    return INDEX_HTML


@route("/api/status")
def api_status():
    response.content_type = "application/json"
    state = dict(grabber_state)
    state["config"] = config
    return json.dumps(state)


@route("/api/config", method="POST")
def api_config():
    data = request.json
    if isinstance(data, dict):
        for key in [
            "token",
            "name",
            "phone",
            "gender",
            "id_card",
            "school",
            "major",
            "xuexin_code",
            "graduation_date",
            "is_shenzhen",
            "come_date",
            "leave_date",
        ]:
            if key in data:
                config[key] = data[key]
        save_config()
    response.content_type = "application/json"
    return json.dumps({"status": "ok", "config": config})


@route("/api/hostels")
def api_hostels():
    response.content_type = "application/json"
    return json.dumps([{"id": h[0], "name": h[1], "time": h[2]} for h in HOSTELS])


@route("/api/select", method="POST")
def api_select():
    data = request.json
    if isinstance(data, dict) and "hostels" in data:
        grabber_state["selected_hostels"] = data["hostels"]
    response.content_type = "application/json"
    return json.dumps({"status": "ok"})


@route("/api/settings", method="POST")
def api_settings():
    data = request.json
    if isinstance(data, dict) and "interval" in data:
        interval = float(data["interval"])
        if 0.5 <= interval <= 10:
            grabber_state["interval"] = interval
    response.content_type = "application/json"
    return json.dumps({"status": "ok", "interval": grabber_state["interval"]})


@route("/api/start", method="POST")
def api_start():
    if not grabber_state["running"]:
        if not grabber_state["selected_hostels"]:
            response.content_type = "application/json"
            return json.dumps({"status": "error", "message": "请至少选择一个驿站"})
        grabber_state["running"] = True
        grabber_state["success"] = False
        grabber_state["logs"] = []
        grabber_state["hostel_status"] = {}
        grabber_state["available_hostels"] = []
        add_log(
            f"🚀 启动! 目标: {len(grabber_state['selected_hostels'])} 个驿站", "success"
        )
        threading.Thread(target=grabber_thread, daemon=True).start()
    response.content_type = "application/json"
    return json.dumps({"status": "started"})


@route("/api/stop", method="POST")
def api_stop():
    grabber_state["running"] = False
    add_log("⏹️ 已停止", "warning")
    response.content_type = "application/json"
    return json.dumps({"status": "stopped"})


INDEX_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>驿站抢房系统</title><style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display',sans-serif;background:#f9fafb;min-height:100vh;padding:20px}
.container{max-width:1400px;margin:0 auto}
h1{color:#1f2937;text-align:center;margin-bottom:30px;font-size:2em;font-weight:600}
h3{color:#1f2937;font-size:1.1em;font-weight:600;margin-bottom:12px}
.card{background:#fff;border-radius:12px;padding:24px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,0.1);border:1px solid #e5e7eb}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.stat{background:linear-gradient(135deg,#0066ff,#3b82f6);color:#fff;padding:20px;border-radius:10px;text-align:center}
.stat-value{font-size:2em;font-weight:600;margin-bottom:5px}
.stat-label{font-size:0.85em;opacity:0.9}
.config-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:15px;margin-bottom:20px}
.config-item{display:flex;flex-direction:column;gap:8px}
.config-label{font-size:0.9em;font-weight:500;color:#374151}
.config-input{padding:10px;border:1px solid #d1d5db;border-radius:8px;font-size:0.95em;transition:border 0.2s}
.config-input:focus{outline:none;border-color:#0066ff}
.config-hint{font-size:0.8em;color:#6b7280}
.controls{display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap}
button{padding:12px 24px;border:none;border-radius:8px;font-size:0.95em;font-weight:500;cursor:pointer;transition:all 0.2s}
.btn-start{background:#10b981;color:#fff}.btn-start:hover{background:#059669;transform:translateY(-1px)}
.btn-stop{background:#ef4444;color:#fff}.btn-stop:hover{background:#dc2626;transform:translateY(-1px)}
.btn-select{background:#0066ff;color:#fff;padding:10px 20px;font-size:0.9em}.btn-select:hover{background:#0052cc;transform:translateY(-1px)}
.btn-save{background:#8b5cf6;color:#fff}.btn-save:hover{background:#7c3aed;transform:translateY(-1px)}
button:disabled{opacity:0.5;cursor:not-allowed;transform:none}
.hostels{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px;margin-bottom:20px}
.hostel-item{display:flex;align-items:center;padding:12px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;transition:all 0.2s}
.hostel-item:hover{background:#f3f4f6;border-color:#d1d5db}
.hostel-item input{margin-right:10px;width:18px;height:18px;cursor:pointer;accent-color:#0066ff}
.hostel-name{flex:1;font-weight:500;color:#1f2937}
.hostel-time{font-size:0.85em;color:#6b7280}
.request-display{background:#1f2937;color:#fff;padding:15px;border-radius:10px;font-family:monospace;font-size:0.85em;max-height:300px;overflow-y:auto;margin-top:15px}
.request-title{color:#60a5fa;font-weight:600;margin-bottom:10px}
.request-content{white-space:pre-wrap;word-break:break-all}
.logs{max-height:400px;overflow-y:auto;background:#1f2937;color:#fff;padding:15px;border-radius:10px;font-family:monospace;font-size:0.9em}
.log-entry{padding:5px 0;border-bottom:1px solid #374151}
.log-time{color:#9ca3af;margin-right:10px}
.log-info{color:#60a5fa}.log-success{color:#34d399}.log-error{color:#f87171}.log-warning{color:#fbbf24}
.success-popup{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:#fff;padding:40px;border-radius:16px;box-shadow:0 20px 60px rgba(0,0,0,0.3);text-align:center;z-index:1000;animation:popup 0.4s}
@keyframes popup{from{transform:translate(-50%,-50%) scale(0.8);opacity:0}to{transform:translate(-50%,-50%) scale(1);opacity:1}}
.success-popup h2{color:#10b981;font-size:2em;margin-bottom:20px;font-weight:600}
.overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.4);z-index:999}
</style></head><body>
<div class="container">
<h1>🏠 驿站抢房系统</h1>
<div class="card">
<h3>📊 运行状态</h3>
<div class="stats">
<div class="stat"><div class="stat-value" id="status">待机</div><div class="stat-label">运行状态</div></div>
<div class="stat"><div class="stat-value" id="attempt">0</div><div class="stat-label">尝试次数</div></div>
<div class="stat"><div class="stat-value" id="selected">0</div><div class="stat-label">已选驿站</div></div>
<div class="stat"><div class="stat-value" id="logCount">0</div><div class="stat-label">日志条数</div></div>
</div>
</div>
<div class="card">
<h3>⚙️ 配置设置</h3>
<div class="config-grid">
<div class="config-item">
<label class="config-label">姓名</label>
<input type="text" class="config-input" id="name" placeholder="请输入姓名">
</div>
<div class="config-item">
<label class="config-label">手机号</label>
<input type="text" class="config-input" id="phone" placeholder="请输入手机号">
</div>
<div class="config-item">
<label class="config-label">身份证号</label>
<input type="text" class="config-input" id="idCard" placeholder="请输入身份证号">
</div>
<div class="config-item">
<label class="config-label">学校</label>
<input type="text" class="config-input" id="school" placeholder="请输入学校">
</div>
<div class="config-item">
<label class="config-label">专业</label>
<input type="text" class="config-input" id="major" placeholder="请输入专业">
</div>
<div class="config-item">
<label class="config-label">学信网验证码</label>
<input type="text" class="config-input" id="xuexinCode" placeholder="请输入学信网验证码">
</div>
<div class="config-item">
<label class="config-label">毕业日期</label>
<input type="date" class="config-input" id="graduationDate">
</div>
<div class="config-item">
<label class="config-label">是否深圳籍贯</label>
<select class="config-input" id="isShenzhen">
<option value="false">否</option>
<option value="true">是</option>
</select>
</div>
<div class="config-item">
<label class="config-label">入住日期</label>
<input type="date" class="config-input" id="comeDate">
</div>
<div class="config-item">
<label class="config-label">离店日期</label>
<input type="date" class="config-input" id="leaveDate">
<span class="config-hint">固定申请区间，脚本会先预检再正式下单</span>
</div>
<div class="config-item">
<label class="config-label">抢房频率（秒）</label>
<input type="number" class="config-input" id="interval" min="0.5" max="10" step="0.5">
<span class="config-hint">范围: 0.5-10秒</span>
</div>
<div class="config-item">
<label class="config-label">Token</label>
<input type="text" class="config-input" id="token" placeholder="请输入Token">
</div>
</div>
<button class="btn-save" onclick="saveConfig()">💾 保存配置</button>
<div id="configStatus" style="margin-top:10px;color:#10b981;font-size:0.9em"></div>
</div>
<div class="card">
<h3>🎯 控制面板</h3>
<div class="controls">
<button class="btn-start" onclick="start()">开始抢房</button>
<button class="btn-stop" onclick="stop()">停止</button>
<button class="btn-select" onclick="selectAll()">全选</button>
<button class="btn-select" onclick="selectNone()">取消全选</button>
</div>
<h3 style="margin-top:20px">选择驿站（按通勤时间排序）</h3>
<div class="hostels" id="hostels"></div>
</div>
<div class="card">
<h3>📡 请求数据</h3>
<div class="request-display">
<div class="request-title">最后请求 (POST)</div>
<div class="request-content" id="lastRequest">暂无请求数据</div>
</div>
<div class="request-display" style="margin-top:10px">
<div class="request-title">最后响应</div>
<div class="request-content" id="lastResponse">暂无响应数据</div>
</div>
</div>
<div class="card">
<h3>📝 实时日志</h3>
<div class="logs" id="logs"></div>
</div>
</div>
<script>
let hostels=[];
async function loadConfig(){
const r=await fetch('/api/status');const d=await r.json();
document.getElementById('name').value=d.config.name||'';
document.getElementById('phone').value=d.config.phone||'';
document.getElementById('idCard').value=d.config.id_card||'';
document.getElementById('school').value=d.config.school||'';
document.getElementById('major').value=d.config.major||'';
document.getElementById('xuexinCode').value=d.config.xuexin_code||'';
document.getElementById('graduationDate').value=d.config.graduation_date||'';
document.getElementById('isShenzhen').value=String(d.config.is_shenzhen??false);
document.getElementById('comeDate').value=d.config.come_date||'';
document.getElementById('leaveDate').value=d.config.leave_date||'';
document.getElementById('interval').value=d.interval||3;
document.getElementById('token').value=d.config.token||'';
}
async function saveConfig(){
const cfg={
name:document.getElementById('name').value,
phone:document.getElementById('phone').value,
id_card:document.getElementById('idCard').value,
school:document.getElementById('school').value,
major:document.getElementById('major').value,
xuexin_code:document.getElementById('xuexinCode').value,
graduation_date:document.getElementById('graduationDate').value,
is_shenzhen:document.getElementById('isShenzhen').value==='true',
come_date:document.getElementById('comeDate').value,
leave_date:document.getElementById('leaveDate').value,
token:document.getElementById('token').value
};
await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
const interval=parseFloat(document.getElementById('interval').value);
await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({interval:interval})});
document.getElementById('configStatus').textContent='✓ 配置已保存';
setTimeout(()=>document.getElementById('configStatus').textContent='',3000);
}
async function loadHostels(){
const r=await fetch('/api/hostels');hostels=await r.json();
const saved=JSON.parse(localStorage.getItem('selected')||'[]');
const selected=saved.length?saved:hostels.map(h=>h.id);
const html=hostels.map(h=>`<div class="hostel-item"><input type="checkbox" value="${h.id}" ${selected.includes(h.id)?'checked':''} onchange="saveSelection()"><span class="hostel-name">${h.name}</span><span class="hostel-time">${h.time}</span></div>`).join('');
document.getElementById('hostels').innerHTML=html;
saveSelection();
}
function saveSelection(){
const checked=[...document.querySelectorAll('.hostel-item input:checked')].map(e=>parseInt(e.value));
localStorage.setItem('selected',JSON.stringify(checked));
fetch('/api/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hostels:checked})});
document.getElementById('selected').textContent=checked.length;
}
function selectAll(){document.querySelectorAll('.hostel-item input').forEach(e=>e.checked=true);saveSelection()}
function selectNone(){document.querySelectorAll('.hostel-item input').forEach(e=>e.checked=false);saveSelection()}
async function start(){await fetch('/api/start',{method:'POST'})}
async function stop(){await fetch('/api/stop',{method:'POST'})}
async function update(){
const r=await fetch('/api/status');const d=await r.json();
document.getElementById('status').textContent=d.running?'运行中':'待机';
document.getElementById('attempt').textContent=d.attempt;
document.getElementById('logCount').textContent=d.logs.length;
const logsHtml=d.logs.slice(-50).reverse().map(l=>`<div class="log-entry"><span class="log-time">${l.time}</span><span class="log-${l.level}">${l.msg}</span></div>`).join('');
document.getElementById('logs').innerHTML=logsHtml||'<div style="color:#9ca3af">暂无日志</div>';
if(d.last_request){
const req=d.last_request;
document.getElementById('lastRequest').textContent=`时间: ${req.time}\nURL: ${req.url}\n方法: ${req.method}\n\n请求头:\n${JSON.stringify(req.headers,null,2)}\n\n请求数据:\n${JSON.stringify(req.data,null,2)}`;
}
if(d.last_response){
const res=d.last_response;
if(res.error){
document.getElementById('lastResponse').textContent=`时间: ${res.time}\n错误: ${res.error}`;
}else{
document.getElementById('lastResponse').textContent=`时间: ${res.time}\n状态码: ${res.status_code}\n\n响应内容:\n${res.text}`;
}
}
if(d.success&&!document.querySelector('.success-popup')){
document.body.insertAdjacentHTML('beforeend',`<div class="overlay"></div><div class="success-popup"><h2>🎉 抢房成功!</h2><p style="font-size:1.2em;color:#374151">${d.success_hostel}</p></div>`);
}
}
loadConfig();loadHostels();setInterval(update,2000);update();
</script></body></html>
"""


if __name__ == "__main__":
    print("启动服务: http://localhost:5011")
    run(host="0.0.0.0", port=5011, debug=False)
