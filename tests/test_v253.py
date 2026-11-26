"""bot3 v2.5.3 tests: deduped report + batch selection.

Covers: report text no longer lists every update (buttons carry the list),
toggle buttons reflect selection, select-all/clear, batch upgrade flow
(progress + summary + remaining list), batch ignore.
Run: BOT_TOKEN=x OWNER_CHAT_ID=1 python3 tests/test_v253.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot

bot.DATA_DIR = tempfile.mkdtemp(prefix="bot3v253-")
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" {extra}" if extra and not cond else ""))


check("version", bot.BOT_VERSION == "2.5.3")
check("welcome_version", "v2.5.3" in bot.WELCOME)

RESULTS = [
    {"cid": "aa0000000001", "name": "web", "image": "web:1.0", "status": "update",
     "old_id": "sha256:old1", "new_id": "sha256:new1"},
    {"cid": "aa0000000002", "name": "db", "image": "db:2.0", "status": "update",
     "old_id": "sha256:old2", "new_id": "sha256:new2"},
    {"cid": "aa0000000003", "name": "cache", "image": "cache:1.0", "status": "update",
     "old_id": "sha256:old3", "new_id": "sha256:new3"},
    {"cid": "aa0000000004", "name": "localapp", "image": "myapp:local", "status": "skipped",
     "error": "本地构建镜像，没有仓库可检查"},
]
bot._LAST_IMAGE_CHECK_RESULTS = [dict(r) for r in RESULTS]
bot._LAST_IMAGE_CHECK_TIME = "2026-11-26 20:20:00"
bot._IMAGE_CHECK_SELECTED = set()

text, markup = bot._cached_image_report()
check("dedup_no_per_item_text", "web:1.0" not in text and "sha256" not in text
      and "有新镜像" not in text, text)
check("summary_kept", "3 个有更新" in text and "已选 0 个" in text, text)
check("problem_kept", "localapp" in text, text)
flat = [b for row in markup["inline_keyboard"] for b in row]
check("toggle_btns", any(b.get("callback_data") == "img:sel:aa0000000001" for b in flat)
      and any(b.get("callback_data") == "img:sel:aa0000000003" for b in flat))
check("no_old_upgrade_btns", not any(str(b.get("callback_data", "")).startswith("c:ask:update:")
                                     for b in flat))
check("batch_btns", any(b.get("callback_data") == "img:ask:batch" for b in flat)
      and any(b.get("callback_data") == "img:selall" for b in flat)
      and any(b.get("callback_data") == "img:ignore:batch" for b in flat))

# selection rendering
bot._IMAGE_CHECK_SELECTED = {"aa0000000001", "aa0000000003"}
text2, markup2 = bot._cached_image_report()
check("selected_count", "已选 2 个" in text2 and "升级所选 (2)" in str(markup2), text2)
flat2 = [b for row in markup2["inline_keyboard"] for b in row]
check("checked_marks", any(b.get("text", "").startswith("☑️") and "web" in b["text"] for b in flat2)
      and any(b.get("text", "").startswith("⬜") and "db" in b["text"] for b in flat2))
check("selected_updates", [r["name"] for r in bot._selected_updates()] == ["web", "cache"])

# batch flow: web ok, cache fails -> summary + remaining list keeps cache+db
bot._LAST_IMAGE_CHECK_RESULTS = [dict(r) for r in RESULTS]
bot._IMAGE_CHECK_SELECTED = {"aa0000000001", "aa0000000003"}
bot.resolve_container = lambda q: {"Id": "ff0000000099", "Names": ["/web"]}


def fake_flow(q):
    if q == "aa0000000001":
        return True, "✅ web 已更新"
    return False, "拉取失败 500"


bot.update_container_flow = fake_flow
sent, edited = [], []
bot._send_via_new_session = lambda chat_id, text, reply_markup=None: sent.append((text, reply_markup))
bot._edit_via_new_session = lambda chat_id, mid, text, reply_markup=None: edited.append((mid, text, reply_markup))
bot.update_containers_batch_async(["aa0000000001", "aa0000000003"], 123, 456)
for _ in range(80):
    if sent and edited and "个有更新" in edited[-1][1]:
        break
    time.sleep(0.05)
check("batch_summary", bool(sent) and "成功 1 个，失败 1 个" in sent[-1][0], str(sent))
check("batch_progress", any("批量升级中" in e[1] for e in edited), str(edited))
check("batch_remaining", "2 个有更新" in edited[-1][1], edited[-1][1])
check("batch_web_current", bot._LAST_IMAGE_CHECK_RESULTS[0]["status"] == "current")
check("batch_cache_kept", bot._LAST_IMAGE_CHECK_RESULTS[2]["status"] == "update")
check("batch_selected_pruned", bot._IMAGE_CHECK_SELECTED == {"aa0000000003"},
      str(bot._IMAGE_CHECK_SELECTED))

# batch ignore marks selected ignored and re-renders without them as updates
bot._LAST_IMAGE_CHECK_RESULTS = [dict(r) for r in RESULTS]
bot._IMAGE_CHECK_SELECTED = {"aa0000000002"}
state = bot._load_image_check_state()
state["ignored"]["aa0000000002"] = {"name": "db", "image": "db:2.0", "new_id": "sha256:new2"}
bot._save_image_check_state(state)
text3, markup3 = bot._cached_image_report()
check("ignored_counted", "1 个已忽略" in text3 and "2 个有更新" in text3, text3)
flat3 = [b for row in markup3["inline_keyboard"] for b in row]
check("unignore_btn", any(b.get("callback_data") == "img:unignore:aa0000000002" for b in flat3))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
