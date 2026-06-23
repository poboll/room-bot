from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
import json
import logging
import os
import re
import ssl
import threading
import time
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import (
    Flask,
    Response,
    make_response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)
from requests.adapters import HTTPAdapter

try:
    import certifi
except Exception:  # pragma: no cover - optional dependency fallback
    certifi = None


DEFAULT_ACTIVITY_ID = "8248ec76-016d-11f0-aa1d-a088c260cfb6"
ACTIVITY_PAGE_URL = "https://sz.inboyu.com/activity/graduate"
BOOKING_URL = "https://sz.inboyu.com/graduate-apply-check-in/add"
DEFAULT_PROXY_API_URL = os.environ.get("QIANGFANG_PROXY_API_URL", "")
DEFAULT_PROXY_WHITELIST_URL = os.environ.get("QIANGFANG_PROXY_WHITELIST_URL", "")
DEFAULT_PROXY_TTL_SECONDS = 20
DEFAULT_PROXY_MIN_FETCH_INTERVAL = 5
DEFAULT_PROXY_FAILURE_BACKOFF = 8
DEFAULT_MAX_WORKERS = 10
DEFAULT_PER_ACCOUNT_MAX_INFLIGHT = 2
DEFAULT_REQUEST_TIMEOUT = 8.0
DEFAULT_DYNAMIC_REQUEST_TIMEOUT_MIN = 5.0
DEFAULT_PROXY_WHITELIST_CACHE_SECONDS = 180
DEFAULT_PROXY_WHITELIST_TIMEOUT = 5.0
DEFAULT_CSRF_STALE_FALLBACK_SECONDS = 900
DEFAULT_PUBLIC_IP_CHECK_URLS = [
    "http://ipinfo.io/ip",
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
]
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.62(0x18003e26) NetType/WIFI "
    "Language/zh_CN miniProgram/wx4e093affadd67bb1"
)
SESSION_POOL_SIZE = 20
HEARTBEAT_INTERVAL_SECONDS = 30
THROTTLE_STATUSES = {405, 429}
RECOVERABLE_RETRY_STATUSES = {400, 405, 429, 502, 503}
SESSION_IDLE_TTL_SECONDS = 90
MAX_THREAD_SESSIONS = 6
UNSET_PROXY = object()


app = Flask(__name__)
scheduler = BackgroundScheduler()
scheduler.start()

BARK_URL = os.environ.get("QIANGFANG_BARK_URL", "")
logger = logging.getLogger(__name__)

csrf_cache = {}
csrf_cache_lock = threading.Lock()
proxy_cache = {}
proxy_cache_lock = threading.Lock()
proxy_whitelist_state = {
    "ip": None,
    "refreshed_at": 0.0,
    "expires_at": 0.0,
    "last_error": None,
}
proxy_whitelist_lock = threading.Lock()
thread_local = threading.local()
scheduled_runs = {}
scheduled_runs_lock = threading.Lock()
manual_run_state = {"run_id": None, "stop_event": None, "started_at": 0.0}
manual_run_lock = threading.Lock()


class ProxyAuthFailure(RuntimeError):
    pass


def resolve_ca_bundle_path():
    candidates = []
    for env_name in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"):
        value = (os.environ.get(env_name) or "").strip()
        if value:
            candidates.append(value)

    if certifi is not None:
        try:
            candidates.append(certifi.where())
        except Exception:
            pass

    verify_paths = ssl.get_default_verify_paths()
    if verify_paths.cafile:
        candidates.append(verify_paths.cafile)
    candidates.append("/etc/ssl/cert.pem")

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isfile(candidate):
            return candidate
    return None


CA_BUNDLE_PATH = resolve_ca_bundle_path()


def build_default_config():
    return {
        "start_delay": 0,
        "interval": 0.5,
        "max_count": 0,
        "keepalive_interval_seconds": 6.0,
        "warmup_interval_seconds": 1.0,
        "peak_interval_seconds": 0.5,
        "cooldown_interval_seconds": 1.0,
        "warmup_before_seconds": 420,
        "peak_after_seconds": 420,
        "cooldown_after_seconds": 900,
        "schedule_lead_seconds": 1800,
        "success_stop_threshold": 10,
        "stop_after_first_success": True,
        "bark_enabled": False,
        "accounts": [],
        "proxy_enabled": True,
        "proxy_api_url": DEFAULT_PROXY_API_URL,
        "proxy_whitelist_enabled": True,
        "proxy_whitelist_url": DEFAULT_PROXY_WHITELIST_URL,
        "proxy_whitelist_cache_seconds": DEFAULT_PROXY_WHITELIST_CACHE_SECONDS,
        "proxy_whitelist_timeout_seconds": DEFAULT_PROXY_WHITELIST_TIMEOUT,
        "public_ip_check_urls": list(DEFAULT_PUBLIC_IP_CHECK_URLS),
        "proxy_ttl_seconds": DEFAULT_PROXY_TTL_SECONDS,
        "csrf_cache_seconds": 24,
        "csrf_stale_fallback_enabled": True,
        "csrf_stale_fallback_seconds": DEFAULT_CSRF_STALE_FALLBACK_SECONDS,
        "proxy_min_fetch_interval_seconds": DEFAULT_PROXY_MIN_FETCH_INTERVAL,
        "proxy_failure_backoff_seconds": DEFAULT_PROXY_FAILURE_BACKOFF,
        "max_workers": DEFAULT_MAX_WORKERS,
        "per_account_max_inflight": DEFAULT_PER_ACCOUNT_MAX_INFLIGHT,
        "request_timeout": DEFAULT_REQUEST_TIMEOUT,
    }


def ensure_config_defaults(config):
    merged = dict(build_default_config())
    merged.update(config or {})
    if "accounts" not in merged:
        merged["accounts"] = []

    try:
        ttl = int(merged.get("proxy_ttl_seconds", DEFAULT_PROXY_TTL_SECONDS))
    except (TypeError, ValueError):
        ttl = DEFAULT_PROXY_TTL_SECONDS
    merged["proxy_ttl_seconds"] = max(5, min(ttl, 25))

    try:
        min_fetch = int(
            merged.get(
                "proxy_min_fetch_interval_seconds", DEFAULT_PROXY_MIN_FETCH_INTERVAL
            )
        )
    except (TypeError, ValueError):
        min_fetch = DEFAULT_PROXY_MIN_FETCH_INTERVAL
    merged["proxy_min_fetch_interval_seconds"] = max(
        1, min(min_fetch, merged["proxy_ttl_seconds"])
    )

    try:
        csrf_cache_seconds = int(merged.get("csrf_cache_seconds", 24))
    except (TypeError, ValueError):
        csrf_cache_seconds = 24
    merged["csrf_cache_seconds"] = max(5, min(csrf_cache_seconds, 60))

    merged["csrf_stale_fallback_enabled"] = bool(
        merged.get("csrf_stale_fallback_enabled", True)
    )

    try:
        stale_seconds = int(
            merged.get(
                "csrf_stale_fallback_seconds", DEFAULT_CSRF_STALE_FALLBACK_SECONDS
            )
        )
    except (TypeError, ValueError):
        stale_seconds = DEFAULT_CSRF_STALE_FALLBACK_SECONDS
    merged["csrf_stale_fallback_seconds"] = max(30, min(stale_seconds, 1800))

    try:
        backoff = int(
            merged.get("proxy_failure_backoff_seconds", DEFAULT_PROXY_FAILURE_BACKOFF)
        )
    except (TypeError, ValueError):
        backoff = DEFAULT_PROXY_FAILURE_BACKOFF
    merged["proxy_failure_backoff_seconds"] = max(2, min(backoff, 60))

    try:
        per_account_max_inflight = int(
            merged.get(
                "per_account_max_inflight", DEFAULT_PER_ACCOUNT_MAX_INFLIGHT
            )
        )
    except (TypeError, ValueError):
        per_account_max_inflight = DEFAULT_PER_ACCOUNT_MAX_INFLIGHT
    merged["per_account_max_inflight"] = max(1, min(per_account_max_inflight, 4))

    try:
        keepalive_interval = float(merged.get("keepalive_interval_seconds", 6.0))
    except (TypeError, ValueError):
        keepalive_interval = 6.0
    merged["keepalive_interval_seconds"] = max(0.5, min(keepalive_interval, 60.0))

    try:
        warmup_interval = float(merged.get("warmup_interval_seconds", 1.0))
    except (TypeError, ValueError):
        warmup_interval = 1.0
    merged["warmup_interval_seconds"] = max(0.2, min(warmup_interval, 10.0))

    try:
        peak_interval = float(merged.get("peak_interval_seconds", 0.5))
    except (TypeError, ValueError):
        peak_interval = 0.5
    merged["peak_interval_seconds"] = max(0.1, min(peak_interval, 10.0))

    try:
        cooldown_interval = float(merged.get("cooldown_interval_seconds", 1.0))
    except (TypeError, ValueError):
        cooldown_interval = 1.0
    merged["cooldown_interval_seconds"] = max(0.2, min(cooldown_interval, 10.0))

    try:
        warmup_before_seconds = int(merged.get("warmup_before_seconds", 420))
    except (TypeError, ValueError):
        warmup_before_seconds = 420
    merged["warmup_before_seconds"] = max(0, min(warmup_before_seconds, 3600))

    try:
        peak_after_seconds = int(merged.get("peak_after_seconds", 420))
    except (TypeError, ValueError):
        peak_after_seconds = 420
    merged["peak_after_seconds"] = max(0, min(peak_after_seconds, 3600))

    try:
        cooldown_after_seconds = int(merged.get("cooldown_after_seconds", 900))
    except (TypeError, ValueError):
        cooldown_after_seconds = 900
    merged["cooldown_after_seconds"] = max(0, min(cooldown_after_seconds, 7200))

    try:
        schedule_lead_seconds = int(merged.get("schedule_lead_seconds", 1800))
    except (TypeError, ValueError):
        schedule_lead_seconds = 1800
    merged["schedule_lead_seconds"] = max(0, min(schedule_lead_seconds, 43200))

    try:
        success_stop_threshold = int(merged.get("success_stop_threshold", 10))
    except (TypeError, ValueError):
        success_stop_threshold = 10
    merged["success_stop_threshold"] = max(1, min(success_stop_threshold, 100))

    merged["stop_after_first_success"] = bool(
        merged.get("stop_after_first_success", True)
    )

    try:
        whitelist_cache = int(
            merged.get(
                "proxy_whitelist_cache_seconds", DEFAULT_PROXY_WHITELIST_CACHE_SECONDS
            )
        )
    except (TypeError, ValueError):
        whitelist_cache = DEFAULT_PROXY_WHITELIST_CACHE_SECONDS
    merged["proxy_whitelist_cache_seconds"] = max(30, min(whitelist_cache, 3600))

    try:
        whitelist_timeout = float(
            merged.get(
                "proxy_whitelist_timeout_seconds", DEFAULT_PROXY_WHITELIST_TIMEOUT
            )
        )
    except (TypeError, ValueError):
        whitelist_timeout = DEFAULT_PROXY_WHITELIST_TIMEOUT
    merged["proxy_whitelist_timeout_seconds"] = max(2.0, min(whitelist_timeout, 15.0))

    ip_check_urls = merged.get("public_ip_check_urls")
    if not isinstance(ip_check_urls, list):
        ip_check_urls = list(DEFAULT_PUBLIC_IP_CHECK_URLS)
    merged["public_ip_check_urls"] = [
        str(item).strip() for item in ip_check_urls if str(item).strip()
    ] or list(DEFAULT_PUBLIC_IP_CHECK_URLS)
    return merged


