"""bot3 v2.5.0 tests (durable copy: /tmp is wiped on VM restart).

Covers v2.5.0: memory TOP5 (read-only), op approval flow (plan -> ✅ -> execute,
5-min expiry, owner-only via handle_callback), TrueNAS service layer
(name validated against live list, blocked services, SSH midclt / API fallback),
and /topmem + /services commands.
Run: BOT_TOKEN=<redacted>
"""
import json
import os
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot

bot.DATA_DIR = tempfile.mkdtemp(prefix="bot3v250-")
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
check("version", bot.BOT_VERSION == "2.5.0")
check("welcome_version", "v2.5.0" in bot.WELCOME)
check("help_new_cmds", "/topmem" in bot.HELP and "/services" in bot.HELP)

# ---------- top_mem_text: sorted by RSS desc, top 5 ----------
fake_procs = {
    "11": ("aaa", 100, 100_000),
    "22": ("bbb", 100, 500_000),
    "33": ("ccc", 100, 50_000),
    "44": ("ddd", 100, 400_000),
    "55": ("eee", 100, 300_000),
    "66": ("fff", 100, 200_000),
    "77": ("ggg", 100, 10_000),
}
with mock.patch.object(bot, "_read_procs", return_value=fake_procs):
    out = bot.top_mem_text()
check("topmem_order", out.index("bbb") < out.index("ddd") < out.index("eee")
      < out.index("fff") < out.index("aaa"), out)
check("topmem_only5", "ccc" not in out and "ggg" not in out, out)
check("topmem_total", "共 7 个进程" in out, out)
check("topmem_html", out.startswith("🧠") and "<pre>" in out, out)
with mock.patch.object(bot, "_read_procs", return_value={}):
    check("topmem_empty", "读不到" in bot.top_mem_text())

# ---------- parse_op_request ----------
check("parse_restart", bot.parse_op_request("重启 plex") == ("restart", "plex"))
check("parse_chinese", bot.parse_op_request("帮我把 ssh 重启一下") == ("restart", "ssh"))
check("parse_stop", bot.parse_op_request("停止 cifs") == ("stop", "cifs"))
check("parse_start", bot.parse_op_request("启动 nfs") == ("start", "nfs"))
check("parse_no_target", bot.parse_op_request("重启") == (None, None))
check("parse_no_action", bot.parse_op_request("内存 top5") == (None, None))
check("parse_injection", bot.parse_op_request("重启 plex; rm -rf /") == (None, None))
check("parse_injection2", bot.parse_op_request("重启 $(reboot)") == (None, None))
check("parse_case", bot.parse_op_request("Restart Plex") == ("restart", "plex"))

# ---------- truenas validation (no live NAS needed) ----------
check("truenas_blocked", "middlewared" in bot.TRUENAS_BLOCKED_SERVICES
      and "docker" in bot.TRUENAS_BLOCKED_SERVICES)
for bad in ("", "a;b", "a b", "../etc", "SSH;reboot"):
    try:
        bot.truenas_service_action(bad, "restart")
        check(f"truenas_bad_{bad!r}", False)
    except ValueError:
        check(f"truenas_bad_{bad!r}", True)
try:
    bot.truenas_service_action("middlewared", "restart")
    check("truenas_blocked_exec", False)
except ValueError as e:
    check("truenas_blocked_exec", "不允许" in str(e))
try:
    bot.truenas_service_action("restart", "explode")
    check("truenas_bad_action", False)
except ValueError:
    check("truenas_bad_action", True)

with mock.patch.object(bot, "truenas_services", return_value=[("ssh", "RUNNING"), ("cifs", "RUNNING")]):
    try:
        bot.truenas_service_action("nosuchsvc", "restart")
        check("truenas_unknown", False)
    except ValueError as e:
        check("truenas_unknown", "没有这个服务" in str(e))
    with mock.patch.object(bot, "truenas_ssh_configured", return_value=True), \
         mock.patch.object(bot, "truenas_ssh_run",
                           return_value=[("true\n", "")]):
        res = bot.truenas_service_action("ssh", "restart")
        check("truenas_ssh_ok", "✅" in res and "ssh" in res, res)
    with mock.patch.object(bot, "truenas_ssh_configured", return_value=False), \
         mock.patch.object(bot, "truenas_api_configured", return_value=True):
        fake_resp = mock.Mock(status_code=200)
        fake_resp.json.return_value = {}
        with mock.patch.object(bot.requests, "post", return_value=fake_resp):
            res = bot.truenas_service_action("cifs", "restart")
            check("truenas_api_ok", "✅" in res and "cifs" in res, res)

# ---------- approval flow ----------
sent = []


def mock_send(chat_id, text, reply_to=None, reply_markup=None, parse_mode=None):
    sent.append((text, reply_markup, parse_mode))


