#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深圳青年驿站抢房脚本 - 后台持续运行版本"""

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, RequestException, Timeout
import time
from datetime import datetime
import logging
import json
import os
from pathlib import Path
from urllib3.util.retry import Retry

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("grab.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

DEFAULT_CONFIG = {
    "token": "",
    "name": "",
    "phone": "",
    "gender": 2000,
    "come_date": "2026-04-04",
    "leave_date": "2026-04-18",
}
CONFIG_PATH = Path(os.environ.get("YOUTH_CONFIG", Path(__file__).with_name("config.json")))


def load_config() -> dict[str, str | int]:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            config.update(json.load(f))

    env_overrides = {
        "token": os.environ.get("YOUTH_TOKEN"),
        "name": os.environ.get("YOUTH_NAME"),
        "phone": os.environ.get("YOUTH_PHONE"),
        "come_date": os.environ.get("YOUTH_COME_DATE"),
        "leave_date": os.environ.get("YOUTH_LEAVE_DATE"),
    }
    for key, value in env_overrides.items():
        if value:
            config[key] = value
    if os.environ.get("YOUTH_GENDER"):
        config["gender"] = int(os.environ["YOUTH_GENDER"])
    return config


CONFIG = load_config()
TOKEN = str(CONFIG["token"])
NAME = str(CONFIG["name"])
PHONE = str(CONFIG["phone"])
GENDER = int(CONFIG["gender"])
COME = str(CONFIG["come_date"])
LEAVE = str(CONFIG["leave_date"])

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

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0",
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


def build_booking_candidates() -> list[dict[str, int | str]]:
    come_date = datetime.strptime(COME, "%Y-%m-%d")
    leave_date = datetime.strptime(LEAVE, "%Y-%m-%d")
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
    url: str, data: dict[str, int | str], step: str
) -> tuple[requests.Response, str, str]:
    r = session.post(
        url,
        json=data,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    msg, msg_level = classify_response(r)
    logging.info(
        f"[{step}] {data['hotel_id']} {data['come_date']} ~ {data['leave_date']} -> HTTP {r.status_code} | {msg}"
    )
    return r, msg, msg_level


def try_grab():
    """尝试抢房"""
    for candidate in build_booking_candidates():
        for hostel_id, hostel_name, _commute in HOSTELS:
            data = {
                "come_date": candidate["come_date"],
                "leave_date": candidate["leave_date"],
                "hotel_id": hostel_id,
                "user_name": NAME,
                "user_phone": PHONE,
                "user_gender": GENDER,
            }

            try:
                check_response, check_msg, _check_level = request_booking_step(
                    CHECK_URL, data, "check"
                )

                if not (
                    check_response.status_code == 200
                    and ("成功" in check_msg or "操作成功" in check_msg)
                ):
                    logging.info(
                        f"⚡ {hostel_name} {candidate['come_date']} 住 {candidate['stay_days']} 天: 预检未通过 - {check_msg}"
                    )
                    continue

                r, msg, _msg_level = request_booking_step(ORDER_URL, data, "order")

                if r.status_code == 200 and "成功" in msg:
                    logging.info(
                        f"🎉 抢房成功! {hostel_name} {candidate['come_date']} ~ {candidate['leave_date']} - {msg}"
                    )
                    return True
                elif "已满" in msg:
                    logging.error(
                        f"❌ {hostel_name} {candidate['come_date']} 住 {candidate['stay_days']} 天已满"
                    )
                else:
                    logging.info(
                        f"⚡ {hostel_name} {candidate['come_date']} 住 {candidate['stay_days']} 天: {msg}"
                    )
            except Exception as e:
                error_msg = classify_exception(e)
                logging.error(
                    f"❌ {hostel_name} {candidate['come_date']} 住 {candidate['stay_days']} 天: {error_msg}"
                )

    return False


def main():
    """主循环"""
    logging.info("=" * 50)
    logging.info("深圳青年驿站抢房脚本启动")
    logging.info(f"固定申请区间: {COME} ~ {LEAVE}，先走预检接口再正式下单")
    logging.info(f"目标驿站: {len(HOSTELS)} 个")
    logging.info("=" * 50)

    attempt = 0
    while True:
        attempt += 1
        logging.info(f"第 {attempt} 次尝试...")

        if try_grab():
            logging.info("抢房成功！脚本退出。")
            break

        if attempt % 10 == 0:
            logging.warning(f"已尝试 {attempt} 次，继续...")

        time.sleep(3)


if __name__ == "__main__":
    main()