def build_proxy_state():
    return {
        "value": None,
        "expires_at": 0.0,
        "source": None,
        "last_fetch_at": 0.0,
        "fail_until": 0.0,
        "version": 0,
    }


def send_bark_async(title, body, group="抢房"):
    if not BARK_URL:
        return

    def _send():
        try:
            from urllib.parse import quote

            title_encoded = quote(title)
            body_encoded = quote(body)
            group_encoded = quote(group)
            url = f"{BARK_URL}/{title_encoded}/{body_encoded}?group={group_encoded}"
            resp = requests.get(url, timeout=3)
            logger.info("[BARK] %s - %s: %s", resp.status_code, title, body)
        except Exception as exc:
            logger.warning("[BARK ERROR] %s", exc)

    threading.Thread(target=_send, daemon=True).start()


def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        raw = build_default_config()
        save_config(raw)
        return raw

    config = ensure_config_defaults(raw)
    if config != raw:
        save_config(config)
    return config


def save_config(config):
    with open("config.json", "w", encoding="utf-8") as handle:
        json.dump(ensure_config_defaults(config), handle, ensure_ascii=False, indent=2)


def resolve_selected_accounts(config, account_id):
    accounts = config.get("accounts", [])
    enabled_accounts = [acc for acc in accounts if acc.get("enabled", True)]
    if not enabled_accounts:
        return []

    if account_id:
        selected_accounts = []
        account_ids = [aid.strip() for aid in account_id.split(",") if aid.strip()]
        for aid in account_ids:
            for acc in enabled_accounts:
                if acc.get("id") == aid:
                    selected_accounts.append(acc)
                    break
        if selected_accounts:
            return selected_accounts

    return [enabled_accounts[0]]


def resolve_runtime_settings(config, requested_interval, requested_max_count):
    interval = config.get("interval", requested_interval)
    max_count = config.get("max_count", requested_max_count)

    try:
        interval = float(interval)
    except (TypeError, ValueError):
        interval = requested_interval

    try:
        max_count = int(max_count)
    except (TypeError, ValueError):
        max_count = requested_max_count

    if interval < 0:
        interval = requested_interval
    if max_count < 0:
        max_count = requested_max_count

    return interval, max_count, config.get("bark_enabled", True)


def build_target_datetime(value, now=None):
    if not value:
        return None

    try:
        hour, minute, second = parse_schedule_time(str(value))
    except Exception:
        return None

    current = now or datetime.now()
    return current.replace(hour=hour, minute=minute, second=second, microsecond=0)


def resolve_schedule_trigger_time(value, lead_seconds, now=None):
    target_dt = build_target_datetime(value, now=now)
    if not isinstance(target_dt, datetime):
        return None
    trigger_dt = target_dt - timedelta(seconds=max(0, int(lead_seconds or 0)))
    return trigger_dt.hour, trigger_dt.minute, trigger_dt.second


def resolve_effective_interval(runtime, now_ts=None):
    base_interval = float(runtime.get("base_interval", runtime.get("interval", 1.0)))
    if not runtime.get("auto_schedule_enabled"):
        return base_interval

    target_dt = runtime.get("target_open_at")
    if not isinstance(target_dt, datetime):
        return runtime.get("keepalive_interval", base_interval)

    timestamp = now_ts if now_ts is not None else time.time()
    target_ts = target_dt.timestamp()
    warmup_start = target_ts - runtime.get("warmup_before_seconds", 420)
    peak_end = target_ts + runtime.get("peak_after_seconds", 420)
    cooldown_end = target_ts + runtime.get("cooldown_after_seconds", 900)

    if warmup_start <= timestamp < target_ts:
        return runtime.get("warmup_interval", base_interval)
    if target_ts <= timestamp < peak_end:
        return runtime.get("peak_interval", base_interval)
    if peak_end <= timestamp < cooldown_end:
        return runtime.get("cooldown_interval", base_interval)
    return runtime.get("keepalive_interval", base_interval)


def describe_effective_interval_phase(runtime, now_ts=None):
    if not runtime.get("auto_schedule_enabled"):
        return "fixed"

    target_dt = runtime.get("target_open_at")
    if not isinstance(target_dt, datetime):
        return "keepalive"

    timestamp = now_ts if now_ts is not None else time.time()
    target_ts = target_dt.timestamp()
    warmup_start = target_ts - runtime.get("warmup_before_seconds", 420)
    peak_end = target_ts + runtime.get("peak_after_seconds", 420)
    cooldown_end = target_ts + runtime.get("cooldown_after_seconds", 900)

    if warmup_start <= timestamp < target_ts:
        return "warmup"
    if target_ts <= timestamp < peak_end:
        return "peak"
    if peak_end <= timestamp < cooldown_end:
        return "cooldown"
    return "keepalive"


def resolve_effective_request_timeout(runtime, now_ts=None):
    base_timeout = float(
        runtime.get(
            "base_request_timeout",
            runtime.get("request_timeout", DEFAULT_REQUEST_TIMEOUT),
        )
    )
    min_dynamic_timeout = float(
        runtime.get(
            "dynamic_request_timeout_min_seconds",
            DEFAULT_DYNAMIC_REQUEST_TIMEOUT_MIN,
        )
    )
    min_dynamic_timeout = max(3.0, min(min_dynamic_timeout, base_timeout))
    phase = describe_effective_interval_phase(runtime, now_ts)
    interval = float(runtime.get("interval", runtime.get("base_interval", 1.0)))
    if phase == "warmup":
        target_timeout = min(base_timeout, 5.5 if interval <= 1.0 else 6.0)
        return max(min_dynamic_timeout, target_timeout)
    if phase == "peak":
        target_timeout = min(base_timeout, 5.0 if interval <= 1.0 else 5.5)
        return max(min_dynamic_timeout, target_timeout)
    if phase == "cooldown":
        target_timeout = min(base_timeout, 5.5 if interval <= 1.0 else 6.0)
        return max(min_dynamic_timeout, target_timeout)
    if interval <= 0.5:
        target_timeout = min(base_timeout, 5.0)
        return max(min_dynamic_timeout, target_timeout)
    if interval <= 1.0:
        target_timeout = min(base_timeout, 5.0)
        return max(min_dynamic_timeout, target_timeout)
    if interval <= 2.0:
        target_timeout = min(base_timeout, 5.5)
        return max(min_dynamic_timeout, target_timeout)
    return base_timeout


def resolve_connect_timeout(runtime):
    read_timeout = float(runtime.get("request_timeout", DEFAULT_REQUEST_TIMEOUT))
    return max(1.0, min(3.0, read_timeout / 2))


def resolve_effective_account_inflight_limit(runtime, now_ts=None):
    try:
        base_limit = int(
            runtime.get("per_account_max_inflight", DEFAULT_PER_ACCOUNT_MAX_INFLIGHT)
        )
    except (TypeError, ValueError):
        base_limit = DEFAULT_PER_ACCOUNT_MAX_INFLIGHT
    base_limit = max(1, min(base_limit, 4))

    phase = describe_effective_interval_phase(runtime, now_ts)
    interval = float(runtime.get("interval", runtime.get("base_interval", 1.0)))

    if phase == "peak":
        return min(base_limit, 3)
    if phase == "warmup":
        return min(base_limit, 2)
    if interval <= 0.5:
        return min(base_limit, 3)
    if interval <= 1.0:
        return min(base_limit, 2)
    return 1


