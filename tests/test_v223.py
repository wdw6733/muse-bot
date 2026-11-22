"""bot3 v2.4.0 tests (durable copy: /tmp is wiped on VM restart).

Covers: image update check for all containers, colored CPU/memory/disk bars,
3-column manage grid, version bump, plus core regressions (keyboards,
plain text, callbacks, alerts, container update flow, document-update validation).
Run: BOT_TOKEN=test OWNER_CHAT_ID=123 python3 tests/test_v223.py
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot

bot.DATA_DIR = tempfile.mkdtemp(prefix="bot3v221-")
PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" {extra}" if extra and not cond else ""))

# ---------- color bars: single threshold color fill + white empty, stark contrast ----------
check("color_0", bot.color_bar(0) == "⬜" * 10)
check("color_100", bot.color_bar(100) == "🟥" * 10)
check("color_50", bot.color_bar(50) == "🟩" * 5 + "⬜" * 5)
check("color_33", bot.color_bar(33) == "🟩" * 3 + "⬜" * 7)
check("color_95", bot.color_bar(95) == "🟥" * 10)
check("color_clamp", bot.color_bar(150) == "🟥" * 10 and bot.color_bar(-5) == "⬜" * 10)
check("color_width5", bot.color_bar(50, width=5) == "🟩" * 3 + "⬜" * 2)
check("color_69_green", bot.color_bar(69) == "🟩" * 7 + "⬜" * 3)
check("color_70_orange", bot.color_bar(70) == "🟧" * 7 + "⬜" * 3)
check("color_89_orange", bot.color_bar(89) == "🟧" * 9 + "⬜" * 1)
check("color_90_red", bot.color_bar(90) == "🟥" * 9 + "⬜" * 1)
check("bar_unchanged", bot.bar(50) == "█" * 5 + "░" * 5)

FAKE = {"Id": "aaaa111111111111", "Names": ["/web"], "Image": "web:1.0",
        "State": "running", "Status": "Up"}
USAGES = [
    {"mount": "/host", "size": "100G", "used": "50G", "avail": "50G", "pct": 50.0},
    {"mount": "/host/mnt/PT", "size": "2T", "used": "1.9T", "avail": "0.1T", "pct": 95.0},
]

orig = {k: getattr(bot, k) for k in ("cpu_percent", "_loadavg", "_cpu_count", "_meminfo",
                                     "_uptime_text", "disk_usages", "list_containers")}
bot.cpu_percent = lambda: 25.0
bot._loadavg = lambda: ["0.10", "0.10", "0.10"]
bot._cpu_count = lambda: 8
bot._meminfo = lambda: {"MemTotal": 1000, "MemAvailable": 500}
bot._uptime_text = lambda: "1 小时 0 分"
bot.disk_usages = lambda: USAGES
bot.list_containers = lambda: [FAKE]
status = bot.fmt_status()
disk_lines = [ln for ln in status.splitlines() if ln.startswith("💾")]
check("status_disk_colored", len(disk_lines) == 2 and "🟩" in disk_lines[0] and "⬜" in disk_lines[0]
      and disk_lines[1].count("🟥") == 10 and "⬜" not in disk_lines[1], status)
check("status_cpu_colored", any("CPU：" in ln and "🟩" in ln and "⬜" in ln and "█" not in ln for ln in status.splitlines()))
check("status_mem_colored", any("内存：" in ln and "🟩" in ln and "⬜" in ln and "█" not in ln for ln in status.splitlines()))
for k, v in orig.items():
    setattr(bot, k, v)

# ---------- 3-column manage grid ----------
def fake_containers(n):
    return [{"Id": f"{i:012d}abcdef", "Names": [f"/c{i:02d}"],
             "Image": "img:1", "State": "running" if i % 2 == 0 else "exited",
             "Status": "Up" if i % 2 == 0 else "Exited"} for i in range(n)]

check("page_size_9", bot.MANAGE_PAGE_SIZE == 9)
bot.list_containers = lambda: fake_containers(20)
kb0, page0, total_pages, total = bot.manage_keyboard(0)
rows0 = kb0["inline_keyboard"]
check("manage_page0_grid", total == 20 and total_pages == 3 and page0 == 0
      and len(rows0) == 5 and [len(r) for r in rows0[:3]] == [3, 3, 3]
      and len(rows0[3]) == 1 and len(rows0[4]) == 2, str([len(r) for r in rows0]))
check("manage_first_btn", rows0[0][0]["text"].startswith("🟢 c00")
      and rows0[0][0]["callback_data"].startswith("c:detail:"))
check("manage_stopped_emoji", rows0[0][1]["text"].startswith("🔴 c01"))
kb1, _, _, _ = bot.manage_keyboard(1)
check("manage_page1_nav", len(kb1["inline_keyboard"][3]) == 2)
kb2, _, _, _ = bot.manage_keyboard(2)
rows2 = kb2["inline_keyboard"]
check("manage_last_page", len(rows2) == 3 and len(rows2[0]) == 2)
def kb_ok(markup):
    try:
        json.dumps(markup)
    except Exception:
        return False
    return all(len(b.get("callback_data", "")) <= 64
               for row in markup["inline_keyboard"] for b in row)
check("manage_kb_ok", kb_ok(kb0) and kb_ok(kb1) and kb_ok(kb2))
bot.list_containers = orig["list_containers"]

# ---------- version & keyboards ----------
check("version", bot.BOT_VERSION == "2.4.0")
check("welcome_version", "v2.4.0" in bot.WELCOME)
main_kb = bot.main_keyboard()
check("main_kb_4_rows", len(main_kb["inline_keyboard"]) == 4 and kb_ok(main_kb))
check("main_kb_container_updates", any(b.get("callback_data") == "menu:container_updates"
                                       for row in main_kb["inline_keyboard"] for b in row))
check("main_kb_no_muse_url", not any("url" in b
                                     for row in main_kb["inline_keyboard"] for b in row))
dkb = bot.container_detail_keyboard("abcdef123456")
flat = [b["callback_data"] for row in dkb["inline_keyboard"] for b in row if "callback_data" in b]
check("detail_kb_new_btns", any(d.startswith("c:search:") for d in flat)
      and any(d.startswith("c:ask:update:") for d in flat) and kb_ok(dkb))
check("help_new_cmds", "/update" not in bot.HELP and "/container_updates" in bot.HELP and "/alerts" in bot.HELP)

# ---------- plain text & callbacks ----------
sent, edits = [], []
bot.send_reply = lambda chat_id, text, reply_to=None, reply_markup=None, parse_mode=None: sent.append(text)
bot.edit_message = lambda chat_id, message_id, text, reply_markup=None, parse_mode=None: edits.append(text)
bot.answer_callback = lambda qid, text=None, show_alert=False: None
bot.fmt_status = lambda: "STATUS-MOCK"
sent.clear()
bot.handle_plain_text("系统状态", 123, 1)
check("plain_status", sent == ["STATUS-MOCK"])
sent.clear()
bot.handle_plain_text("重启 web 容器", 123, 1)
check("plain_action_guidance", sent and "找不到" in sent[0], str(sent))
sent.clear()
bot.handle_plain_text("重启", 123, 1)
check("plain_action_guidance_no_target", sent and "按钮" in sent[0] and "2 号" in sent[0], str(sent))
bot._set_pending_search(123, "aaaa11111111")
bot.resolve_container = lambda q: FAKE
bot.search_container_logs = lambda name, kw, tail=200, max_hits=30: (["ERROR x"], 1)
sent.clear()
bot.handle_plain_text("error", 123, 1)
check("plain_search", sent and "命中 1 行" in sent[0] and 123 not in bot._PENDING_SEARCH)

def cb(data):
    return {"id": "q1", "data": data, "from": {"id": 123},
            "message": {"chat": {"id": 123}, "message_id": 99}}
edits.clear()
bot.handle_callback(cb("menu:status"))
check("cb_status", edits == ["STATUS-MOCK"])
edits.clear()
bot.handle_callback(cb("c:search:aaaa11111111"))
check("cb_search_sets_pending", 123 in bot._PENDING_SEARCH)
bot.set_alerts_enabled(True)
bot.handle_callback(cb("alerts:off"))
check("cb_alerts_off", bot.alerts_enabled() is False)
bot.handle_callback(cb("alerts:on"))
check("cb_alerts_on", bot.alerts_enabled() is True)
bot.prune_images = lambda: (3, 2048)
bot.manage_keyboard = lambda page: (bot.back_keyboard(), 0, 1, 1)
edits.clear()
bot.handle_callback(cb("prune:do"))
check("cb_prune_do", edits and "3 个" in edits[0])

# ---------- alerts (compact) ----------
def reset_alerts():
    bot._ALERT_STATE.update({"running": None, "disk_alerted": set(), "load_until": 0.0,
                             "mem_until": 0.0, "last_check": None, "last_error": None})
    bot._INTENTIONAL_STOPS.clear()
    bot._PENDING_SEARCH.clear()

FAKE_B = dict(FAKE, Id="bbbb222222222222", Names=["/db"])
FAKE_B_STOPPED = dict(FAKE_B, State="exited", Status="Exited")
sent.clear()
reset_alerts()
bot.list_containers = lambda: [FAKE, FAKE_B]
bot.disk_usages = lambda: [{"mount": "/host", "size": "100G", "used": "10G", "avail": "90G", "pct": 10.0}]
bot._loadavg = lambda: ["0.10", "0.10", "0.10"]
bot._meminfo = lambda: {"MemTotal": 1000, "MemAvailable": 900}
bot._cpu_count = lambda: 8
bot.set_alerts_enabled(True)
bot.health_check_once()
check("alert_baseline", sent == [])
bot.list_containers = lambda: [FAKE, FAKE_B_STOPPED]
sent.clear()
bot.health_check_once()
check("alert_stopped", len(sent) == 1 and "db" in sent[0])
sent.clear()
bot.health_check_once()
check("alert_no_repeat", sent == [])
reset_alerts()
bot.list_containers = lambda: [FAKE, FAKE_B]
bot.health_check_once()
bot.record_intentional_stop(FAKE_B["Id"])
bot.list_containers = lambda: [FAKE, FAKE_B_STOPPED]
sent.clear()
bot.health_check_once()
check("alert_suppressed", sent == [])

# ---------- container update flow (compact) ----------
OLD_INSPECT = {
    "Id": FAKE["Id"], "Image": "sha256:old123", "Created": "2026-01-01T00:00:00Z",
    "Config": {"Image": "web:1.0", "Env": ["A=1"], "Cmd": ["run"], "Labels": {},
               "Hostname": "", "ExposedPorts": {"80/tcp": {}}, "Volumes": {"/data": {}}},
    "HostConfig": {"Binds": ["/host/conf:/conf:ro"],
                   "PortBindings": {"8080/tcp": [{"HostPort": "8080"}]},
                   "RestartPolicy": {"Name": "always"}, "NetworkMode": "bridge"},
    "Mounts": [{"Type": "volume", "Name": "abc123vol", "Destination": "/data", "RW": True}],
    "NetworkSettings": {"Networks": {"mynet": {"Aliases": ["web"]}}},
}
cfg = bot.build_recreate_config(OLD_INSPECT, "web:1.0")
check("recreate_anon_volume", "abc123vol:/data:rw" in cfg["HostConfig"]["Binds"])
check("split_image", bot._split_image("ghcr.io/foo/bar:tag") == ("ghcr.io/foo/bar", "tag"))

def run_flow(states, image_id="sha256:new999"):
    api_calls = []
    bot.resolve_container = lambda q: FAKE
    bot.inspect_container = lambda cid: OLD_INSPECT
    bot.docker_pull = lambda image, timeout=600: (True, "pulled")
    bot.docker_json = lambda m, p, timeout=30: {"Id": image_id}
    bot.docker_json_body = lambda m, p, payload, timeout=60: {"Id": "newid999999999999"}
    def fake_api(m, p, timeout=30):
        api_calls.append((m, p))
        return (204, b"")
    bot.docker_api = fake_api
    it = iter(states)
    bot._container_state = lambda cid: next(it, ("running", None))
    with mock.patch("time.sleep", lambda s: None):
        result = bot.update_container_flow("web")
    return result, api_calls

result, api_calls = run_flow([("running", None)])
check("update_success", result[0] is True and "已更新" in result[1], str(result))
check("update_sequence", any(p == f"/containers/{FAKE['Id']}" for m, p in api_calls if m == "DELETE")
      and any("/rename" in p for _, p in api_calls))
result, _ = run_flow([("running", None)], image_id="sha256:old123")
check("update_latest", result[0] is True and "无需重建" in result[1])
result, api_calls = run_flow([("exited", None)])
check("update_rollback", result[0] is False and "回滚" in result[1]
      and any("/start" in p and FAKE["Id"] in p for _, p in api_calls))
bot.resolve_container = lambda q: dict(FAKE, Names=["/tg-bot3"])
check("update_self_refused", bot.update_container_flow("tg-bot3")[0] is False)

# ---------- document-update validation ----------
check("parse_version", bot.parse_version("2.3.0") == (2, 3, 0))
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py"), "rb") as f:
    CURRENT_SRC = f.read()
CANDIDATE = CURRENT_SRC.replace(b'BOT_VERSION = "2.5.0"', b'BOT_VERSION = "9.9.9"')
assert CANDIDATE != CURRENT_SRC
ok, ver = bot.validate_candidate(CANDIDATE)
check("validate_good", ok is True and ver == "9.9.9", str(ver))
ok, _ = bot.validate_candidate(b"tiny")
check("validate_tiny_rejected", ok is False)
ok, _ = bot.validate_candidate(CANDIDATE + b"\ndef broken(:\n")
check("validate_syntax_rejected", ok is False)
# ---------- image update check (v2.3) ----------
check("image_check_defaults", bot.IMAGE_CHECK_ENABLED is True and bot.IMAGE_CHECK_INTERVAL_SEC == 21600)
check("help_check_updates", "/container_updates" in bot.HELP and "/check_updates" not in bot.HELP)

IMG_CONTAINERS = [
    {"Id": "aa0000000001", "Names": ["/web"], "Image": "web:1.0", "State": "running", "Status": "Up"},
    {"Id": "aa0000000002", "Names": ["/db"], "Image": "db:2.0", "State": "running", "Status": "Up"},
    {"Id": "aa0000000003", "Names": ["/bad"], "Image": "bad:1.0", "State": "running", "Status": "Up"},
    {"Id": "aa0000000004", "Names": ["/pinned"], "Image": "pinned@sha256:abc", "State": "running", "Status": "Up"},
    {"Id": "aa0000000005", "Names": ["/tg-bot3"], "Image": "muse-bot-bot3:latest", "State": "running", "Status": "Up"},
]
OLD_IDS = {"aa0000000001": "sha256:old1", "aa0000000002": "sha256:same",
           "aa0000000003": "sha256:old3", "aa0000000004": "sha256:pin"}

def fake_pull(image, timeout=300):
    if image == "bad:1.0":
        return False, "拉取失败 404"
    if "@" in image:
        raise ValueError("带 digest 的镜像暂不支持更新")
    return True, "pulled"

orig_check_fn = bot.check_container_image_updates
bot.list_containers = lambda: IMG_CONTAINERS
bot.inspect_container = lambda cid: {"Image": OLD_IDS[cid]}
bot.docker_pull = fake_pull
bot.docker_json = lambda m, p, timeout=30: {"Id": {"web%3A1.0": "sha256:new1", "db%3A2.0": "sha256:same"}[p.split("/images/")[1].split("/json")[0]]}
results = bot.check_container_image_updates()
by_name = {r["name"]: r for r in results}
check("img_check_statuses", len(results) == 4 and by_name["web"]["status"] == "update"
      and by_name["db"]["status"] == "current" and by_name["bad"]["status"] == "error"
      and by_name["pinned"]["status"] == "skipped" and "tg-bot3" not in by_name, str(results))
check("img_check_ids", by_name["web"]["old_id"] == "sha256:old1" and by_name["web"]["new_id"] == "sha256:new1")
bot.list_containers = lambda: (_ for _ in ()).throw(RuntimeError("sock down"))
check("img_check_fatal", bot.check_container_image_updates()[0]["status"] == "fatal")
bot.list_containers = orig["list_containers"]

text, markup = bot.format_image_check_report(results)
check("img_report_text", "web" in text and "web:1.0" in text and "1 个有更新" in text, text)
flat2 = [b for row in markup["inline_keyboard"] for b in row]
check("img_report_kb", any(b.get("callback_data", "").startswith("c:ask:update:aa0000000001") for b in flat2)
      and any(b.get("callback_data", "").startswith("img:ignore:aa0000000001") for b in flat2)
      and any(b.get("callback_data") == "menu:main" for b in flat2) and kb_ok(markup))
text2, markup2 = bot.format_image_check_report([dict(by_name["db"])])
check("img_report_none", "全部容器都已是最新" in text2 and len(markup2["inline_keyboard"]) == 1)

sent.clear()
bot.send_reply = lambda chat_id, text, reply_to=None, reply_markup=None, parse_mode=None: sent.append(text)
WEB_UPDATE = [{"cid": "aa0000000001", "name": "web", "image": "web:1.0",
               "status": "update", "old_id": "sha256:old1", "new_id": "sha256:new1"}]
bot.check_container_image_updates = lambda: WEB_UPDATE
bot.run_image_check(manual=False, chat_id=123)
check("img_notify_first", len(sent) == 1)
bot.run_image_check(manual=False, chat_id=123)
check("img_notify_dedup", len(sent) == 1)
WEB_UPDATE[0]["new_id"] = "sha256:new2"
bot.run_image_check(manual=False, chat_id=123)
check("img_notify_newer", len(sent) == 2)
bot.check_container_image_updates = lambda: [{**WEB_UPDATE[0], "status": "current", "new_id": "sha256:new2"}]
bot.run_image_check(manual=False, chat_id=123)
check("img_notify_cleared", len(sent) == 2 and bot._load_image_check_state()["notified"] == {})
bot.run_image_check(manual=True, chat_id=123)
check("img_manual_always", len(sent) == 3 and "全部容器都已是最新" in sent[2])
bot.check_container_image_updates = lambda: [{"status": "fatal", "error": "sock down"}]
sent.clear()
bot.run_image_check(manual=True, chat_id=123)
check("img_fatal_manual", len(sent) == 1 and "检查失败" in sent[0])
sent.clear()
bot.run_image_check(manual=False, chat_id=123)
check("img_fatal_auto_silent", sent == [])
bot.check_container_image_updates = orig_check_fn

class FakeThread:
    def __init__(self, target=None, daemon=None):
        self._target = target
    def start(self):
        self._target()

bot._IMAGE_CHECK_LOCK.acquire()
check("img_check_lock_busy", bot.start_image_check(manual=True, chat_id=123) is False)
bot._IMAGE_CHECK_LOCK.release()
orig_run = bot.run_image_check
called = []
bot.run_image_check = lambda manual, chat_id: called.append((manual, chat_id))
orig_thread = bot.threading.Thread
bot.threading.Thread = FakeThread
try:
    check("img_check_starts", bot.start_image_check(manual=True, chat_id=123) is True and called == [(True, 123)])
finally:
    bot.threading.Thread = orig_thread
    bot.run_image_check = orig_run

orig_start = bot.start_image_check
bot.start_image_check = lambda manual, chat_id: True
edits.clear()
bot.handle_callback(cb("menu:container_updates"))
check("cb_container_updates", edits and "正在检查" in edits[0])
bot.start_image_check = lambda manual, chat_id: False
edits.clear()
bot.handle_callback(cb("menu:container_updates"))
check("cb_container_updates_busy", edits and "稍等" in edits[0])
sent.clear()
bot.start_image_check = lambda manual, chat_id: True
bot.handle("/container_updates", 123, 1)
check("cmd_container_updates", sent and "正在检查" in sent[0])
bot._PENDING_SEARCH.clear()
sent.clear()
bot.handle_plain_text("检查更新", 123, 1)
check("plain_check_updates", sent and "正在检查" in sent[0])
sent.clear()
bot.handle_plain_text("容器更新", 123, 1)
check("plain_container_update", sent and "正在检查" in sent[0])
bot.start_image_check = orig_start

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
