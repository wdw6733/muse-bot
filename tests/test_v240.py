"""bot3 v2.4.0 tests (durable copy: /tmp is wiped on VM restart).

Covers the v2.4.0 redo: removed menu items/commands, 容器更新 rename +
ignore/upgrade buttons, status with network row and ESXi-only temperature,
top list <pre> format, delete-never-removes-volumes, version bump.
Run: BOT_TOKEN=test OWNER_CHAT_ID=123 python3 tests/test_v240.py
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot

bot.DATA_DIR = tempfile.mkdtemp(prefix="bot3v240-")
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" {extra}" if extra and not cond else ""))


def kb_ok(markup):
    try:
        json.dumps(markup)
    except Exception:
        return False
    return all(len(b.get("callback_data", "")) <= 64
               for row in markup["inline_keyboard"] for b in row
               if "callback_data" in b)


def flat_buttons(markup):
    return [b for row in markup["inline_keyboard"] for b in row]


# ---------- version ----------
check("version", bot.BOT_VERSION == "2.4.0")
check("welcome_version", "v2.4.0" in bot.WELCOME)

# ---------- removed things are gone ----------
for fn in ("fmt_containers", "stats_text", "disk_text", "temp_text", "net_text",
           "host_stats", "calc_cpu_percent", "container_stats_all",
           "check_github_update", "announce_pending_update", "fetch_github_file",
           "url_btn", "MUSE_BOT_USERNAME"):
    check(f"gone_fn_{fn}", not hasattr(bot, fn))

main_kb = bot.main_keyboard()
datas = [b.get("callback_data", "") for b in flat_buttons(main_kb)]
texts = [b.get("text", "") for b in flat_buttons(main_kb)]
check("menu_no_removed", not (set(datas) & {"menu:containers", "menu:stats", "menu:disk",
                                            "menu:temp", "menu:net", "menu:update",
                                            "menu:check_updates"}))
check("menu_no_removed_text", not any(t in "".join(texts) for t in
                                      ("容器列表", "容器资源", "磁盘空间", "检查更新", "给 Muse 下指令")))
check("menu_no_temp_net", "温度" not in "".join(texts) and "网络流量" not in "".join(texts))
check("menu_no_url_btn", not any("url" in b for b in flat_buttons(main_kb)))
check("menu_has_container_updates", "menu:container_updates" in datas
      and any("容器更新" in t for t in texts))
check("menu_rows", len(main_kb["inline_keyboard"]) == 4 and kb_ok(main_kb))
check("welcome_no_muse", "给 Muse 下指令" not in bot.WELCOME and "MuseAI6_Bot" not in bot.WELCOME)
check("help_no_removed", "/disk" not in bot.HELP and "/update" not in bot.HELP
      and "/check_updates" not in bot.HELP and "磁盘空间" not in bot.HELP
      and "/container_updates" in bot.HELP)

# ---------- removed commands ----------
sent, edits = [], []


def mock_send(chat_id, text, reply_to=None, reply_markup=None, parse_mode=None):
    sent.append((text, reply_markup, parse_mode))


def mock_edit(chat_id, message_id, text, reply_markup=None, parse_mode=None):
    edits.append((text, reply_markup, parse_mode))


bot.send_reply = mock_send
bot.edit_message = mock_edit
bot.answer_callback = lambda qid, text=None, show_alert=False: None

for cmd in ("/disk", "/update", "/check_updates"):
    sent.clear()
    bot.handle(cmd, 123, 1)
    check(f"cmd_gone_{cmd}", sent and "不认识的命令" in sent[0][0], str(sent))

orig_start = bot.start_image_check
bot.start_image_check = lambda manual, chat_id: True
sent.clear()
bot.handle("/container_updates", 123, 1)
check("cmd_container_updates", sent and "正在检查" in sent[0][0])
bot.start_image_check = orig_start

# ---------- plain text: removed views fold into status / manage ----------
orig = {k: getattr(bot, k) for k in ("cpu_percent", "_loadavg", "_cpu_count", "_meminfo",
                                     "_uptime_text", "disk_usages", "list_containers",
                                     "_net_rates", "esxi_temperature")}
bot.cpu_percent = lambda: 25.0
bot._loadavg = lambda: ["0.10", "0.10", "0.10"]
bot._cpu_count = lambda: 8
bot._meminfo = lambda: {"MemTotal": 22000000, "MemAvailable": 8000000, "SwapTotal": 0}
bot._uptime_text = lambda: "3 天 2 小时 10 分"
bot.disk_usages = lambda: [
    {"mount": "/host", "size": "16G", "used": "1G", "avail": "15G", "pct": 5.0},
    {"mount": "/host/mnt/PT", "size": "12T", "used": "5.8T", "avail": "5.3T", "pct": 53.0},
]
bot.list_containers = lambda: [{"Id": "aa0000000001", "Names": ["/web"], "Image": "web:1.0",
                                "State": "running", "Status": "Up"}]
bot._net_rates = lambda: [("eth0", 5_300_000_000, 1_100_000_000, 1_200_000, 300_000)]
bot.esxi_temperature = lambda: ([], [])

status = bot.fmt_status()
check("status_has_net_row", "🌍 eth0" in status and "↓" in status and "↑" in status, status)
check("status_no_temp_when_unavailable", "🌡️" not in status and "温度" not in status, status)
check("status_has_disks", "💾" in status and "PT" in status)
check("status_has_bars", "🟩" in status and "⬜" in status)
check("status_has_uptime", "已运行" in status and "3 天" in status)
check("status_has_containers", "容器：运行 1 / 共 1" in status, status)

bot.esxi_temperature = lambda: ([("Inlet Temp", 23.0), ("CPU Temp", 45.5)],
                                [("Fan 1", 3120.0), ("Fan 2", 3060.0)])
status2 = bot.fmt_status()
check("status_esxi_temp", "🌡️ ESXi 温度" in status2 and "23.0°C" in status2 and "45.5°C" in status2, status2)
check("status_esxi_fan", "🌀 ESXi 风扇" in status2 and "3120 RPM" in status2, status2)
bot.esxi_temperature = lambda: ([], [])

for phrase in ("磁盘空间", "温度", "网络流量"):
    sent.clear()
    bot.handle_plain_text(phrase, 123, 1)
    check(f"plain_folds_{phrase}", sent and "NAS 状态" in sent[0][0], str(sent))
sent.clear()
bot.handle_plain_text("容器", 123, 1)
check("plain_container_manage", sent and any(
    b.get("callback_data", "").startswith("c:detail:") for b in flat_buttons(sent[0][1])),
    str(sent))
for k, v in orig.items():
    setattr(bot, k, v)

# ---------- ESXi temperature parser ----------
bot.ESXI_HOST = "192.168.1.1"
bot.ESXI_PASSWORD = "x"
ESXCLI_SAMPLE = """Sensor
   Name: Inlet Temp
   Sensor Number: 32
   Sensor Type: Temperature
   Reading: 23.000
   Base Units: degrees C