def should_inline_retry(runtime, retry_count, status_code=None):
    if retry_count >= 1:
        return False
    if status_code in THROTTLE_STATUSES:
        return False

    interval = float(runtime.get("interval", runtime.get("base_interval", 1.0)))
    phase = describe_effective_interval_phase(runtime)
    inflight_limit = resolve_effective_account_inflight_limit(runtime)

    if inflight_limit > 1:
        return False
    if phase in ("warmup", "peak"):
        return False
    if interval <= 1.5:
        return False
    return True


def should_retry_request_exception(exc):
    if isinstance(exc, requests.Timeout):
        return True
    if isinstance(exc, requests.ConnectionError):
        return True

    message = str(exc).lower()
    retry_markers = (
        "timed out",
        "remote end closed connection",
        "remotedisconnected",
        "proxyerror",
        "connection aborted",
        "connection reset",
        "503 server error",
        "502 server error",
    )
    return any(marker in message for marker in retry_markers)


def finalize_attempt_result(
    result, runtime, started_at, csrf_ready, retry_count, proxy
):
    result["duration_ms"] = int((time.time() - started_at) * 1000)
    result["interval"] = runtime.get("interval")
    result["csrf_ready"] = bool(csrf_ready)
    result["retry_count"] = retry_count
    if proxy and "proxy" not in result:
        result["proxy"] = proxy
    return result


def replace_manual_run(run_id, stop_event):
    with manual_run_lock:
        previous = None
        current_event = manual_run_state.get("stop_event")
        current_run_id = manual_run_state.get("run_id")
        if current_event and current_run_id != run_id:
            previous = {
                "run_id": current_run_id,
                "stop_event": current_event,
                "started_at": manual_run_state.get("started_at", 0.0),
            }
        manual_run_state.update(
            {"run_id": run_id, "stop_event": stop_event, "started_at": time.time()}
        )
    if previous:
        previous["stop_event"].set()
    return previous


def stop_manual_run(run_id=None):
    with manual_run_lock:
        stop_event = manual_run_state.get("stop_event")
        current_run_id = manual_run_state.get("run_id")
        if not stop_event:
            return False, None
        if run_id and current_run_id != run_id:
            return False, current_run_id
        stop_event.set()
        return True, current_run_id


def clear_manual_run(run_id, stop_event):
    with manual_run_lock:
        if (
            manual_run_state.get("run_id") == run_id
            and manual_run_state.get("stop_event") is stop_event
        ):
            manual_run_state.update(
                {"run_id": None, "stop_event": None, "started_at": 0.0}
            )


def classify_activity_page_response(response):
    if response.status_code in (401, 403):
        return False, "expired", "❌ Cookie已过期，需要重新登录"

    text = response.text or ""
    text_lower = text.lower()
    url = (getattr(response, "url", "") or "").lower()
    csrf_markers = ("var _csrf", 'name="_csrf"', "csrf-token", '"_csrf"')
    if response.status_code == 200 and any(marker in text for marker in csrf_markers):
        return True, "valid", "✅ Cookie有效"

    login_markers = ("请登录", "重新登录", "login", "signin", "登录")
    if response.history:
        for item in response.history:
            location = (item.headers.get("Location") or "").lower()
            if "login" in location:
                return (
                    False,
                    "redirected",
                    "❌ 请求被重定向到登录页，Cookie很可能已失效",
                )

    if any(marker in url for marker in ("login", "signin")) or any(
        marker in text_lower for marker in login_markers
    ):
        return False, "expired", "❌ Cookie已过期或已跳转到登录页"

    if response.status_code == 200:
        return (
            False,
            "structure_changed",
            "❌ 页面可访问，但未找到CSRF，可能是页面结构变化或账号状态异常",
        )

    return False, "http_error", f"❌ 请求失败 (状态码: {response.status_code})"


def extract_html_title(text):
    match = re.search(
        r"<title[^>]*>(.*?)</title>", text or "", flags=re.IGNORECASE | re.DOTALL
    )
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def classify_booking_html_response(response):
    if response.status_code in (401, 403):
        return "expired", "❌ 提交请求返回未授权，Cookie已失效，需要重新登录"

    text = response.text or ""
    text_lower = text.lower()
    url = (getattr(response, "url", "") or "").lower()
    title = extract_html_title(text) or "N/A"
    login_markers = ("请登录", "重新登录", "login", "signin", "登录")

    if response.history:
        for item in response.history:
            location = (item.headers.get("Location") or "").lower()
            if "login" in location:
                return "redirected", "❌ 提交请求被重定向到登录页，Cookie很可能已失效"

    if any(marker in url for marker in ("login", "signin")) or any(
        marker in text_lower for marker in login_markers
    ):
        return "expired", "❌ 提交请求返回登录页，Cookie已过期或账号已掉线"

    if response.status_code == 200:
        return "html_response", f"❌ 提交接口返回HTML页面，未拿到JSON（title: {title}）"

    return "http_error", f"❌ 提交请求失败 (状态码: {response.status_code})"


def resolve_engine_settings(config):
    try:
        max_workers = int(config.get("max_workers", DEFAULT_MAX_WORKERS))
    except (TypeError, ValueError):
        max_workers = DEFAULT_MAX_WORKERS
    max_workers = max(1, min(max_workers, 16))

    try:
        per_account_max_inflight = int(
            config.get("per_account_max_inflight", DEFAULT_PER_ACCOUNT_MAX_INFLIGHT)
        )
    except (TypeError, ValueError):
        per_account_max_inflight = DEFAULT_PER_ACCOUNT_MAX_INFLIGHT
    per_account_max_inflight = max(1, min(per_account_max_inflight, 4))

    try:
        request_timeout = float(config.get("request_timeout", DEFAULT_REQUEST_TIMEOUT))
    except (TypeError, ValueError):
        request_timeout = DEFAULT_REQUEST_TIMEOUT
    request_timeout = max(3.0, min(request_timeout, 30.0))

    try:
        proxy_ttl_seconds = int(
            config.get("proxy_ttl_seconds", DEFAULT_PROXY_TTL_SECONDS)
        )
    except (TypeError, ValueError):
        proxy_ttl_seconds = DEFAULT_PROXY_TTL_SECONDS
    proxy_ttl_seconds = max(5, min(proxy_ttl_seconds, 25))

    try:
        proxy_min_fetch = int(
            config.get(
                "proxy_min_fetch_interval_seconds", DEFAULT_PROXY_MIN_FETCH_INTERVAL
            )
        )
    except (TypeError, ValueError):
        proxy_min_fetch = DEFAULT_PROXY_MIN_FETCH_INTERVAL
    proxy_min_fetch = max(1, min(proxy_min_fetch, proxy_ttl_seconds))

    try:
        proxy_failure_backoff = int(
            config.get("proxy_failure_backoff_seconds", DEFAULT_PROXY_FAILURE_BACKOFF)
        )
    except (TypeError, ValueError):
        proxy_failure_backoff = DEFAULT_PROXY_FAILURE_BACKOFF
    proxy_failure_backoff = max(2, min(proxy_failure_backoff, 60))

    try:
        start_delay_ms = int(config.get("start_delay", 0))
    except (TypeError, ValueError):
        start_delay_ms = 0

    try:
        proxy_whitelist_cache_seconds = int(
            config.get(
                "proxy_whitelist_cache_seconds", DEFAULT_PROXY_WHITELIST_CACHE_SECONDS
            )
        )
    except (TypeError, ValueError):
        proxy_whitelist_cache_seconds = DEFAULT_PROXY_WHITELIST_CACHE_SECONDS
    proxy_whitelist_cache_seconds = max(30, min(proxy_whitelist_cache_seconds, 3600))

    try:
        proxy_whitelist_timeout_seconds = float(
            config.get(
                "proxy_whitelist_timeout_seconds", DEFAULT_PROXY_WHITELIST_TIMEOUT
            )
        )
    except (TypeError, ValueError):
        proxy_whitelist_timeout_seconds = DEFAULT_PROXY_WHITELIST_TIMEOUT
    proxy_whitelist_timeout_seconds = max(
        2.0, min(proxy_whitelist_timeout_seconds, 15.0)
    )

    public_ip_check_urls = config.get("public_ip_check_urls")
    if not isinstance(public_ip_check_urls, list):
        public_ip_check_urls = list(DEFAULT_PUBLIC_IP_CHECK_URLS)
    public_ip_check_urls = [
        str(item).strip() for item in public_ip_check_urls if str(item).strip()
    ] or list(DEFAULT_PUBLIC_IP_CHECK_URLS)

    return {
        "max_workers": max_workers,
        "per_account_max_inflight": per_account_max_inflight,
        "request_timeout": request_timeout,
        "proxy_enabled": bool(config.get("proxy_enabled", True)),
        "proxy_api_url": (config.get("proxy_api_url") or "").strip(),
        "proxy_whitelist_enabled": bool(config.get("proxy_whitelist_enabled", True)),
        "proxy_whitelist_url": (config.get("proxy_whitelist_url") or "").strip(),
        "proxy_whitelist_cache_seconds": proxy_whitelist_cache_seconds,
        "proxy_whitelist_timeout_seconds": proxy_whitelist_timeout_seconds,
        "public_ip_check_urls": public_ip_check_urls,
        "proxy_ttl_seconds": proxy_ttl_seconds,
        "proxy_min_fetch_interval_seconds": proxy_min_fetch,
        "proxy_failure_backoff_seconds": proxy_failure_backoff,
        "start_delay_ms": max(0, start_delay_ms),
    }


