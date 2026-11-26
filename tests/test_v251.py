"""bot3 v2.5.1 tests: container-update image-ref resolution.

Covers: list API returning a short image ID after the tag moved to a newer
image (bot must pull Config.Image instead), ID-only refs skipped instead of
404, local builds (no RepoDigests) skipped, registry auth header built from
a docker config file, report wording when some containers could not be
checked.
Run: BOT_TOKEN=x OWNER_CHAT_ID=1 python3 tests/test_v251.py
"""
import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot

bot.DATA_DIR = tempfile.mkdtemp(prefix="bot3v251-")
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" {extra}" if extra and not cond else ""))


check("version", bot.BOT_VERSION == "2.5.1")
check("welcome_version", "v2.5.1" in bot.WELCOME)

# ---------- ref resolution ----------
c_hex = {"Id": "c1", "Names": ["/avdb"], "Image": "4549f6f47a11"}
insp = {"Image": "sha256:4549f6f47a11aa", "Config": {"Image": "leolitaly/avdb:latest"}}
check("ref_prefers_config", bot._container_image_ref(c_hex, insp) == "leolitaly/avdb:latest")
check("ref_fallback_list", bot._container_image_ref(c_hex, {"Image": "sha256:x"}) == "4549f6f47a11")
check("id_ref_short", bot._is_image_id_ref("4549f6f47a11"))
check("id_ref_sha256", bot._is_image_id_ref("sha256:" + "a" * 64))
check("id_ref_name_ok", not bot._is_image_id_ref("leolitaly/avdb:latest"))
check("id_ref_digest_ok", not bot._is_image_id_ref("pinned@sha256:abc"))
check("registry_ghcr", bot._registry_of("ghcr.io/wdw6733/mp-rag-bot") == "ghcr.io")
check("registry_hub", bot._registry_of("leolitaly/avdb") == "docker.io")
check("registry_lscr", bot._registry_of("lscr.io/linuxserver/emby") == "lscr.io")

# ---------- registry auth header ----------
cfg = {"auths": {"ghcr.io": {"auth": base64.b64encode(b"user:secretpw").decode()},
                 "https://index.docker.io/v1/": {"username": "hubuser", "password": "hubpw"}}}
cfg_path = os.path.join(bot.DATA_DIR, "config.json")
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg, f)
bot._docker_config_paths = lambda: [cfg_path]
hdr = bot._registry_auth_header("ghcr.io/wdw6733/mp-rag-bot")
payload = json.loads(base64.urlsafe_b64decode(hdr.encode()))
check("auth_ghcr", payload.get("username") == "user" and payload.get("password") == "secretpw"
      and payload.get("serveraddress") == "ghcr.io", str(payload))
hdr2 = bot._registry_auth_header("leolitaly/avdb")
payload2 = json.loads(base64.urlsafe_b64decode(hdr2.encode()))
check("auth_hub", payload2.get("username") == "hubuser", str(payload2))
check("auth_none", bot._registry_auth_header("lscr.io/linuxserver/emby") is None)

# ---------- full check flow with NAS-shaped data ----------
CONTAINERS = [
    # tag moved: list shows short ID, Config.Image has the real ref
    {"Id": "aa01", "Names": ["/avdb"], "Image": "4549f6f47a11"},
    # normal current container
    {"Id": "aa02", "Names": ["/web"], "Image": "web:1.0"},
    # local build: no RepoDigests
    {"Id": "aa03", "Names": ["/localapp"], "Image": "myapp:local"},
    # Config.Image itself is an ID: must skip, not pull garbage
    {"Id": "aa04", "Names": ["/orphan"], "Image": "bbbbbbbbbbbb"},
    # digest pinned: existing skipped path
    {"Id": "aa05", "Names": ["/pinned"], "Image": "pinned@sha256:abc"},
]
INSPECTS = {
    "aa01": {"Image": "sha256:oldavdb", "Config": {"Image": "leolitaly/avdb:latest"}},
    "aa02": {"Image": "sha256:sameweb", "Config": {"Image": "web:1.0"}},
    "aa03": {"Image": "sha256:localimg", "Config": {"Image": "myapp:local"}},
    "aa04": {"Image": "sha256:orphanimg", "Config": {"Image": "sha256:" + "b" * 64}},
    "aa05": {"Image": "sha256:pinimg", "Config": {"Image": "pinned@sha256:abc"}},
}
pulled = []


def fake_pull(image, timeout=300):
    pulled.append(image)
    if "@" in image:
        raise ValueError("带 digest 的镜像暂不支持更新")
    return True, "pulled"


def fake_json(method, path, timeout=30):
    if path.startswith("/images/sha256%3Alocalimg"):
        return {"Id": "sha256:localimg", "RepoDigests": []}
    if path.startswith("/images/sha256%3A"):
        return {"Id": path, "RepoDigests": ["repo@sha256:digest"]}
    if "leolitaly%2Favdb%3Alatest" in path:
        return {"Id": "sha256:newavdb"}
    if "web%3A1.0" in path:
        return {"Id": "sha256:sameweb"}
    raise KeyError(path)


bot.list_containers = lambda: CONTAINERS
bot.inspect_container = lambda cid: INSPECTS[cid]
bot.docker_pull = fake_pull
bot.docker_json = fake_json
results = bot.check_container_image_updates()
by_name = {r["name"]: r for r in results}
check("check_avdb_update", by_name["avdb"]["status"] == "update"
      and by_name["avdb"]["image"] == "leolitaly/avdb:latest", str(results))
check("check_web_current", by_name["web"]["status"] == "current")
check("check_local_skipped", by_name["localapp"]["status"] == "skipped"
      and "本地构建" in by_name["localapp"]["error"])
check("check_orphan_skipped", by_name["orphan"]["status"] == "skipped"
      and "只剩 ID" in by_name["orphan"]["error"])
check("check_pinned_skipped", by_name["pinned"]["status"] == "skipped")
check("check_pulled_refs", pulled == ["leolitaly/avdb:latest", "pinned@sha256:abc", "web:1.0"],
      str(pulled))

# report wording: no updates but problems -> must not claim all latest
text, _ = bot.format_image_check_report(
    [{"cid": "x", "name": "avdb", "image": "leolitaly/avdb:latest",
      "status": "error", "error": "拉取失败 404"}])
check("report_problem_wording", "没检查成" in text and "全部容器都已是最新" not in text, text)
text2, _ = bot.format_image_check_report(
    [{"cid": "x", "name": "web", "image": "web:1.0", "status": "current",
      "old_id": "sha256:a", "new_id": "sha256:a"}])
check("report_all_latest", "全部容器都已是最新" in text2, text2)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