bot.send_reply = mock_send
bot.edit_message = lambda *a, **k: None
bot.answer_callback = lambda qid, text=None, show_alert=False: None

sent.clear()
tok = bot.request_op_approval(123, 1, "container", "plex", "restart", "容器：plex（running）")
check("approval_sent", sent and "确定要重启" in sent[0][0] and "plex" in sent[0][0], str(sent))
check("approval_kb", sent and kb_ok(sent[0][1]), str(sent))
datas = [b.get("callback_data", "") for b in flat_buttons(sent[0][1])]
check("approval_buttons", any(d.startswith("op:do:") for d in datas)
      and any(d.startswith("op:cancel:") for d in datas), str(datas))
check("approval_stored", tok in bot._PENDING_OPS and bot._PENDING_OPS[tok][0] == "container")

# expired token -> refused
bot._PENDING_OPS["deadbeef"] = ("container", "plex", "restart", time.monotonic() - 1)
try:
    bot.execute_pending_op("deadbeef")
    check("approval_expired", False)
except ValueError as e:
    check("approval_expired", "过期" in str(e))
try:
    bot.execute_pending_op("00000000")
    check("approval_unknown", False)
except ValueError:
    check("approval_unknown", True)

# confirmed container op executes once, then token is gone
with mock.patch.object(bot, "container_action",
                       return_value={"Id": "abc123", "Names": ["/plex"]}), \
     mock.patch.object(bot, "container_display_name", return_value="plex"):
    res = bot.execute_pending_op(tok)
    check("approval_exec", "✅" in res and "plex" in res, res)
check("approval_consumed", tok not in bot._PENDING_OPS)

# handle_callback op:do: / op:cancel: (owner only - handle_callback checks OWNER)
edits = []
bot.edit_message = lambda chat_id, message_id, text, reply_markup=None, parse_mode=None: edits.append(text)
tok2 = bot.request_op_approval(123, 1, "service", "ssh", "restart", "系统服务：ssh")
with mock.patch.object(bot, "truenas_service_action", return_value="✅ 重启服务 ssh：成功"):
    bot.handle_callback({"data": f"op:do:{tok2}", "id": "q1",
                         "from": {"id": bot.OWNER_CHAT_ID},
                         "message": {"chat": {"id": 123}, "message_id": 9}})
check("callback_do", edits and "✅" in edits[-1] and "ssh" in edits[-1], str(edits))
tok3 = bot.request_op_approval(123, 1, "service", "ssh", "restart", "系统服务：ssh")
bot.handle_callback({"data": f"op:cancel:{tok3}", "id": "q2",
                     "from": {"id": bot.OWNER_CHAT_ID},
                     "message": {"chat": {"id": 123}, "message_id": 9}})
check("callback_cancel", edits and "已取消" in edits[-1] and tok3 not in bot._PENDING_OPS, str(edits))
# non-owner callback is ignored
tok4 = bot.request_op_approval(123, 1, "service", "ssh", "restart", "系统服务：ssh")
bot.handle_callback({"data": f"op:do:{tok4}", "id": "q3",
                     "from": {"id": 999999},
                     "message": {"chat": {"id": 123}, "message_id": 9}})
check("callback_nonowner", tok4 in bot._PENDING_OPS)

# ---------- plain text routing ----------
sent.clear()
with mock.patch.object(bot, "_read_procs", return_value=fake_procs):
    bot.handle_plain_text("内存 TOP5", 123, 1)
check("plain_topmem", sent and "内存 TOP5" in sent[0][0] and sent[0][2] == "HTML", str(sent))
sent.clear()
bot.handle_plain_text("重启 nosuchthing123", 123, 1)
check("plain_restart_unknown", sent and "找不到" in sent[0][0], str(sent))
sent.clear()
with mock.patch.object(bot, "resolve_container",
                       return_value={"Id": "abc123456789", "Names": ["/plex"], "State": "running"}), \
     mock.patch.object(bot, "container_display_name", return_value="plex"):
    bot.handle_plain_text("重启 plex", 123, 1)
check("plain_restart_approval", sent and "确定要重启" in sent[0][0], str(sent))
check("plain_restart_no_exec", sent and "已执行" not in sent[0][0], str(sent))

# ---------- /topmem + /services commands ----------
sent.clear()
with mock.patch.object(bot, "_read_procs", return_value=fake_procs):
    bot.handle("/topmem", 123, 1)
check("cmd_topmem", sent and "内存 TOP5" in sent[0][0], str(sent))
sent.clear()
with mock.patch.object(bot, "truenas_services", return_value=[("ssh", "RUNNING")]):
    bot.handle("/services", 123, 1)
check("cmd_services", sent and "ssh" in sent[0][0], str(sent))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