def build_cookie_str(cookies):
    if "raw_cookie" in cookies:
        raw = cookies["raw_cookie"]
        waf_token = cookies.get("acw_sc__v3", "").strip()
        if waf_token and "acw_sc__v3" not in raw:
            raw = raw.rstrip("; ") + f"; acw_sc__v3={waf_token}"
        return raw

    cookie_names = (
        "PHPSESSID",
        "_csrf",
        "_identity",
        "acw_tc",
        "acw_sc__v3",
        "HMACCOUNT",
        "gr_user_id",
        "a079f784704ff034_gr_session_id",
        "a079f784704ff034_gr_cs1",
    )
    parts = []
    for name in cookie_names:
        value = str(cookies.get(name, "")).strip()
        if value:
            parts.append(f"{name}={value}")

    weixin_token = str(cookies.get("weixin_access_token", "")).strip()
    if weixin_token:
        parts.append(f"weixin_access_token_RELEASE_5Yi1X={weixin_token}")
    return "; ".join(parts)


def extract_cookie_value(cookie_str, name):
    for part in (cookie_str or "").split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == name:
            return value.strip()
    return ""


def extract_csrf_from_cookie(cookie_str):
    csrf_cookie = extract_cookie_value(cookie_str, "_csrf")
    if not csrf_cookie:
        return ""

    decoded = unquote(csrf_cookie)
    serialized_values = re.findall(r's:\d+:"([^"]*)"', decoded)
    for candidate in reversed(serialized_values):
        if candidate and candidate != "_csrf":
            return candidate

    if re.fullmatch(r"[A-Za-z0-9_-]{16,}", decoded):
        return decoded
    return ""


def extract_csrf_from_page_text(text):
    patterns = (
        r'var\s+_csrf\s*=\s*["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
        r'<input[^>]+name=["\']_csrf["\'][^>]+value=["\']([^"\']+)["\']',
        r'"_csrf"\s*:\s*"([^"]+)"',
    )
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1)
    return ""


def cache_csrf_token(cache_key, token, proxy=None, timestamp=None):
    with csrf_cache_lock:
        csrf_cache[cache_key] = {
            "token": token,
            "timestamp": timestamp or time.time(),
            "proxy": proxy,
        }


def build_http_session(proxy=None):
    session = requests.Session()
    session.trust_env = False
    adapter = HTTPAdapter(
        pool_connections=SESSION_POOL_SIZE,
        pool_maxsize=SESSION_POOL_SIZE,
        max_retries=0,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    if CA_BUNDLE_PATH:
        session.verify = CA_BUNDLE_PATH
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def get_thread_session(proxy=None):
    if not hasattr(thread_local, "sessions"):
        thread_local.sessions = {}

    key = proxy or "__direct__"
    now = time.time()
    sessions = thread_local.sessions
    current = sessions.get(key)
    if current is None:
        current = {"session": build_http_session(proxy), "last_used_at": now}
        sessions[key] = current
    else:
        current["last_used_at"] = now

    stale_keys = []
    for session_key, item in sessions.items():
        if session_key == key:
            continue
        if (now - item.get("last_used_at", now)) > SESSION_IDLE_TTL_SECONDS:
            stale_keys.append(session_key)

    if len(sessions) - len(stale_keys) > MAX_THREAD_SESSIONS:
        overflow = sorted(
            (
                session_key
                for session_key in sessions.keys()
                if session_key != key and session_key not in stale_keys
            ),
            key=lambda session_key: sessions[session_key].get("last_used_at", 0.0),
        )
        stale_keys.extend(
            overflow[: max(0, len(sessions) - len(stale_keys) - MAX_THREAD_SESSIONS)]
        )

    for stale_key in stale_keys:
        stale = sessions.pop(stale_key, None)
        if stale is None:
            continue
        try:
            stale["session"].close()
        except Exception:
            pass

    return current["session"]


def close_thread_sessions():
    sessions = getattr(thread_local, "sessions", None)
    if not sessions:
        return

    for item in sessions.values():
        try:
            item["session"].close()
        except Exception:
            pass
    thread_local.sessions = {}


def get_proxy_cache_key(account):
    return (account.get("id") or account.get("name") or "default_proxy").strip()


def get_proxy_state(account):
    cache_key = get_proxy_cache_key(account)
    with proxy_cache_lock:
        state = proxy_cache.get(cache_key)
        if state is None:
            state = build_proxy_state()
            proxy_cache[cache_key] = state
        return cache_key, state


def invalidate_csrf_cache(cache_key):
    with csrf_cache_lock:
        csrf_cache.pop(cache_key, None)


def invalidate_proxy_for_account(account, activity_id=None):
    _cache_key, state = get_proxy_state(account)
    with proxy_cache_lock:
        state["value"] = None
        state["expires_at"] = 0.0
        state["version"] += 1


def parse_proxy_from_response(raw_text):
    try:
        payload = json.loads(raw_text)
    except (TypeError, ValueError):
        payload = None

    if isinstance(payload, dict):
        items = payload.get("data") or []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                ip = str(item.get("ip") or "").strip()
                port = str(item.get("port") or "").strip()
                if ip and port:
                    return f"http://{ip}:{port}"

    for line in raw_text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        match = re.search(r"((?:https?://)?[A-Za-z0-9.-]+:\d+)", candidate)
        if not match:
            continue
        proxy = match.group(1)
        if not proxy.startswith("http://") and not proxy.startswith("https://"):
            proxy = f"http://{proxy}"
        return proxy
    return None


def extract_ip_text(raw_text):
    candidate = str(raw_text or "").strip()
    match = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", candidate)
    return match.group(0) if match else None


def build_whitelist_url(template_url, ip_value):
    parts = urlsplit(template_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    updated = []
    replaced = False
    for key, value in query:
        if key == "ip":
            updated.append((key, ip_value))
            replaced = True
        else:
            updated.append((key, value))
    if not replaced:
        updated.append(("ip", ip_value))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(updated), parts.fragment)
    )


def is_proxy_auth_failure(error):
    message = str(error or "")
    normalized = message.upper()
    return "401 PROXY 401 RET" in normalized or "PROXYAUTHORIZATIONERROR" in normalized


def get_public_ip(timeout_seconds, check_urls=None):
    session = build_http_session()
    try:
        for url in check_urls or DEFAULT_PUBLIC_IP_CHECK_URLS:
            try:
                response = session.get(url, timeout=(3, timeout_seconds))
                response.raise_for_status()
                ip_value = extract_ip_text(response.text)
                if ip_value:
                    return ip_value
            except Exception as exc:
                logger.warning("公网 IP 检测失败: %s -> %s", url, exc)
        raise ValueError("无法检测当前公网出口 IP")
    finally:
        session.close()


def refresh_proxy_whitelist(config, force=False):
    settings = resolve_engine_settings(config)
    if not settings["proxy_whitelist_enabled"] or not settings["proxy_whitelist_url"]:
        return None

    timeout_seconds = min(
        settings["request_timeout"], settings["proxy_whitelist_timeout_seconds"]
    )
    public_ip = get_public_ip(timeout_seconds, settings["public_ip_check_urls"])
    now = time.time()

    with proxy_whitelist_lock:
        if (
            not force
            and proxy_whitelist_state.get("ip") == public_ip
            and proxy_whitelist_state.get("expires_at", 0.0) > now
        ):
            return public_ip

    request_url = build_whitelist_url(settings["proxy_whitelist_url"], public_ip)
    session = build_http_session()
    try:
        response = session.get(request_url, timeout=(3, timeout_seconds))
        response.raise_for_status()
        body = response.text.strip()
        lowered = body.lower()
        if any(keyword in lowered for keyword in ("fail", "error", "false")):
            raise ValueError(f"白名单接口返回异常: {body[:160]}")
    finally:
        session.close()

    with proxy_whitelist_lock:
        proxy_whitelist_state.update(
            {
                "ip": public_ip,
                "refreshed_at": now,
                "expires_at": now + settings["proxy_whitelist_cache_seconds"],
                "last_error": None,
            }
        )
    logger.info("代理白名单已刷新: %s", public_ip)
    return public_ip


def fetch_proxy_from_api(api_url, timeout_seconds):
    session = build_http_session()
    try:
        response = session.get(api_url, timeout=(3, timeout_seconds))
        response.raise_for_status()
        proxy = parse_proxy_from_response(response.text)
        if not proxy:
            raise ValueError(f"代理接口返回无法解析: {response.text[:120]}")
        return proxy
    finally:
        session.close()


def get_account_proxy(config, account):
    settings = resolve_engine_settings(config)
    if not settings["proxy_enabled"] or not settings["proxy_api_url"]:
        return None

    cache_key, state = get_proxy_state(account)
    now = time.time()
    with proxy_cache_lock:
        if state["value"] and state["expires_at"] > now:
            return state["value"]

        if now < state.get("fail_until", 0):
            return state["value"]

        if (now - state.get("last_fetch_at", 0)) < settings[
            "proxy_min_fetch_interval_seconds"
        ]:
            return state["value"]

    try:
        refresh_proxy_whitelist(config)
        proxy = fetch_proxy_from_api(
            settings["proxy_api_url"], settings["request_timeout"]
        )
    except Exception as exc:
        logger.warning("代理提取失败: %s", exc)
        with proxy_cache_lock:
            state["fail_until"] = now + settings["proxy_failure_backoff_seconds"]
            state["last_fetch_at"] = now
            return state["value"]

    with proxy_cache_lock:
        state.update(
            {
                "value": proxy,
                "expires_at": now + settings["proxy_ttl_seconds"],
                "source": settings["proxy_api_url"],
                "last_fetch_at": now,
                "fail_until": 0.0,
                "version": state.get("version", 0) + 1,
            }
        )
    invalidate_csrf_cache(f"{cache_key}:{DEFAULT_ACTIVITY_ID}")
    logger.info("账号代理已刷新: %s -> %s", cache_key, proxy)
    return proxy


