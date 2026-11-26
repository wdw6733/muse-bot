"""bot3 v2.5.4 tests: companion databases are not upgraded on their own.

Covers: db-like detection (image basename + service/name tokens),
companion grouping by compose project, check flow marking companions
without pulling them, report summary, and the single-update guard.
Run: BOT_TOKEN=x OWNER_CHAT_ID=1 python3 tests/test_v254.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot

bot.DATA_DIR = tempfile.mkdtemp(prefix="bot3v254-")
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" {extra}" if extra and not cond else ""))


check("version", bot.BOT_VERSION == "2.5.4")
check("welcome_version", "v2.5.4" in bot.WELCOME)

# db-like detection
check("db_postgres_image", bot._is_db_like("postgres:18", "postgresql3", "postgresql3"))
check("db_redis_image", bot._is_db_like("redis:7.4-alpine", "panghu-redis", "panghu-redis"))
check("db_by_service_token", bot._is_db_like("sha256:abc", "pgsql", "msgo-pgsql"))
check("db_by_name_token", bot._is_db_like("", "redis_mpv3", "redis_mpv3"))
check("main_not_db", not bot._is_db_like("leolitaly/avdb:latest", "avdb", "avdb"))
check("main_app_not_db", not bot._is_db_like("ghcr.io/workroom2008/trackferry:latest",
                                            "trackferry", "trackferry"))
check("flaresolverr_not_db", not bot._is_db_like("ghcr.io/flaresolverr/flaresolverr:latest",
                                                "flaresolverr", "panghu-flaresolverr"))

# grouping: multi-container project -> dbs are companions; single -> not
entries = [
    {"cid": "c1", "name": "moviepilot-v3", "image": "jxxghp/moviepilot-v2:latest",
     "project": "moviepilot", "service": "moviepilot"},
    {"cid": "c2", "name": "postgresql3", "image": "postgres:18",
     "project": "moviepilot", "service": "postgresql3"},
    {"cid": "c3", "name": "redis_mpv3", "image": "redis",
     "project": "moviepilot", "service": "redis_mpv3"},
    {"cid": "c4", "name": "minio", "image": "quay.io/minio/minio",
     "project": "s3-minio", "service": "minio"},
    {"cid": "c5", "name": "solo-pg", "image": "postgres:17",
     "project": "solo", "service": "pg"},
    {"cid": "c6", "name": "plain", "image": "nginx:latest", "project": "", "service": ""},
]
comp = bot._companion_db_projects(entries)
check("companions_grouped", comp == {"c2": "moviepilot", "c3": "moviepilot"}, str(comp))

# check flow: companions are marked without pulling; main still checked
CONTAINERS = [
    {"Id": "aa0000000001", "Names": ["/moviepilot-v3"], "Image": "mp:1",
     "Labels": {"com.docker.compose.project": "moviepilot",
                "com.docker.compose.service": "moviepilot"}},
    {"Id": "aa0000000002", "Names": ["/postgresql3"], "Image": "22fac36945da",
     "Labels": {"com.docker.compose.project": "moviepilot",
                "com.docker.compose.service": "postgresql3"}},
    {"Id": "aa0000000003", "Names": ["/redis_mpv3"], "Image": "978293674c6b",
     "Labels": {"com.docker.compose.project": "moviepilot",
                "com.docker.compose.service": "redis_mpv3"}},
    {"Id": "aa0000000004", "Names": ["/web"], "Image": "web:1.0", "Labels": {}},
]
INSPECTS = {
    "aa0000000001": {"Image": "sha256:old1", "Config": {"Image": "mp:1", "Labels": {}}},
    "aa0000000002": {"Image": "sha256:old2", "Config": {"Image": "postgres:18", "Labels": {}}},
    "aa0000000003": {"Image": "sha256:old3", "Config": {"Image": "redis", "Labels": {}}},
    "aa0000000004": {"Image": "sha256:old4", "Config": {"Image": "web:1.0", "Labels": {}}},
}
pulled = []
bot.list_containers = lambda: CONTAINERS
bot.inspect_container = lambda cid: INSPECTS[cid]
bot._image_has_repo_digest = lambda image_id: True
bot.docker_pull = lambda ref, timeout=None: (pulled.append(ref), (True, ""))[1]
bot.docker_json = lambda m, p, **kw: {"Id": "sha256:new-" + p.split("/")[2][:3]}
results = bot.check_container_image_updates()
by_name = {r["name"]: r for r in results}
check("pg_companion", by_name["postgresql3"]["status"] == "companion", str(results))
check("redis_companion", by_name["redis_mpv3"]["status"] == "companion")
check("main_checked", by_name["moviepilot-v3"]["status"] in ("update", "current"))
check("companions_not_pulled", "postgres:18" not in pulled and "redis" not in pulled, str(pulled))
check("web_checked", by_name["web"]["status"] in ("update", "current"))

# report: companions counted separately, not selectable
bot._LAST_IMAGE_CHECK_RESULTS = results
bot._LAST_IMAGE_CHECK_TIME = "2026-11-26 20:50:00"
bot._IMAGE_CHECK_SELECTED = set()
text, markup = bot._cached_image_report()
check("report_companion_line", "2 个附属数据库不单独升级" in text
      and "postgresql3" in text and "redis_mpv3" in text, text)
flat = [b for row in markup["inline_keyboard"] for b in row]
check("companions_not_selectable",
      not any(b.get("callback_data") in ("img:sel:aa0000000002", "img:sel:aa0000000003")
              for b in flat))

# single-update guard
bot.resolve_container = lambda q: CONTAINERS[1]
ok, msg = bot._update_container_inner("aa0000000002")
check("update_guard", ok is False and "附属数据库" in msg and "moviepilot" in msg, msg)
bot.resolve_container = lambda q: CONTAINERS[3]
bot.docker_pull = lambda ref, timeout=None: (False, "拉取失败 404")
ok2, msg2 = bot._update_container_inner("aa0000000004")
check("main_not_guarded", "附属数据库" not in msg2, msg2)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