Sensor
   Name: CPU Temp
   Sensor Number: 33
   Sensor Type: Temperature
   Reading: 45.500
   Base Units: degrees C
Sensor
   Name: Fan 1
   Sensor Number: 1
   Sensor Type: Fan
   Reading: 3120.000
   Base Units: RPM
"""
bot.esxi_run = lambda cmds, connect_timeout=15, exec_timeout=30: [(ESXCLI_SAMPLE, "")]
temps, fans = bot.esxi_temperature()
check("esxi_temp_parse", temps == [("Inlet Temp", 23.0), ("CPU Temp", 45.5)]
      and fans == [("Fan 1", 3120.0)], str((temps, fans)))
bot.esxi_run = lambda cmds, connect_timeout=15, exec_timeout=30: (_ for _ in ()).throw(RuntimeError("ssh down"))
check("esxi_temp_fail_silent", bot.esxi_temperature() == ([], []))
bot.esxi_run = lambda cmds, connect_timeout=15, exec_timeout=30: [("garbage\nno sensors here\n", "")]
check("esxi_temp_unparsable", bot.esxi_temperature() == ([], []))
bot.ESXI_HOST = ""
bot.ESXI_PASSWORD = ""
check("esxi_temp_unconfigured", bot.esxi_temperature() == ([], []))

# ---------- top list format ----------
PROCS = {"100": ("dockerd", 1000, 120000), "200": ("python3", 500, 60000),
         "300": ("a-very-long-process-name-here", 300, 30000)}
bot._read_procs = lambda: dict(PROCS)
with mock.patch("time.sleep", lambda s: None):
    top = bot.top_text()
check("top_has_pre", "<pre>" in top and "</pre>" in top, top)
pre = top.split("<pre>")[1].split("</pre>")[0].strip("\n").splitlines()
check("top_header", "NAME" in pre[0] and "CPU%" in pre[0] and "MEM" in pre[0], str(pre))
data_rows = pre[1:]
check("top_rows_aligned", len(data_rows) == 3
      and len({bot.display_width(r) for r in data_rows}) == 1
      and all(r.index("%") == data_rows[0].index("%") for r in data_rows), str(pre))
check("top_truncates_long_name", any("a-very-long-proce…" in r for r in data_rows), str(pre))
check("top_count", "共 3 个进程" in top)
check("top_no_pid_col", all(" 100 " not in r and " 200 " not in r for r in data_rows))

# ---------- delete never removes volumes ----------
FAKE = {"Id": "aaaa111111111111", "Names": ["/web"], "Image": "web:1.0",
        "State": "running", "Status": "Up"}
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
deletes = [p for m, p in api_calls if m == "DELETE"]
check("delete_no_volumes", result[0] is True and deletes
      and all("v=" not in p and "volumes" not in p for p in deletes), str(api_calls))
check("delete_plain_rm", any(p == f"/containers/{FAKE['Id']}" for p in deletes), str(deletes))
result, api_calls = run_flow([("exited", None)])
deletes = [p for m, p in api_calls if m == "DELETE"]
check("rollback_no_volumes", result[0] is False and all("v=" not in p for p in deletes), str(api_calls))

prune_paths = []
bot.docker_json = lambda m, p, timeout=30: (prune_paths.append(p),
                                            {"ImagesDeleted": [{"Deleted": "x"}], "SpaceReclaimed": 100})[1]
count, reclaimed = bot.prune_images()
check("prune_only_dangling", prune_paths == ["/images/prune?dangling=true"]
      and count == 1 and reclaimed == 100, str(prune_paths))

dkb = bot.container_detail_keyboard("abcdef123456")
flat = [b.get("callback_data", "") for row in dkb["inline_keyboard"] for b in row]
check("detail_no_delete_btn", not any("delete" in d or "remove" in d or d.startswith("c:do:rm") for d in flat))

# ---------- 容器更新：rename + ignore / upgrade ----------
RESULTS = [
    {"cid": "aa0000000001", "name": "web", "image": "web:1.0", "status": "update",
     "old_id": "sha256:old1", "new_id": "sha256:new1"},
    {"cid": "aa0000000002", "name": "db", "image": "db:2.0", "status": "current",
     "old_id": "sha256:same", "new_id": "sha256:same"},
    {"cid": "aa0000000003", "name": "bad", "image": "bad:1.0", "status": "error",
     "error": "拉取失败 404"},
]
bot._LAST_IMAGE_CHECK_RESULTS = RESULTS
text, markup = bot.format_image_check_report(RESULTS)
check("report_title_renamed", text.startswith("🔍 容器更新（") and "容器镜像更新检查" not in text, text)
check("report_upgrade_btn", any(b.get("callback_data") == "c:ask:update:aa0000000001"
                                for b in flat_buttons(markup)))
check("report_ignore_btn", any(b.get("callback_data") == "img:ignore:aa0000000001"
                               for b in flat_buttons(markup)))
check("report_short_ids", "old1" in text and "new1" in text, text)


def cb(data):
    return {"id": "q1", "data": data, "from": {"id": 123},
            "message": {"chat": {"id": 123}, "message_id": 99}}


edits.clear()
bot.handle_callback(cb("img:ignore:aa0000000001"))
state = bot._load_image_check_state()
check("ignore_persisted", state.get("ignored", {}).get("aa0000000001", {}).get("new_id") == "sha256:new1",
      str(state.get("ignored")))
check("ignore_rerenders", edits and "已忽略" in edits[0][0]
      and not any(b.get("callback_data") == "c:ask:update:aa0000000001"
                  for b in flat_buttons(edits[0][1])), str(edits))
check("ignore_restore_btn", edits and any(
    b.get("callback_data") == "img:unignore:aa0000000001" for b in flat_buttons(edits[0][1])))

edits.clear()
bot.handle_callback(cb("img:unignore:aa0000000001"))
state = bot._load_image_check_state()
check("unignore_clears", state.get("ignored", {}) == {})
check("unignore_reply", edits and "已恢复" in edits[0][0] and "web" in edits[0][0], str(edits))

# 忽略后自动检查不再通知，手动检查仍出报告且带已忽略段；出了更新的版本会再提醒。
bot._save_image_check_state({"notified": {}, "ignored": {
    "aa0000000001": {"name": "web", "image": "web:1.0", "new_id": "sha256:new1"}}})
bot.check_container_image_updates = lambda: RESULTS
sent.clear()
bot.run_image_check(manual=False, chat_id=123)
check("ignored_auto_silent", sent == [])
sent.clear()
bot.run_image_check(manual=True, chat_id=123)
check("ignored_manual_shows", len(sent) == 1 and "已忽略" in sent[0][0] and "web" in sent[0][0], str(sent))
newer = [dict(RESULTS[0], new_id="sha256:new2")] + RESULTS[1:]
bot.check_container_image_updates = lambda: newer
sent.clear()
bot.run_image_check(manual=False, chat_id=123)
check("ignored_newer_renotifies", len(sent) == 1 and "web" in sent[0][0], str(sent))
bot._save_image_check_state({"notified": {}, "ignored": {}})

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