def resolve_proxy_for_account(config, account):
    custom_proxy = (account.get("proxy") or "").strip()
    if custom_proxy:
        return custom_proxy
    return get_account_proxy(config, account)


def fetch_activity_page(
    session, cookie_str, timeout_seconds, user_agent=DEFAULT_USER_AGENT
):
    return session.get(
        ACTIVITY_PAGE_URL,
        params={"activity_id": DEFAULT_ACTIVITY_ID},
        headers={"User-Agent": user_agent, "Cookie": cookie_str},
        timeout=(3, timeout_seconds),
    )


def get_cached_csrf_entry(cache_key, max_age=None):
    now = time.time()
    with csrf_cache_lock:
        cached = csrf_cache.get(cache_key)

    if not cached or not cached.get("token"):
        return None

    try:
        timestamp = float(cached.get("timestamp") or 0.0)
    except (TypeError, ValueError):
        timestamp = 0.0
    age_seconds = max(0.0, now - timestamp) if timestamp else float("inf")

    if max_age is not None and age_seconds > max(1, float(max_age)):
        return None

    return {
        "token": cached["token"],
        "timestamp": timestamp,
        "age_seconds": age_seconds,
        "proxy": cached.get("proxy"),
    }


def get_csrf_from_page(
    session,
    activity_id,
    cookie_str,
    user_agent,
    cache_key,
    timeout_seconds,
    connect_timeout=3,
    proxy=None,
    cache_seconds=20,
):
    cached = get_cached_csrf_entry(cache_key, max_age=cache_seconds)
    if cached:
        return cached["token"]

    now = time.time()
    cookie_csrf = extract_csrf_from_cookie(cookie_str)

    try:
        params = {"activity_id": activity_id}
        headers = {"User-Agent": user_agent, "Cookie": cookie_str}
        response = session.get(
            ACTIVITY_PAGE_URL,
            params=params,
            headers=headers,
            timeout=(connect_timeout, timeout_seconds),
        )
        if response.status_code == 200:
            token = extract_csrf_from_page_text(response.text)
            if token:
                cache_csrf_token(cache_key, token, proxy=proxy, timestamp=now)
                return token
            resp_text = response.text or ""
            waf_page = "aliyun_waf" in resp_text.lower() or "acw_sc__v3" in resp_text.lower() or "nocaptcha" in resp_text.lower()
            if waf_page:
                logger.error(
                    "[CSRF] 阿里云 WAF 滑块拦截！页面被 WAF 劫持，需要在 raw_cookie 中添加 acw_sc__v3。"
                    " 请手动浏览器访问活动页面，过滑块后从 Cookie 中复制 acw_sc__v3 值。status=%s proxy=%s",
                    response.status_code,
                    proxy,
                )
            if cookie_csrf:
                logger.warning(
                    "[CSRF] 页面未暴露 token，使用 Cookie 中的 _csrf token status=%s waf=%s proxy=%s",
                    response.status_code,
                    waf_page,
                    proxy,
                )
                cache_csrf_token(cache_key, cookie_csrf, proxy=proxy, timestamp=now)
                return cookie_csrf
    except Exception as exc:
        if proxy and is_proxy_auth_failure(exc):
            raise ProxyAuthFailure(str(exc)) from exc
        logger.warning("[CSRF ERROR] %s", exc)
        if cookie_csrf:
            logger.warning("[CSRF] 页面请求失败，使用 Cookie 中的 _csrf token proxy=%s", proxy)
            cache_csrf_token(cache_key, cookie_csrf, proxy=proxy, timestamp=now)
            return cookie_csrf
    return ""


def build_headers(cookie_str):
    return {
        "Host": "sz.inboyu.com",
        "Accept": "application/json, text/plain, */*",
        "Sec-Fetch-Site": "same-origin",
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Mode": "cors",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://sz.inboyu.com",
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": "https://sz.inboyu.com/activity/graduate",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Cookie": cookie_str,
    }


def build_runtime(config, payload):
    interval, max_count, bark_enabled = resolve_runtime_settings(
        config,
        payload.get("requested_interval", 1.0),
        payload.get("requested_max_count", 0),
    )
    engine = resolve_engine_settings(config)

    projects = []
    if payload.get("project_id1"):
        projects.append((payload["project_id1"], payload.get("name1", "房源1")))
    if payload.get("project_id2"):
        projects.append((payload["project_id2"], payload.get("name2", "房源2")))

    target_time = (payload.get("target_time") or "").strip()

    return {
        "activity_id": DEFAULT_ACTIVITY_ID,
        "account_id": payload.get("account_id", ""),
        "projects": projects,
        "base_interval": interval,
        "interval": interval,
        "max_count": max_count,
        "bark_enabled": bark_enabled,
        "max_workers": engine["max_workers"],
        "per_account_max_inflight": engine["per_account_max_inflight"],
        "base_request_timeout": engine["request_timeout"],
        "request_timeout": engine["request_timeout"],
        "connect_timeout": min(3.0, max(1.0, engine["request_timeout"] / 2)),
        "start_delay_ms": engine["start_delay_ms"],
        "csrf_cache_seconds": int(config.get("csrf_cache_seconds", 24)),
        "csrf_stale_fallback_enabled": bool(
            config.get("csrf_stale_fallback_enabled", True)
        ),
        "csrf_stale_fallback_seconds": int(
            config.get(
                "csrf_stale_fallback_seconds", DEFAULT_CSRF_STALE_FALLBACK_SECONDS
            )
        ),
        "auto_schedule_enabled": bool(payload.get("auto_schedule_enabled", False)),
        "target_time": target_time,
        "target_open_at": build_target_datetime(target_time) if target_time else None,
        "keepalive_interval": float(config.get("keepalive_interval_seconds", 6.0)),
        "warmup_interval": float(config.get("warmup_interval_seconds", 1.0)),
        "peak_interval": float(config.get("peak_interval_seconds", 0.5)),
        "cooldown_interval": float(config.get("cooldown_interval_seconds", 1.0)),
        "warmup_before_seconds": int(config.get("warmup_before_seconds", 420)),
        "peak_after_seconds": int(config.get("peak_after_seconds", 420)),
        "cooldown_after_seconds": int(config.get("cooldown_after_seconds", 900)),
        "success_stop_threshold": int(config.get("success_stop_threshold", 10)),
        "stop_after_first_success": bool(config.get("stop_after_first_success", True)),
        "project_id1": payload.get("project_id1", ""),
        "name1": payload.get("name1", "房源1"),
        "project_id2": payload.get("project_id2", ""),
        "name2": payload.get("name2", "房源2"),
    }


def build_payload_from_request_args():
    auto_schedule_value = (
        (request.args.get("auto_schedule_enabled", "") or "").strip().lower()
    )
    return {
        "project_id1": request.args.get("id1", ""),
        "name1": request.args.get("name1", "房源1"),
        "project_id2": request.args.get("id2", ""),
        "name2": request.args.get("name2", "房源2"),
        "requested_interval": float(request.args.get("interval", "1")),
        "requested_max_count": int(request.args.get("max_count", "0")),
        "account_id": request.args.get("account_id", ""),
        "client_run_id": request.args.get("client_run_id", ""),
        "auto_schedule_enabled": auto_schedule_value in ("1", "true", "yes", "on"),
        "target_time": request.args.get("target_time", ""),
    }


def build_error_event(name, message):
    return {
        "name": name,
        "error": message,
        "time": datetime.now().strftime("%H:%M:%S"),
    }


def build_info_event(name, message, **extra):
    event = {
        "name": name,
        "status": "info",
        "message": message,
        "time": datetime.now().strftime("%H:%M:%S"),
    }
    event.update(extra)
    return event


def cancel_pending_attempts(pending):
    for item in list(pending.values()):
        for future in list(item.get("remaining", ())):
            if not future.done():
                future.cancel()
        item["remaining"] = set()
    pending.clear()


