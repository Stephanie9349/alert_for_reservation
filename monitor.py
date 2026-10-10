"""네이버 예약 빈자리 감시 → 텔레그램 알림.

한 번 실행하면 한 번 조회하고 끝난다. 주기 실행은 GitHub Actions(.github/workflows/monitor.yml)가 담당.
설정은 모두 환경변수로 받는다 (README 참고).
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
GRAPHQL_URL = "https://m.booking.naver.com/graphql"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

BUSINESS_ID = os.getenv("BUSINESS_ID", "597072")
BUSINESS_TYPE_ID = int(os.getenv("BUSINESS_TYPE_ID", "13"))
# "빈자리 나오면 새로운 예약창이 뜹니다" 안내용 상품 (실제 예약 불가) — 감시 제외
IGNORE_ITEM_IDS = {s.strip() for s in os.getenv("IGNORE_ITEM_IDS", "6576710").split(",") if s.strip()}
WEEKDAYS = {int(s) for s in os.getenv("WEEKDAYS", "0,1,2,3,4,5,6").split(",")}  # 0=월 ... 4=금 ... 6=일
MIN_HOUR = int(os.getenv("MIN_HOUR", "0"))
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "60"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
FAIL_ALERT_THRESHOLD = 3  # 연속 실패 n회(=30분)면 텔레그램으로 알림

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

WEEKDAY_KO = "월화수목금토일"

BIZ_ITEMS_QUERY = """
query bizItems($input: BizItemsParams) {
  bizItems(input: $input) { bizItemId name isImp bookingTimeUnitCode }
}"""

HOURLY_QUERY = """
query hourlySchedule($scheduleParams: ScheduleParams) {
  schedule(input: $scheduleParams) {
    bizItemSchedule {
      hourly {
        unitStartTime unitBookingCount unitStock bookingCount occupiedBookingCount
        stock maxBookingCount isUnitSaleDay isUnitBusinessDay
      }
    }
  }
}"""


def gql(op, query, variables):
    body = json.dumps({"operationName": op, "query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        f"{GRAPHQL_URL}?opName={op}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Referer": f"https://m.booking.naver.com/booking/{BUSINESS_TYPE_ID}/bizes/{BUSINESS_ID}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    if data.get("errors"):
        raise RuntimeError(f"GraphQL error ({op}): {data['errors']}")
    return data["data"]


def remaining(slot):
    # 네이버 예약 프론트엔드의 잔여 수량 계산과 동일
    unit_left = slot["unitStock"] - (slot["unitBookingCount"] or 0)
    if slot["stock"] is None:
        total_left = slot["unitStock"]
    else:
        total_left = slot["stock"] - (slot["bookingCount"] or 0) - (slot["occupiedBookingCount"] or 0)
    return min(unit_left, total_left)


def find_open_slots(now):
    items = gql("bizItems", BIZ_ITEMS_QUERY,
                {"input": {"businessId": BUSINESS_ID, "lang": "ko", "projections": "RESOURCE"}})["bizItems"]
    items = [it for it in items if it["isImp"] and it["bizItemId"] not in IGNORE_ITEM_IDS]
    print(f"감시 대상 상품 {len(items)}개: {[it['name'] for it in items]}")

    end = now + timedelta(days=DAYS_AHEAD)
    found = {}
    for it in items:
        hourly = gql("hourlySchedule", HOURLY_QUERY, {"scheduleParams": {
            "businessId": BUSINESS_ID,
            "businessTypeId": BUSINESS_TYPE_ID,
            "bizItemId": it["bizItemId"],
            "startDateTime": now.strftime("%Y-%m-%dT00:00:00"),
            "endDateTime": end.strftime("%Y-%m-%dT23:59:59"),
        }})["schedule"]["bizItemSchedule"]["hourly"] or []
        for slot in hourly:
            start = datetime.strptime(slot["unitStartTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            if (start > now and start.weekday() in WEEKDAYS and start.hour >= MIN_HOUR
                    and slot["isUnitBusinessDay"] and slot["isUnitSaleDay"] and remaining(slot) > 0):
                key = f"{it['bizItemId']}|{slot['unitStartTime']}"
                found[key] = {"item_id": it["bizItemId"], "item_name": it["name"].strip(), "start": start}
    return found


def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT_ID):
        print("[텔레그램 미설정] " + text)
        return
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": "true",
    }).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=30)


def format_alert(new_slots):
    lines = ["🔔 네이버 예약 빈자리 발견!"]
    by_item = {}
    for s in sorted(new_slots, key=lambda s: s["start"]):
        by_item.setdefault((s["item_id"], s["item_name"]), []).append(s["start"])
    for (item_id, name), starts in by_item.items():
        lines.append(f"\n📋 {name}")
        for st in starts:
            lines.append(f"  • {st:%m/%d}({WEEKDAY_KO[st.weekday()]}) {st:%H:%M}")
        lines.append(f"👉 https://m.booking.naver.com/booking/{BUSINESS_TYPE_ID}/bizes/{BUSINESS_ID}/items/{item_id}")
    return "\n".join(lines)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def main():
    state = load_state()
    now = datetime.now(KST)
    print(f"조회 시각: {now:%Y-%m-%d %H:%M:%S} KST")

    try:
        found = find_open_slots(now)
    except (urllib.error.URLError, RuntimeError, KeyError, TypeError, ValueError) as e:
        fails = state.get("consecutive_failures", 0) + 1
        state["consecutive_failures"] = fails
        print(f"조회 실패 ({fails}회 연속): {e!r}", file=sys.stderr)
        if fails == FAIL_ALERT_THRESHOLD:
            send_telegram(f"⚠️ 네이버 예약 조회가 {fails}회 연속 실패했습니다.\n{e!r}")
        save_state(state)
        return

    if state.get("consecutive_failures", 0) >= FAIL_ALERT_THRESHOLD:
        send_telegram("✅ 네이버 예약 조회가 다시 정상 작동합니다.")
    state["consecutive_failures"] = 0

    # 직전 조회 때 없던 자리만 알림 (자리가 없어졌다 다시 생기면 다시 알림)
    previous = set(state.get("open_slots", []))
    new_keys = [k for k in found if k not in previous]
    print(f"조건에 맞는 빈자리 {len(found)}개, 새로 생긴 자리 {len(new_keys)}개")
    if new_keys:
        send_telegram(format_alert([found[k] for k in new_keys]))

    state["open_slots"] = sorted(found)
    save_state(state)


if __name__ == "__main__":
    if "--test-telegram" in sys.argv:
        send_telegram("✅ 네이버 예약 알림 봇 테스트 메시지입니다.")
    else:
        main()
