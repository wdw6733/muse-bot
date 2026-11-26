"""bot3 v2.5.2 tests: update list survives an upgrade.

Covers: cached report renders without a re-check, report has a re-check
button and keeps the original check time, marking an upgraded container
current in the cache, and the async update flow restoring the remaining
list (edit path) instead of dropping it.
Run: BOT_TOKEN=x OWNER_CHAT_ID=1 python3 tests/test_v252.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot

bot.DATA_DIR = tempfile.mkdtemp(prefix="bot3v252-")
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" {extra}" if extra and not cond else ""))


check("version", bot.BOT_VERSION == "2.5.2")
check("welcome_version", "v2.5.2" in bot.WELCOME)

RESULTS = [
    {"cid": "aa0000000001", "name": "web", "image": "web:1.0", "status": "update",
     "old_id": "sha256:old1", "new_id": "sha256:new1"},
    {"cid": "aa0000000002", "name": "db", "image": "db:2.0", "status": "update",
     "old_id": "sha256:old2", "new_id": "sha256:new2"},
    {"cid": "aa0000000003", "name": "cache", "image": "cache:1.0", "status": "current",
     "old_id": "sha256:same", "new_id": "sha256:same"},
]
bot._LAST_IMAGE_CHECK_RESULTS = [dict(r) for r in RESULTS]
bot._LAST_IMAGE_CHECK_TIME = "2026-11-26 20:00:00"

cached = bot._cached_image_report()
check("cached_exists", cached is not None)
text, markup = cached
check("cached_time_kept", "2026-11-26 20:00:00" in text, text)
check("cached_lists_updates", "web" in text and "db" in text and "2 个有更新" in text, text)
flat = [b for row in markup["inline_keyboard"] for b in row]
check("cached_recheck_btn", any(b.get("callback_data") == "menu:container_updates_recheck" for b in flat))
check("cached_upgrade_btns", any(b.get("callback_data") == "c:ask:update:aa0000000001" for b in flat)
      and any(b.get("callback_data") == "c:ask:update:aa0000000002" for b in flat))

# marking web upgraded: cache entry becomes current, cid follows the new container
bot.resolve_container = lambda q: {"Id": "ff0000000099", "Names": ["/web"]}
bot._mark_image_check_updated("aa0000000001")
entry = bot._LAST_IMAGE_CHECK_RESULTS[0]
check("mark_current", entry["status"] == "current" and entry["old_id"] == "sha256:new1", str(entry))
check("mark_new_cid", entry["cid"] == "ff0000000099", str(entry))
text2, markup2 = bot._cached_image_report()
check("remaining_list", "1 个有更新" in text2 and "db" in text2, text2)
flat2 = [b for row in markup2["inline_keyboard"] for b in row]
check("remaining_btns", any(b.get("callback_data") == "c:ask:update:aa0000000002" for b in flat2)
      and not any(b.get("callback_data") == "c:ask:update:aa0000000001" for b in flat2))

# async flow: result sent, then the start message is edited back to remaining list
bot._LAST_IMAGE_CHECK_RESULTS = [dict(r) for r in RESULTS]
bot.update_container_flow = lambda q: (True, "✅ web 已更新")
bot.resolve_container = lambda q: {"Id": "ff0000000099", "Names": ["/web"]}
sent, edited = [], []
bot._send_via_new_session = lambda chat_id, text, reply_markup=None: sent.append((text, reply_markup))
bot._edit_via_new_session = lambda chat_id, mid, text, reply_markup=None: edited.append((mid, text, reply_markup))
bot.update_container_async("aa0000000001", 123, 456)
for _ in range(50):
    if edited:
        break
    time.sleep(0.05)
check("async_result_sent", sent and "已更新" in sent[0][0], str(sent))
check("async_list_restored", bool(edited) and edited[0][0] == 456 and "1 个有更新" in edited[0][1],
      str(edited))

# failed update keeps the entry in the list
bot._LAST_IMAGE_CHECK_RESULTS = [dict(r) for r in RESULTS]
bot.update_container_flow = lambda q: (False, "拉取失败")
sent.clear(); edited.clear()
bot.update_container_async("aa0000000002", 123, 789)
for _ in range(50):
    if edited:
        break
    time.sleep(0.05)
check("async_fail_keeps_entry", bot._LAST_IMAGE_CHECK_RESULTS[1]["status"] == "update")
check("async_fail_list_restored", bool(edited) and "2 个有更新" in edited[0][1], str(edited))

# no cache -> None (menu falls back to a fresh check)
bot._LAST_IMAGE_CHECK_RESULTS = []
check("no_cache_none", bot._cached_image_report() is None)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