def execute_booking_attempt(
    account,
    project_id,
    project_name,
    count,
    runtime,
    config,
    forced_proxy=UNSET_PROXY,
    whitelist_retried=False,
    retry_count=0,
):
    account_name = account.get("name", "未知账号")
    display_name = f"{account_name}-{project_name}"
    timeout_seconds = runtime["request_timeout"]
    connect_timeout = runtime.get(
        "connect_timeout", min(3.0, max(1.0, timeout_seconds / 2))
    )
    proxy = (
        forced_proxy
        if forced_proxy is not UNSET_PROXY
        else resolve_proxy_for_account(config, account)
    )
    started_at = time.time()
    account_cache_key = get_proxy_cache_key(account)
    cache_key = f"{account_cache_key}:{runtime['activity_id']}"

    try:
        user = account["user"]
        cookie_str = build_cookie_str(account["cookies"])
        session = get_thread_session(proxy)
        headers = build_headers(cookie_str)
        csrf_source = "fresh"
        csrf_token = get_csrf_from_page(
            session,
            runtime["activity_id"],
            cookie_str,
            headers["User-Agent"],
            cache_key=cache_key,
            timeout_seconds=timeout_seconds,
            connect_timeout=connect_timeout,
            proxy=proxy,
            cache_seconds=runtime.get("csrf_cache_seconds", 20),
        )
        if not csrf_token and proxy:
            invalidate_proxy_for_account(account, runtime["activity_id"])
            proxy = resolve_proxy_for_account(config, account)
            session = get_thread_session(proxy)
            csrf_token = get_csrf_from_page(
                session,
                runtime["activity_id"],
                cookie_str,
                headers["User-Agent"],
                cache_key=cache_key,
                timeout_seconds=timeout_seconds,
                connect_timeout=connect_timeout,
                proxy=proxy,
                cache_seconds=runtime.get("csrf_cache_seconds", 20),
            )

        if not csrf_token and runtime.get("csrf_stale_fallback_enabled", True):
            cached_entry = get_cached_csrf_entry(
                cache_key, max_age=runtime.get("csrf_stale_fallback_seconds", 900)
            )
            if cached_entry:
                csrf_token = cached_entry["token"]
                csrf_source = "stale_cache"
                logger.warning(
                    "[%s] 新取 CSRF 失败，回退使用 %.1f 秒前缓存的 CSRF interval=%s proxy=%s retry=%s",
                    display_name,
                    cached_entry["age_seconds"],
                    runtime.get("interval"),
                    proxy,
                    retry_count,
                )

        if not csrf_token:
            logger.warning(
                "[%s] CSRF 获取失败，取消本次提交 interval=%s proxy=%s retry=%s",
                display_name,
                runtime.get("interval"),
                proxy,
                retry_count,
            )
            return finalize_attempt_result(
                {
                    "count": count,
                    "name": display_name,
                    "error": "未获取到 CSRF，已跳过本次提交",
                    "reason": "csrf_missing",
                    "csrf_source": csrf_source,
                    "time": datetime.now().strftime("%H:%M:%S"),
                },
                runtime,
                started_at,
                False,
                retry_count,
                proxy,
            )

        form_data = {
            "name": user["name"],
            "card_num": user["card_num"],
            "phone": user["phone"],
            "native_place": user["native_place"],
            "project_id": project_id,
            "house_type": user["house_type"],
            "intoStore": "",
            "check_in_date": user["check_in_date"],
            "school": user["school"],
            "major": user["major"],
            "graduation_date": user["graduation_date"],
            "graduation_certificate": "",
            "xxw_code": user["xxw_code"],
            "intended_industry": "",
            "intended_ent": user["intended_ent"],
            "intended_city": user["intended_city"],
            "activity_id": runtime["activity_id"],
            "_csrf": csrf_token,
        }

        response = session.post(
            BOOKING_URL,
            data=form_data,
            headers=headers,
            timeout=(connect_timeout, timeout_seconds),
        )

        if response.status_code in RECOVERABLE_RETRY_STATUSES:
            invalidate_csrf_cache(cache_key)
            if proxy:
                invalidate_proxy_for_account(account, runtime["activity_id"])
            if should_inline_retry(
                runtime, retry_count, status_code=response.status_code
            ):
                logger.warning(
                    "[%s] 命中可恢复状态码 %s，立即刷新代理和 CSRF 后重试 interval=%s proxy=%s",
                    display_name,
                    response.status_code,
                    runtime.get("interval"),
                    proxy,
                )
                return execute_booking_attempt(
                    account,
                    project_id,
                    project_name,
                    count,
                    runtime,
                    config,
                    forced_proxy=UNSET_PROXY,
                    whitelist_retried=whitelist_retried,
                    retry_count=retry_count + 1,
                )
            logger.warning(
                "[%s] 命中可恢复状态码 %s，已刷新代理等待下一轮 interval=%s proxy=%s",
                display_name,
                response.status_code,
                runtime.get("interval"),
                proxy,
            )

        if response.status_code in (401, 403):
            if runtime["bark_enabled"]:
                send_bark_async("Cookie过期", f"{display_name} - 需要更新Cookie")
            return finalize_attempt_result(
                {
                    "count": count,
                    "name": display_name,
                    "error": "Cookie已过期",
                    "reason": "expired",
                    "token_expired": True,
                    "status": response.status_code,
                    "time": datetime.now().strftime("%H:%M:%S"),
                },
                runtime,
                started_at,
                True,
                retry_count,
                proxy,
            )

        if response.status_code in THROTTLE_STATUSES:
            invalidate_proxy_for_account(account, runtime["activity_id"])

        try:
            parsed_json = response.json()
            errmsg = parsed_json.get("errmsg", "")
            is_success = parsed_json.get("errcode") == 0 or any(
                keyword in errmsg for keyword in ("审批中", "审核中", "已提交报名")
            )
            if is_success and runtime["bark_enabled"]:
                send_bark_async("🎉抢房成功", f"{display_name} - {errmsg}")
            parsed = {
                "type": "json",
                "errcode": parsed_json.get("errcode"),
                "errmsg": errmsg,
                "real_success": is_success,
            }
        except ValueError:
            reason, message = classify_booking_html_response(response)
            title = extract_html_title(response.text) or "N/A"
            if reason in ("expired", "redirected"):
                if runtime["bark_enabled"]:
                    send_bark_async("Cookie过期", f"{display_name} - {message}")
                return finalize_attempt_result(
                    {
                        "count": count,
                        "name": display_name,
                        "error": message,
                        "reason": reason,
                        "token_expired": True,
                        "status": response.status_code,
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "parsed": {
                            "type": "html",
                            "title": title,
                            "real_success": False,
                        },
                    },
                    runtime,
                    started_at,
                    True,
                    retry_count,
                    proxy,
                )
            parsed = {"type": "html", "title": title, "real_success": False}
            result = {
                "count": count,
                "name": display_name,
                "status": response.status_code,
                "time": datetime.now().strftime("%H:%M:%S"),
                "reason": reason,
                "error": message,
                "csrf_source": csrf_source,
                "parsed": parsed,
            }
            return finalize_attempt_result(
                result, runtime, started_at, True, retry_count, proxy
            )

        result = {
            "count": count,
            "name": display_name,
            "status": response.status_code,
            "time": datetime.now().strftime("%H:%M:%S"),
            "csrf_source": csrf_source,
            "parsed": parsed,
        }
        return finalize_attempt_result(
            result, runtime, started_at, True, retry_count, proxy
        )
    except Exception as exc:
        if proxy and not whitelist_retried and is_proxy_auth_failure(exc):
            logger.warning("[%s] 代理鉴权失败，刷新白名单后重试: %s", display_name, exc)
            try:
                refresh_proxy_whitelist(config, force=True)
            except Exception as refresh_exc:
                logger.warning("[%s] 白名单刷新失败: %s", display_name, refresh_exc)
            invalidate_proxy_for_account(account, runtime["activity_id"])
            return execute_booking_attempt(
                account,
                project_id,
                project_name,
                count,
                runtime,
                config,
                forced_proxy=UNSET_PROXY,
                whitelist_retried=True,
                retry_count=retry_count,
            )
        if should_retry_request_exception(exc):
            invalidate_proxy_for_account(account, runtime["activity_id"])
            if should_inline_retry(runtime, retry_count):
                logger.warning(
                    "[%s] 请求异常可恢复，立即重试 interval=%s proxy=%s error=%s",
                    display_name,
                    runtime.get("interval"),
                    proxy,
                    exc,
                )
                return execute_booking_attempt(
                    account,
                    project_id,
                    project_name,
                    count,
                    runtime,
                    config,
                    forced_proxy=UNSET_PROXY,
                    whitelist_retried=whitelist_retried,
                    retry_count=retry_count + 1,
                )
            logger.warning(
                "[%s] 请求异常可恢复，已切换代理等待下一轮 interval=%s proxy=%s error=%s",
                display_name,
                runtime.get("interval"),
                proxy,
                exc,
            )
            return finalize_attempt_result(
                {
                    "count": count,
                    "name": display_name,
                    "error": str(exc),
                    "reason": "recoverable_exception",
                    "csrf_source": locals().get("csrf_source", ""),
                    "time": datetime.now().strftime("%H:%M:%S"),
                },
                runtime,
                started_at,
                bool(locals().get("csrf_token")),
                retry_count,
                proxy,
            )
        logger.error("[%s] 请求异常: %s", display_name, exc)
        invalidate_proxy_for_account(account, runtime["activity_id"])
        return finalize_attempt_result(
            {
                "count": count,
                "name": display_name,
                "error": str(exc),
                "time": datetime.now().strftime("%H:%M:%S"),
            },
            runtime,
            started_at,
            False,
            retry_count,
            proxy,
        )


