#!/usr/bin/env python3
"""海航官网国内单程、普通成人经济舱监控。Python 3.9+。"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / ".local"
TZ = ZoneInfo("Asia/Shanghai")
SEARCH_URL = "https://new.hnair.com/hainanair/ibe/common/flightSearch.do"
RULES_URL = "https://ffp.hnair.com/FFPClub/baseInfoController/queryQpQs?carrier=HU&segtyp=D"
RULES_PAGE = "https://ffp.hnair.com/FFPClub/cn/hqjf/hkjl/gszl/201809/t20180905_24975.html"
LOG = logging.getLogger("hainan-watch")

# 只读取每个票价系列的基础经济舱行。展开区域内还有权益套装和军人等专享价，
# 不能把那些嵌套行当成普通成人票；.flight-cabin[cabincode=Y] 的 Y 只是大舱。
EXTRACT = r"""flightNumber => {
  const normalized = s => s.replace(/\s/g, '').toUpperCase();
  const flights = [...document.querySelectorAll('.flight-list-go .flight-list-item')]
    .filter(f => [...f.querySelectorAll('.airline-code .flightNo')]
      .some(n => normalized(n.textContent) === flightNumber));
  if (flights.length !== 1) throw new Error('指定航班缺失或匹配不唯一');
  const f = flights[0];
  if (/共享/.test(f.querySelector('.tag-box')?.textContent || '') ||
      !/海南航空/.test(f.querySelector('.airline-code')?.textContent || ''))
    throw new Error('无法确认海南航空实际承运');
  const segments = [...f.querySelectorAll('.airline-code .flightNo')];
  if (segments.length !== 1) throw new Error('仅支持单程直达航班');
  const summary = f.querySelector('.flight-cabin[cabincode="Y"] .pricetag');
  if (!summary) throw new Error('未取得经济舱最低价');
  const fares = [...f.querySelectorAll('.dis-default-table.dis-cabin[cabincode="Y"]')]
    .map(r => ({
      brand: r.getAttribute('brandname'),
      title: r.querySelector('.dis-title')?.textContent?.trim(),
      cabin: r.querySelector('.dis-cang')?.textContent?.trim(),
      price: r.querySelector('.dis-price')?.textContent?.trim(),
      tax_note: r.querySelector('.dis-tax')?.textContent?.trim(),
      button: r.querySelector('.dis-yuding')?.textContent?.trim(),
      button_class: r.querySelector('.dis-yuding')?.className || ''
    }));
  return {
    flight_number: flightNumber,
    departure_time: f.querySelector('.flight-departure .time')?.textContent?.trim(),
    arrival_time: f.querySelector('.flight-arrive .time')?.textContent?.trim(),
    origin_label: f.querySelector('.flight-departure .airport')?.textContent?.trim(),
    destination_label: f.querySelector('.flight-arrive .airport')?.textContent?.trim(),
    summary_price: summary.textContent.trim(), fares
  };
}"""


class DataError(RuntimeError):
    pass


class NotificationError(RuntimeError):
    pass


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temp.replace(path)
    path.chmod(0o600)


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    config["flight_number"] = re.sub(r"\s", "", config["flight_number"]).upper()
    if not re.fullmatch(r"HU\d{3,4}", config["flight_number"]):
        raise ValueError("flight_number 必须是 HU 开头的海航航班号")
    date.fromisoformat(config["departure_date"])
    for key in ("origin", "destination"):
        config[key] = config[key].upper()
        if not re.fullmatch(r"[A-Z]{3}", config[key]):
            raise ValueError(key + " 必须是三字机场代码")
    if config["origin"] == config["destination"]:
        raise ValueError("出发与到达机场不能相同")
    if int(config.get("interval_seconds", 900)) < 300:
        raise ValueError("查询间隔至少 300 秒，建议 900 秒")
    rate = Decimal(config.get("minimum_qualifying_rate", "0.50"))
    if not rate.is_finite() or rate <= 0:
        raise ValueError("minimum_qualifying_rate 必须为正数")
    return config


def parse_rules(payload, departure_date):
    """按飞行日选择生效表；endDate 是包含当天的日期。"""
    if str(payload.get("code")) != "200":
        raise DataError("官方积累规则接口未成功")
    target = date.fromisoformat(departure_date)
    epoch_date = lambda ms: datetime.fromtimestamp(ms / 1000, TZ).date()
    applicable = [r for r in payload.get("data", [])
                  if r.get("segtyp") == "D"
                  and (r.get("startDate") is None or epoch_date(r["startDate"]) <= target)
                  and (r.get("endDate") is None or target <= epoch_date(r["endDate"]))]
    if not applicable:
        raise DataError("没有适用于出发日期的官方积累表")
    selected = max(applicable, key=lambda r: r.get("startDate") or 0)
    rates = {}
    for group in selected.get("qpqsBigClassDtos", []):
        for item in group.get("qpQsItems", []):
            start, end = item.get("startDate"), item.get("endDate")
            if start and target < date.fromisoformat(start[:10]):
                continue
            if end and target > date.fromisoformat(end[:10]):
                continue
            rate = Decimal(item["qp"])
            if not rate.is_finite() or rate < 0:
                raise DataError("官方积累比例格式异常")
            rates[item["cabin"].upper()] = str(rate)
    if not rates or "N" not in rates or "V" not in rates:
        raise DataError("官方积累表缺少预期舱位，停止判断")
    return rates


def fetch_rules(departure_date):
    request = Request(RULES_URL, headers={"Accept": "application/json"})
    with urlopen(request, timeout=25) as response:
        payload = json.load(response)
    rates = parse_rules(payload, departure_date)
    return {"rates": rates, "source": RULES_PAGE,
            "checked_at": datetime.now(TZ).isoformat(timespec="seconds")}


def money(raw):
    value = re.sub(r"[¥￥,\s]", "", raw or "")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", value):
        raise DataError("无法可靠读取票价")
    number = Decimal(value)
    if number <= 0:
        raise DataError("票价格式异常")
    return number


def normalize_snapshot(raw, config):
    fares = []
    for row in raw["fares"]:
        if not row.get("button") or re.search(r"disabled|soldout", row["button_class"], re.I):
            continue
        if not re.search(r"选购|预订", row["button"]):
            continue
        cabin = re.fullmatch(r"([A-Z])\s*舱", row.get("cabin") or "")
        if not cabin:
            raise DataError("票价对应的订座舱位缺失，停止判断")
        if "不含税" not in (row.get("tax_note") or ""):
            raise DataError("票价税费口径发生变化，停止判断")
        fares.append({"cabin": cabin.group(1), "price": str(money(row["price"])),
                      "brand": row["brand"], "title": row["title"]})
    if not fares:
        raise DataError("未取得可售普通成人经济舱票价")
    summary = money(raw["summary_price"])
    if min(Decimal(f["price"]) for f in fares) != summary:
        raise DataError("明细最低价与航班展示最低价不一致，停止判断")
    if not re.fullmatch(r"\d{2}:\d{2}", raw.get("departure_time") or ""):
        raise DataError("未取得起飞时间")
    return {"flight_number": config["flight_number"],
            "departure_date": config["departure_date"],
            "origin": config["origin"], "destination": config["destination"],
            "departure_at": config["departure_date"] + "T" + raw["departure_time"] + ":00+08:00",
            "origin_label": raw.get("origin_label"),
            "destination_label": raw.get("destination_label"),
            "checked_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "source": SEARCH_URL, "price_basis": "普通成人经济舱，不含税费", "fares": fares}


def query_flight(config, headed=False):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel=config.get("browser_channel", "msedge"), headless=not headed)
        try:
            page = browser.new_page(locale="zh-CN", timezone_id="Asia/Shanghai")
            page.set_default_timeout(15000)
            response = page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
            if not response or response.status != 200:
                raise DataError("官网拒绝访问；可用 check --headed 手动查看")
            page.wait_for_function("typeof window.submitForm === 'function'")
            page.locator("#searchForm .depart-input").wait_for(state="visible")
            # 填普通搜索表单，然后点击官网正常查询按钮；不处理验证码或购买。
            page.evaluate("""c => {
              const form = document.querySelector('#searchForm');
              const set = (name, value) => { form.elements.namedItem(name).value = value; };
              for (const [key, role] of [['origin','Origin'],['destination','Destination']]) {
                set(`Search/OriginDestinationInformation/${role}/location`, `AIR_${c[key]}_CN`);
                set(`Search/OriginDestinationInformation/${role}/location_input`, c[key]);
                set(`Search/OriginDestinationInformation/${role}/location_type`, 'false');
              }
              set('Search/DateInformation/departDate', c.departure_date);
              form.querySelector('.depart-input').value = c.departure_date;
            }""", config)
            page.locator(".search-btn").click()
            page.wait_for_url("**/air/searchResults.do**", timeout=60000)
            page.wait_for_selector(".flight-list-go .flight-list-item", timeout=30000)
            # 校验结果页实际使用的日期和机场，防止站点忽略输入后读错行程。
            actual = page.evaluate("""() => {
              const form = document.querySelector('#plan_trip');
              return Object.fromEntries(['Search/DateInformation/departDate',
                'Search/OriginDestinationInformation/Origin/location',
                'Search/OriginDestinationInformation/Destination/location']
                .map(n => [n, form?.elements.namedItem(n)?.value]));
            }""")
            expected = {"Search/DateInformation/departDate": config["departure_date"],
                        "Search/OriginDestinationInformation/Origin/location": "AIR_" + config["origin"] + "_CN",
                        "Search/OriginDestinationInformation/Destination/location": "AIR_" + config["destination"] + "_CN"}
            if actual != expected:
                raise DataError("结果页日期或机场与配置不符，停止判断")
            raw = page.evaluate(EXTRACT, config["flight_number"])
            return normalize_snapshot(raw, config)
        finally:
            browser.close()


def evaluate(snapshot, rules, minimum_rate):
    fares = snapshot["fares"]
    lowest = min(Decimal(f["price"]) for f in fares)
    cheapest = [f for f in fares if Decimal(f["price"]) == lowest]
    if any(f["cabin"] not in rules["rates"] for f in cheapest):
        raise DataError("最低价中有官方积累表未收录的舱位，停止判断")
    eligible = [dict(f, qualifying_rate=rules["rates"][f["cabin"]]) for f in cheapest
                if Decimal(rules["rates"][f["cabin"]]) >= Decimal(minimum_rate)]
    return {"matched": bool(eligible), "lowest_price": str(lowest),
            "cheapest_cabins": sorted({f["cabin"] for f in cheapest}), "eligible_fares": eligible}


def event_key(snapshot, result):
    if not result["matched"]:
        return None
    body = [snapshot[k] for k in ("flight_number", "departure_date", "origin", "destination")]
    body += [result["lowest_price"], sorted({(f["cabin"], f["qualifying_rate"]) for f in result["eligible_fares"]})]
    return hashlib.sha256(json.dumps(body, ensure_ascii=False).encode()).hexdigest()


def notify_macos(title, body, subtitle=""):
    if sys.platform != "darwin":
        raise ValueError("本地通知仅支持 macOS")
    # 通知内容只作为 argv 传入，不拼入 AppleScript 或 shell，兼容引号和换行。
    script = """on run argv
        display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv) sound name "Glass"
    end run
    """
    try:
        subprocess.run(["/usr/bin/osascript", "-", title, subtitle, body],
                       input=script, text=True, capture_output=True, check=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NotificationError("Mac 本地通知未提交成功，请检查系统通知设置") from exc


def notify_match(snapshot, result):
    cabins = "/".join(sorted({f["cabin"] for f in result["eligible_fares"]}))
    body = (f"{snapshot['origin']} → {snapshot['destination']}　最低 ¥{result['lowest_price']}　{cabins}舱（不含税费）\n"
            "符合金鹏定级积分条件，请打开海航官网确认舱位及含税总价。")
    notify_macos("海航机票符合条件", body,
                 f"{snapshot['flight_number']} · {snapshot['departure_date']}")


def print_result(snapshot, result):
    rows = ", ".join(f"{f['cabin']} ¥{f['price']}" for f in snapshot["fares"])
    print(f"{snapshot['checked_at']} {snapshot['flight_number']} {snapshot['departure_date']} "
          f"最低 ¥{result['lowest_price']} {'/'.join(result['cheapest_cabins'])}舱 "
          f"{'符合条件' if result['matched'] else '不符合条件'}；全部基础经济舱：{rows}", flush=True)


def check(config, headed=False):
    snapshot = query_flight(config, headed)
    rules = fetch_rules(config["departure_date"])
    result = evaluate(snapshot, rules, config.get("minimum_qualifying_rate", "0.50"))
    write_json(LOCAL / "last_check.json", {"snapshot": snapshot, "rules": rules, "result": result})
    print_result(snapshot, result)
    return snapshot, result


@contextlib.contextmanager
def single_instance():
    LOCAL.mkdir(exist_ok=True, mode=0o700)
    with (LOCAL / "watch.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("已有一个监控实例正在运行")
        yield


def watch(config):
    if sys.platform != "darwin":
        raise ValueError("本地通知仅支持 macOS")
    identity = "|".join(config[k] for k in ("flight_number", "departure_date", "origin", "destination"))
    # 与旧邮箱通知状态分开，切换渠道后仍能收到首次本地提醒。
    state_path = LOCAL / ("state-macos-" + hashlib.sha256(identity.encode()).hexdigest()[:16] + ".json")
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    # 尚未取得当天时刻前，以出发日结束作为停止上界。
    stop_at = datetime.combine(date.fromisoformat(config["departure_date"]) + timedelta(days=1), datetime.min.time(), TZ)
    if state.get("departure_at"):
        stop_at = datetime.fromisoformat(state["departure_at"])
    interval = int(config.get("interval_seconds", 900))
    print(f"监控 {identity}，每 {interval // 60} 分钟一次；Ctrl+C 停止。", flush=True)
    while datetime.now(TZ) < stop_at:
        started = time.monotonic()
        try:
            snapshot, result = check(config)
            stop_at = datetime.fromisoformat(snapshot["departure_at"])
            state["departure_at"] = snapshot["departure_at"]
            if datetime.now(TZ) >= stop_at:
                break
            key = event_key(snapshot, result)
            if key is not None and key != state.get("last_notified_key"):
                notify_match(snapshot, result)
                state["last_notified_key"] = key  # 系统接受通知后才去重；提交失败会重试。
                LOG.info("符合条件的本地通知已提交 macOS")
            elif key is None:
                state["last_notified_key"] = None  # 从不符合再次变为符合时重新提醒。
            state["failures"] = 0
            state["failure_notified"] = False
        except Exception as exc:
            state["failures"] = state.get("failures", 0) + 1
            LOG.error("检查或通知失败（%s），连续 %s 次；详情可运行 check --headed", type(exc).__name__, state["failures"])
            if state["failures"] >= 3 and not state.get("failure_notified"):
                try:
                    notify_macos("海航机票监控异常",
                                 "已连续三次无法完成查询或通知，请运行 check --headed 检查网络及官网。",
                                 f"{config['flight_number']} · {config['departure_date']}")
                    state["failure_notified"] = True
                except Exception:
                    LOG.error("异常告警通知也未提交成功，请检查系统通知设置")
        write_json(state_path, state)
        delay = min(max(1, interval - (time.monotonic() - started)),
                    max(0, (stop_at - datetime.now(TZ)).total_seconds()))
        time.sleep(delay)
    print("已到起飞时间或出发日结束，监控停止。", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("test-notification", help="发送一条 Mac 本地测试通知")
    sub.add_parser("rules", help="查询适用于出发日的官方定级积分比例")
    sub.add_parser("watch", help="每 15 分钟查询并在符合时通知")
    check_parser = sub.add_parser("check", help="只查询一次，不发送通知")
    check_parser.add_argument("--headed", action="store_true", help="显示独立浏览器以查看查询过程")
    args = parser.parse_args()
    LOCAL.mkdir(exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(LOCAL / "watch.log", maxBytes=1024 * 1024,
                                  backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[handler, logging.StreamHandler()])
    try:
        if args.command == "test-notification":
            notify_macos("海航机票监控：通知测试",
                         "这是一条测试通知。航班符合条件时，将在这里显示价格和舱位。")
            print("测试通知已提交 macOS，请查看右上角横幅或通知中心。未显示时请检查通知权限和专注模式。")
        else:
            config = load_config(args.config)
            if args.command == "rules":
                print(json.dumps(fetch_rules(config["departure_date"]), ensure_ascii=False, indent=2))
            elif args.command == "check":
                check(config, args.headed)
            else:
                with single_instance():
                    watch(config)
    except KeyboardInterrupt:
        print("\n监控已停止。")
    except Exception as exc:
        if isinstance(exc, (DataError, NotificationError, ValueError, FileNotFoundError)):
            print("错误：" + str(exc), file=sys.stderr)
        else:
            print("操作失败（" + type(exc).__name__ + "），请检查网络、浏览器或系统通知设置。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