def iter_booking_events(runtime, stop_event=None, include_heartbeat=False):
    config = load_config()
    selected_accounts = resolve_selected_accounts(config, runtime["account_id"])
    if not selected_accounts:
        yield build_error_event("系统", "没有启用的账号")
        return

    if not runtime["projects"]:
        yield build_error_event("系统", "未选择任何房源")
        return

    executor = ThreadPoolExecutor(
        max_workers=runtime["max_workers"],
        thread_name_prefix="grab-worker",
    )
    rounds_started = 0
    last_heartbeat = time.time()
    last_interval_signature = None
    account_states = {}
    pending = {}
    global_stop_reason = None
    abort_immediately = False

    logger.info("========== 抢房任务开始 ==========")
    logger.info(
        "账号数量: %s, 房源数量: %s, 间隔: %s秒, 最大次数: %s, 最大并发: %s",
        len(selected_accounts),
        len(runtime["projects"]),
        runtime["interval"],
        runtime["max_count"] if runtime["max_count"] > 0 else "无限制",
        runtime["max_workers"],
    )

    if runtime["start_delay_ms"] > 0:
        time.sleep(runtime["start_delay_ms"] / 1000.0)

    try:
        while not (stop_event and stop_event.is_set()):
            config = load_config()
            selected_accounts = resolve_selected_accounts(config, runtime["account_id"])
            if not selected_accounts:
                yield build_error_event("系统", "当前没有可用的启用账号，任务已停止")
                return

            interval, max_count, bark_enabled = resolve_runtime_settings(
                config,
                runtime.get("base_interval", runtime["interval"]),
                runtime["max_count"],
            )
            runtime["base_interval"] = interval
            runtime["max_count"] = max_count
            runtime["bark_enabled"] = bark_enabled

            now = time.time()
            runtime["interval"] = resolve_effective_interval(runtime, now)
            runtime["request_timeout"] = resolve_effective_request_timeout(runtime, now)
            runtime["connect_timeout"] = resolve_connect_timeout(runtime)
            runtime["account_inflight_limit"] = resolve_effective_account_inflight_limit(
                runtime, now
            )
            interval_signature = (
                round(float(runtime["interval"]), 3),
                describe_effective_interval_phase(runtime, now),
                round(float(runtime["request_timeout"]), 2),
                int(runtime["account_inflight_limit"]),
            )
            if interval_signature != last_interval_signature:
                phase = interval_signature[1]
                target_open_at = runtime.get("target_open_at")
                target_label = (
                    target_open_at.strftime("%H:%M:%S")
                    if isinstance(target_open_at, datetime)
                    else "N/A"
                )
                interval_message = (
                    f"调频切换: phase={phase} interval={runtime['interval']}s "
                    f"timeout={runtime['request_timeout']}s inflight={runtime['account_inflight_limit']} "
                    f"target={target_label}"
                )
                logger.info("[SCHEDULE] %s", interval_message)
                if runtime.get("auto_schedule_enabled"):
                    yield build_info_event("系统", interval_message)
                last_interval_signature = interval_signature
            spread = max(runtime["interval"] / max(len(selected_accounts), 1), 0.05)
            selected_ids = {
                get_proxy_cache_key(account) for account in selected_accounts
            }
            scheduling_enabled = not global_stop_reason

            for stale_key in list(account_states.keys()):
                still_pending = any(
                    item.get("account_key") == stale_key for item in pending.values()
                )
                if stale_key not in selected_ids and not still_pending:
                    account_states.pop(stale_key, None)

            all_accounts_finished = runtime["max_count"] > 0 or not scheduling_enabled
            for index, account in enumerate(selected_accounts):
                account_key = get_proxy_cache_key(account)
                state = account_states.get(account_key)
                if state is None:
                    state = {
                        "next_run_at": now + (index * spread),
                        "round_count": 0,
                        "success_hits": 0,
                        "stopped": False,
                        "stop_reason": "",
                        "throttled": False,
                    }
                    account_states[account_key] = state

                if state.get("stopped"):
                    continue

                if not scheduling_enabled:
                    continue

                if (
                    runtime["max_count"] == 0
                    or state["round_count"] < runtime["max_count"]
                ):
                    all_accounts_finished = False

                inflight_count = 0
                for item in pending.values():
                    if item.get("account_key") == account_key:
                        inflight_count += 1
                if inflight_count >= runtime["account_inflight_limit"]:
                    continue

                if (
                    runtime["max_count"] > 0
                    and state["round_count"] >= runtime["max_count"]
                ):
                    continue

                if now < state["next_run_at"]:
                    continue

                rounds_started += 1
                state["round_count"] += 1
                round_proxy = resolve_proxy_for_account(config, account)
                futures = []
                for project_id, project_name in runtime["projects"]:
                    futures.append(
                        executor.submit(
                            execute_booking_attempt,
                            account,
                            project_id,
                            project_name,
                            state["round_count"],
                            dict(runtime),
                            config,
                            round_proxy,
                        )
                    )
                pending_key = f"{account_key}:{state['round_count']}:{time.time_ns()}"
                pending[pending_key] = {
                    "account_key": account_key,
                    "account": account,
                    "futures": futures,
                    "remaining": set(futures),
                    "due_total": len(futures),
                    "done_count": 0,
                    "throttled": False,
                }
                state["next_run_at"] = now + runtime["interval"]

            if all_accounts_finished and not pending:
                break

            if pending:
                future_map = {}
                for pending_key, item in pending.items():
                    for future in item["remaining"]:
                        future_map[future] = pending_key

                if future_map:
                    done, _ = wait(
                        list(future_map.keys()),
                        timeout=0.2,
                        return_when=FIRST_COMPLETED,
                    )
                else:
                    done = []

                for future in done:
                    pending_key = future_map[future]
                    item = pending.get(pending_key)
                    if item is None:
                        continue
                    result = future.result()
                    item["remaining"].discard(future)
                    item["done_count"] += 1
                    status = result.get("status")
                    account_key = item.get("account_key")
                    state = account_states.get(account_key)
                    if status in THROTTLE_STATUSES:
                        item["throttled"] = True

                    parsed = result.get("parsed") or {}
                    if state and parsed.get("real_success"):
                        state["success_hits"] += 1
                        result["success_hits"] = state["success_hits"]
                        threshold = max(
                            1, int(runtime.get("success_stop_threshold", 10))
                        )
                        if state["success_hits"] >= threshold:
                            state["stopped"] = True
                            state["stop_reason"] = (
                                f"成功样式响应累计达到 {threshold} 次，自动停止"
                            )
                            result["auto_stopped"] = True
                            result["terminal"] = True
                            result["stop_reason"] = state["stop_reason"]
                            global_stop_reason = f"{item['account'].get('name', account_key)} 达到成功阈值，任务自动停止"
                            if (
                                runtime.get("stop_after_first_success", True)
                                and stop_event
                            ):
                                stop_event.set()
                                abort_immediately = True
                                cancel_pending_attempts(pending)
                    elif state and result.get("token_expired"):
                        state["stopped"] = True
                        state["stop_reason"] = "Cookie已过期，停止该账号后续请求"
                    yield result

                    if item["done_count"] >= item["due_total"]:
                        if state and item["throttled"]:
                            state["next_run_at"] = max(
                                state["next_run_at"], time.time() + 1.5
                            )
                        pending.pop(pending_key, None)
                if not done:
                    time.sleep(0.05)
            else:
                time.sleep(0.05)

            if (
                include_heartbeat
                and time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS
            ):
                yield {"type": "heartbeat"}
                last_heartbeat = time.time()

            if global_stop_reason and not pending:
                yield build_info_event("系统", global_stop_reason, terminal=True)
                break

            if stop_event and stop_event.is_set():
                abort_immediately = True
                cancel_pending_attempts(pending)
                break
    finally:
        try:
            executor.shutdown(
                wait=not abort_immediately, cancel_futures=abort_immediately
            )
        except TypeError:
            # Python 3.7 does not support cancel_futures.
            executor.shutdown(wait=not abort_immediately)
        logger.info("========== 抢房任务结束 ==========")


def log_background_event(job_id, event):
    if event.get("type") == "heartbeat":
        logger.info("[%s] heartbeat", job_id)
        return
    if event.get("error"):
        logger.warning(
            "[%s] %s - reason=%s error=%s interval=%s duration_ms=%s retry=%s proxy=%s",
            job_id,
            event.get("name"),
            event.get("reason", ""),
            event.get("error"),
            event.get("interval"),
            event.get("duration_ms"),
            event.get("retry_count"),
            event.get("proxy"),
        )
        return
    parsed = event.get("parsed") or {}
    logger.info(
        "[%s] %s - status=%s errmsg=%s stop_reason=%s interval=%s duration_ms=%s retry=%s proxy=%s",
        job_id,
        event.get("name"),
        event.get("status"),
        parsed.get("errmsg", ""),
        event.get("stop_reason", ""),
        event.get("interval"),
        event.get("duration_ms"),
        event.get("retry_count"),
        event.get("proxy"),
    )


def start_scheduled_run(job_id, payload):
    with scheduled_runs_lock:
        existing = scheduled_runs.get(job_id)
        if existing and existing["thread"].is_alive():
            logger.info("[%s] 已在运行，跳过本次定时触发", job_id)
            return

        stop_event = threading.Event()
        thread = threading.Thread(
            target=run_scheduled_booking,
            args=(job_id, payload, stop_event),
            daemon=True,
        )
        scheduled_runs[job_id] = {"thread": thread, "stop_event": stop_event}
        thread.start()


def run_scheduled_booking(job_id, payload, stop_event):
    try:
        config = load_config()
        runtime = build_runtime(config, payload)
        if runtime.get("auto_schedule_enabled") and runtime.get("target_open_at"):
            logger.info(
                "[%s] 自动模式已启用：目标=%s 保活=%ss 预热=%ss 准点=%ss 冷却=%ss",
                job_id,
                runtime["target_open_at"].strftime("%H:%M:%S"),
                runtime["keepalive_interval"],
                runtime["warmup_interval"],
                runtime["peak_interval"],
                runtime["cooldown_interval"],
            )
        for event in iter_booking_events(
            runtime, stop_event=stop_event, include_heartbeat=False
        ):
            log_background_event(job_id, event)
    finally:
        close_thread_sessions()
        with scheduled_runs_lock:
            existing = scheduled_runs.get(job_id)
            if existing and existing.get("stop_event") is stop_event:
                scheduled_runs.pop(job_id, None)


def stop_all_scheduled_runs():
    with scheduled_runs_lock:
        runs = list(scheduled_runs.values())
    for item in runs:
        item["stop_event"].set()


def parse_schedule_time(value):
    parts = value.split(":")
    hour = int(parts[0])
    minute = int(parts[1])
    second = int(float(parts[2])) if len(parts) > 2 else 0
    return hour, minute, second


@app.route("/")
def index():
    response = make_response(render_template("index.html"))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/test")
def test_page():
    return jsonify({"errcode": 0, "errmsg": "archive build: test page removed"})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify(load_config())

    old_config = load_config()
    new_config = request.json or {}
    for key, value in new_config.items():
        if key != "accounts":
            old_config[key] = value
    save_config(old_config)
    return jsonify({"errcode": 0, "errmsg": "ok"})


@app.route("/api/accounts", methods=["GET", "POST", "DELETE"])
def api_accounts():
    config = load_config()

    if request.method == "GET":
        return jsonify(config.get("accounts", []))

    if request.method == "POST":
        account = request.json or {}
        accounts = config.get("accounts", [])

        if "id" in account:
            for index, existing in enumerate(accounts):
                if existing.get("id") == account["id"]:
                    accounts[index] = account
                    break
            else:
                accounts.append(account)
        else:
            import uuid

            account["id"] = f"account_{uuid.uuid4().hex[:8]}"
            accounts.append(account)

        config["accounts"] = accounts
        save_config(config)
        return jsonify({"errcode": 0, "errmsg": "保存成功", "account": account})

    account_id = request.args.get("id")
    config["accounts"] = [
        account
        for account in config.get("accounts", [])
        if account.get("id") != account_id
    ]
    save_config(config)
    return jsonify({"errcode": 0, "errmsg": "删除成功"})


@app.route("/api/stop_manual_run", methods=["POST"])
def api_stop_manual_run():
    data = request.json or {}
    requested_run_id = (data.get("client_run_id") or "").strip() or None
    stopped, active_run_id = stop_manual_run(requested_run_id)
    if stopped:
        return jsonify({"errcode": 0, "errmsg": "ok", "run_id": active_run_id})
    return jsonify({"errcode": 0, "errmsg": "idle", "run_id": active_run_id})


@app.route("/api/test_cookie", methods=["POST"])
def api_test_cookie():
    data = request.json or {}
    account_id = data.get("account_id")

    config = load_config()
    account = next(
        (item for item in config.get("accounts", []) if item.get("id") == account_id),
        None,
    )
    if not account:
        return jsonify({"errcode": 1, "errmsg": "账号不存在", "valid": False})

    timeout_seconds = resolve_engine_settings(config)["request_timeout"]
    proxy = resolve_proxy_for_account(config, account)
    session = build_http_session(proxy)
    try:
        cookie_str = build_cookie_str(account.get("cookies", {}))
        if "PHPSESSID=" not in cookie_str:
            return jsonify(
                {
                    "errcode": 1,
                    "errmsg": "❌ Cookie缺少PHPSESSID，当前登录态无法验证",
                    "valid": False,
                    "reason": "missing_phpsessid",
                }
            )
        retried_auth = False
        while True:
            try:
                response = fetch_activity_page(session, cookie_str, timeout_seconds)
                break
            except Exception as exc:
                if proxy and not retried_auth and is_proxy_auth_failure(exc):
                    retried_auth = True
                    session.close()
                    refresh_proxy_whitelist(config, force=True)
                    invalidate_proxy_for_account(account, DEFAULT_ACTIVITY_ID)
                    proxy = resolve_proxy_for_account(config, account)
                    session = build_http_session(proxy)
                    continue
                raise

        if response.status_code in THROTTLE_STATUSES and proxy:
            session.close()
            invalidate_proxy_for_account(account, DEFAULT_ACTIVITY_ID)
            proxy = resolve_proxy_for_account(config, account)
            session = build_http_session(proxy)
            response = fetch_activity_page(session, cookie_str, timeout_seconds)

        is_valid, reason, message = classify_activity_page_response(response)
        return jsonify(
            {
                "errcode": 0 if is_valid else 1,
                "errmsg": message,
                "valid": is_valid,
                "reason": reason,
            }
        )
    except Exception as exc:
        return jsonify({"errcode": 1, "errmsg": f"测试失败: {exc}", "valid": False})
    finally:
        session.close()


@app.route("/api/activities")
def get_activities():
    activities = [
        {
            "id": "3a0d7aea-0e58-4ce3-c3d6-55068d910493",
            "title": "西丽湖国际科教城平山公寓（每日12:15开放）",
            "cover": "",
        },
        {
            "id": "39f76e56-e416-01e1-25a5-a8405f9727ba",
            "title": "南头古城青年驿站（每日14:00开放）",
            "cover": "",
        },
    ]
    return jsonify({"errcode": 0, "data": activities})


@app.route("/run_dual")
def run_dual():
    payload = build_payload_from_request_args()
    run_id = (
        payload.get("client_run_id") or ""
    ).strip() or f"manual-{int(time.time() * 1000)}"

    def generate():
        stop_event = threading.Event()
        replaced = replace_manual_run(run_id, stop_event)
        config = load_config()
        runtime = build_runtime(config, payload)
        runtime["client_run_id"] = run_id
        try:
            if replaced:
                replaced_message = f"检测到已有手动任务 {replaced['run_id']}，已停止旧任务并切换到当前任务"
                yield f"data: {json.dumps(build_info_event('系统', replaced_message), ensure_ascii=False)}\n\n"
            if runtime.get("auto_schedule_enabled") and runtime.get("target_open_at"):
                auto_message = (
                    f"手动自动调频已启用：目标={runtime['target_open_at'].strftime('%H:%M:%S')} "
                    f"保活={runtime['keepalive_interval']}s 预热={runtime['warmup_interval']}s "
                    f"准点={runtime['peak_interval']}s 冷却={runtime['cooldown_interval']}s"
                )
                yield f"data: {json.dumps(build_info_event('系统', auto_message), ensure_ascii=False)}\n\n"
            for event in iter_booking_events(
                runtime, stop_event=stop_event, include_heartbeat=True
            ):
                if event.get("type") == "heartbeat":
                    yield ": heartbeat\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            close_thread_sessions()
            clear_manual_run(run_id, stop_event)

    response = Response(stream_with_context(generate()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.route("/api/schedule", methods=["GET", "POST"])
def api_schedule():
    if request.method == "GET":
        jobs = scheduler.get_jobs()
        return jsonify(
            {
                "errcode": 0,
                "jobs": [
                    {"id": job.id, "next_run": str(job.next_run_time)} for job in jobs
                ],
            }
        )

    data = request.json or {}
    action = data.get("action")

    if action == "start":
        account_id = data.get("account_id", "")
        config = load_config()
        if not resolve_selected_accounts(config, account_id):
            return jsonify({"errcode": 1, "errmsg": "没有配置可用账号"})

        scheduler.remove_all_jobs()
        stop_all_scheduled_runs()

        try:
            lead_seconds = int(config.get("schedule_lead_seconds", 1800))
            xh_trigger = resolve_schedule_trigger_time(
                data.get("xilihu_time", "12:15:00"), lead_seconds
            )
            nt_trigger = resolve_schedule_trigger_time(
                data.get("nantou_time", "14:00:00"), lead_seconds
            )
            if xh_trigger is None or nt_trigger is None:
                raise ValueError("无法解析启动时间")
            xh_hour, xh_min, xh_sec = xh_trigger
            nt_hour, nt_min, nt_sec = nt_trigger
        except Exception as exc:
            return jsonify({"errcode": 1, "errmsg": f"时间格式错误: {exc}"})

        xilihu_payload = {
            "account_id": account_id,
            "project_id1": data.get("id1", ""),
            "name1": data.get("name1", "西丽湖"),
            "project_id2": "",
            "name2": "",
            "requested_interval": data.get("interval", 1.0),
            "requested_max_count": data.get("max_count", 0),
            "auto_schedule_enabled": True,
            "target_time": data.get("xilihu_time", "12:15:00"),
        }
        nantou_payload = {
            "account_id": account_id,
            "project_id1": data.get("id2", ""),
            "name1": data.get("name2", "南头古城"),
            "project_id2": "",
            "name2": "",
            "requested_interval": data.get("interval", 1.0),
            "requested_max_count": data.get("max_count", 0),
            "auto_schedule_enabled": True,
            "target_time": data.get("nantou_time", "14:00:00"),
        }

        if xilihu_payload["project_id1"]:
            scheduler.add_job(
                start_scheduled_run,
                CronTrigger(hour=xh_hour, minute=xh_min, second=xh_sec),
                args=("xilihu_job", xilihu_payload),
                id="xilihu_job",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )

        if nantou_payload["project_id1"]:
            scheduler.add_job(
                start_scheduled_run,
                CronTrigger(hour=nt_hour, minute=nt_min, second=nt_sec),
                args=("nantou_job", nantou_payload),
                id="nantou_job",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )

        return jsonify(
            {
                "errcode": 0,
                "errmsg": (
                    f"定时任务已启动：西丽湖提前 {lead_seconds // 60} 分钟预热，目标 {data.get('xilihu_time', '12:15:00')}；"
                    f"南头古城提前 {lead_seconds // 60} 分钟预热，目标 {data.get('nantou_time', '14:00:00')}"
                ),
            }
        )

    if action == "stop":
        scheduler.remove_all_jobs()
        stop_all_scheduled_runs()
        return jsonify({"errcode": 0, "errmsg": "定时任务已停止"})

    return jsonify({"errcode": 1, "errmsg": "未知操作"})


if __name__ == "__main__":
    import sys

    port = 5001
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except Exception:
            port = 5001

    log_file = f"qiangfang_{port}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    logger.info("========== 服务启动 ==========")
    logger.info("端口: %s, 日志文件: %s", port, log_file)
    app.run(host="0.0.0.0", port=port, debug=False)
