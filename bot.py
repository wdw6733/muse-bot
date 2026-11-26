#!/usr/bin/env python3
"""3 号机器人：NAS + Docker + ESXi 管理（v2.5.4：附属数据库不单独升级）。

部署在 NAS（ESXi 上的 VM）里的 Docker 容器中，通过 OpenBox SOCKS5 代理
访问 Telegram Bot API（长轮询，不需要 webhook、不需要开 inbound 端口）。

- 命令（/status、/docker、/esxi、/container_updates、/alerts、/help）都能用。
- /start 会发主菜单快捷按钮，点按钮直接查 NAS、管容器（本机秒回）。
- Docker：直接读 /var/run/docker.sock 调 Engine API（不依赖 docker CLI）。
- NAS 主机状态：读只读挂载的 /host/proc、/host/sys 和 df /host；温度/风扇读 ESXi。
- ESXi：SSH（paramiko）执行 vim-cmd / esxcli。
- 只认 OWNER_CHAT_ID 一个人；没有任意命令执行功能。
- 危险操作（停容器、重启容器、关 VM、重启 NAS）要点两次确认。
- v2.5.4：附属数据库不单独升级：按 Compose 项目识别 postgres/redis 等附属服务，
  检查时不列更新、单容器更新入口也拦截；数据库版本由 Compose/主程序决定。
- v2.5.3：容器更新报告去重（正文只留汇总，容器列表只在按钮里）+ 批量勾选：
  点容器名勾选/取消，可全选，选好后一次确认批量升级或批量忽略。
- v2.5.2：容器更新报告缓存可返回：升级完自动回到剩余更新列表，不用重新全量检测；
  主菜单“容器更新”先显示上次检查结果，报告里有“重新检查”按钮强制重查。
- v2.5.1：容器更新检查改用 inspect 的 Config.Image 解析镜像名（列表接口在标签被顶掉后
  只给短 ID，拿去拉取会 404）；本地构建镜像跳过检查；私有仓库拉取自动带 NAS 上
  docker login 存的凭据。
- v2.5.0：打字说“内存 TOP5”直接查（只读）；说“重启 X”先报计划，点 ✅ 才执行，5 分钟过期；
  TrueNAS 系统服务（ssh/cifs/nfs 等）走 NAS 主机 SSH 的 midclt，名字先对实时服务列表校验，
  不在列表里的不执行；middlewared、docker 等关键服务永不重启。
- 主动告警：容器意外停了、磁盘/负载/内存超阈值会主动提醒（可关）。
- 自我更新：主人发 bot.py 文件，验证后安装重启。
- 容器更新：拉新镜像并重建，旧容器保留到新容器跑起来后才删；可忽略某次更新。
- CPU/内存/磁盘用量都用 🟥/🟩 彩条显示；容器管理改成 3 列网格。
- 容器镜像更新检测：每 6 小时自动检查全部容器，有新镜像就通知，点按钮确认后才更新。
- 容器/VM 列表按钮文字等宽对齐：名字按显示宽度补齐，同一行 🟢/🔴 圆点横向对齐。
- 删除容器只删容器本身（docker rm 不带 -v），绝不删数据卷。
"""

import html
import http.client
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# ---------------- 配置 ----------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID", "0") or 0)
SOCKS_PROXY = os.environ.get("SOCKS_PROXY", "").strip()
ESXI_HOST = os.environ.get("ESXI_HOST", "").strip()
ESXI_USER = os.environ.get("ESXI_USER", "root").strip()
ESXI_PASSWORD = os.environ.get("ESXI_PASSWORD", "")
ESXI_KEY_FILE = os.environ.get("ESXI_KEY_FILE", "").strip()
NAS_VM_NAME = os.environ.get("NAS_VM_NAME", "").strip()
NAS_VM_ID = os.environ.get("NAS_VM_ID", "").strip()
# TrueNAS 系统服务（v2.5.0）：SSH 优先（midclt），API 备用（REST v2.0，TrueNAS 26 已移除）。
# 填了 SSH 凭据就用 SSH；只填 API_KEY 就用 API；都没填则系统服务功能提示未配置。
TRUENAS_HOST = os.environ.get("TRUENAS_HOST", "192.168.124.251").strip()
TRUENAS_SSH_PORT = _env_int("TRUENAS_SSH_PORT", 22)
TRUENAS_SSH_USER = os.environ.get("TRUENAS_SSH_USER", "root").strip()
TRUENAS_SSH_PASSWORD = os.environ.get("TRUENAS_SSH_PASSWORD", "").strip()
TRUENAS_SSH_KEY_FILE = os.environ.get("TRUENAS_SSH_KEY_FILE", "").strip()
TRUENAS_API_URL = os.environ.get("TRUENAS_API_URL", "").strip().rstrip("/")
TRUENAS_API_KEY = os.environ.get("TRUENAS_API_KEY", "").strip()
_HOST_DATA_PATHS_RAW = os.environ.get("HOST_DATA_PATHS", "").strip()
# 留空默认看 /mnt/PT 和 /mnt/Strongge（NAS 的两个数据目录）；.env 里填了就按填的来。
HOST_DATA_PATHS = [p.strip() for p in _HOST_DATA_PATHS_RAW.split(",") if p.strip()] or ["/mnt/PT", "/mnt/Strongge"]
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
BOT_VERSION = "2.5.4"
SELF_CONTAINER_NAME = os.environ.get("SELF_CONTAINER_NAME", "tg-bot3").strip() or "tg-bot3"
ALERT_ENABLED_DEFAULT = os.environ.get("ALERT_ENABLED", "1").strip() not in ("0", "false", "no")
ALERT_INTERVAL_SEC = _env_int("ALERT_INTERVAL_SEC", 300)
ALERT_DISK_PCT = _env_float("ALERT_DISK_PCT", 90)
ALERT_LOAD_MULT = _env_float("ALERT_LOAD_MULT", 2.0)
ALERT_MEM_PCT = _env_float("ALERT_MEM_PCT", 92)
ALERT_COOLDOWN_SEC = _env_int("ALERT_COOLDOWN_SEC", 3600)
IMAGE_CHECK_ENABLED = os.environ.get("IMAGE_CHECK_ENABLED", "1").strip() not in ("0", "false", "no")
IMAGE_CHECK_INTERVAL_SEC = _env_int("IMAGE_CHECK_INTERVAL_SEC", 21600)

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
MAX_REPLY = 3800
MANAGE_PAGE_SIZE = 9  # 容器管理 3 列 × 3 行

session = requests.Session()
if SOCKS_PROXY:
    session.proxies = {"http": SOCKS_PROXY, "https": SOCKS_PROXY}


def log(*args):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), *args, flush=True)


def cst_now():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def human_bytes(n):
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024.0 or unit == "T":
            if unit == "B":
                return f"{int(n)}B"
            return f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}T"


def fmt_gib_from_kib(kib):
    return f"{kib / 1024.0 / 1024.0:.1f}G"


def bar(pct, width=10):
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(round(pct / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


def color_bar(pct, width=10):
    """用量柱状条：单色填充 + 白色留白，填充色按阈值变（<70% 绿，70–90% 橙，>=90% 红），
    填充与留白对比明显，一眼能看出用了多少。TG 消息没有文字颜色，只能用色块。"""
    pct = max(0.0, min(100.0, float(pct)))
    filled = max(0, min(width, int(pct / 100.0 * width + 0.5)))
    if pct >= 90:
        fill = "🟥"
    elif pct >= 70:
        fill = "🟧"
    else:
        fill = "🟩"
    return fill * filled + "⬜" * (width - filled)


def truncate(text, limit=3800):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（已截断）"


# ---------------- Telegram ----------------

def tg_call(method, payload, timeout=60):
    resp = session.post(f"{API}/{method}", json=payload, timeout=timeout)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} 失败：{data}")
    return data["result"]


def send_reply(chat_id, text, reply_to=None, reply_markup=None, parse_mode=None):
    chunks = []
    rest = text
    while len(rest) > MAX_REPLY:
        cut = rest.rfind("\n", 0, MAX_REPLY)
        if cut <= 0:
            cut = MAX_REPLY
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    if not chunks:
        chunks = [""]
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to and i == 0:
            payload["reply_parameters"] = {"message_id": reply_to}
        if reply_markup and i == 0:
            payload["reply_markup"] = reply_markup
        tg_call("sendMessage", payload)


def edit_message(chat_id, message_id, text, reply_markup=None, parse_mode=None):
    payload = {"chat_id": chat_id, "message_id": message_id,
               "text": truncate(text, 4000)}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        return tg_call("editMessageText", payload)
    except RuntimeError as e:
        if "message is not modified" in str(e):
            return None
        raise


def answer_callback(callback_query_id, text=None, show_alert=False):
    try:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
            payload["show_alert"] = show_alert
        tg_call("answerCallbackQuery", payload, timeout=15)
    except Exception as e:
        log("answerCallbackQuery 失败（忽略）：", e)


# ---------------- Docker（Unix socket + Engine API） ----------------

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._sock_path)


def docker_api(method, path, timeout=30):
    conn = UnixHTTPConnection(DOCKER_SOCK, timeout=timeout)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, body
    finally:
        conn.close()


def docker_json(method, path, timeout=30):
    status, body = docker_api(method, path, timeout=timeout)
    if status >= 400:
        raise RuntimeError(f"Docker API {method} {path} 返回 {status}：{body[:200]!r}")
    return json.loads(body) if body else None


def list_containers():
    return docker_json("GET", "/containers/json?all=1")


def container_display_name(c):
    names = c.get("Names") or []
    if names:
        return names[0].lstrip("/")
    return c.get("Id", "?")[:12]


def resolve_container(query):
    items = list_containers()
    exact = [c for c in items
             if query in [n.lstrip("/") for n in c.get("Names", [])]
             or c["Id"].startswith(query)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValueError("匹配到多个容器：" + "、".join(
            container_display_name(c) for c in exact))
    partial = [c for c in items if any(query in n for n in c.get("Names", []))]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise ValueError("匹配到多个容器：" + "、".join(
            container_display_name(c) for c in partial))
    raise ValueError(f"找不到容器：{query}")


def demux_logs(raw):
    """Docker 日志是 8 字节头 + 内容的帧流；tty 容器则是纯文本。"""
    frames = []
    i = 0
    framed = True
    while i + 8 <= len(raw):
        stream, size = raw[i], int.from_bytes(raw[i + 4:i + 8], "big")
        if stream not in (0, 1, 2) or i + 8 + size > len(raw):
            framed = False
            break
        frames.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    if framed and frames:
        return b"".join(frames).decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def container_logs(name, tail=50):
    c = resolve_container(name)
    status, body = docker_api(
        "GET", f"/containers/{c['Id']}/logs?stdout=1&stderr=1&tail={tail}")
    if status >= 400:
        raise RuntimeError(f"读日志失败 {status}")
    return demux_logs(body)


def container_action(name, action):
    c = resolve_container(name)
    path = f"/containers/{c['Id']}/{action}"
    if action == "stop":
        path += "?t=10"
    status, body = docker_api("POST", path)
    if status not in (204, 304):
        raise RuntimeError(f"{action} 失败 {status}：{body[:200]!r}")
    if action == "stop":
        record_intentional_stop(c["Id"])
    return c


def docker_api_body(method, path, body, timeout=60, content_type="application/json"):
    conn = UnixHTTPConnection(DOCKER_SOCK, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers={"Content-Type": content_type})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def docker_json_body(method, path, payload, timeout=60):
    status, body = docker_api_body(method, path, json.dumps(payload).encode("utf-8"), timeout=timeout)
    if status >= 400:
        raise RuntimeError(f"Docker API {method} {path} 返回 {status}：{body[:200]!r}")
    return json.loads(body) if body else None


# 主人主动停的容器，15 分钟内不触发“容器停了”告警。
_INTENTIONAL_STOPS = {}


def record_intentional_stop(cid):
    _INTENTIONAL_STOPS[cid] = time.monotonic() + 900


def inspect_container(cid):
    return docker_json("GET", f"/containers/{cid}/json")


def _split_image(image):
    image = image.strip()
    if "@" in image:
        raise ValueError("带 digest 的镜像暂不支持更新")
    slash = image.rfind("/")
    colon = image.rfind(":")
    if colon > slash:
        return image[:colon], image[colon + 1:]
    return image, "latest"


_IMAGE_ID_RE = re.compile(r"^(sha256:)?[0-9a-f]{12,64}$")


def _is_image_id_ref(ref):
    """列表接口在镜像标签被新镜像顶掉后会把 Image 字段显示成短 ID，
    这种值不是仓库名，拿去拉取只会得到 404 pull access denied。"""
    return bool(_IMAGE_ID_RE.match((ref or "").strip().lower()))


def _container_image_ref(c, inspect_data=None):
    """容器真正用的镜像名：优先 inspect 的 Config.Image（创建时写死的引用），
    列表接口的 Image 在标签被顶掉后会退化成短 ID，不能拿来拉取。"""
    cfg_image = (((inspect_data or {}).get("Config") or {}).get("Image") or "").strip()
    if cfg_image:
        return cfg_image
    return (c.get("Image") or "").strip()


def _registry_of(name):
    first = name.split("/", 1)[0]
    if "/" in name and ("." in first or ":" in first or first == "localhost"):
        return first
    return "docker.io"


def _docker_config_paths():
    paths = []
    cfg_file = os.environ.get("DOCKER_CONFIG_FILE", "").strip()
    if cfg_file:
        paths.append(cfg_file)
    cfg_dir = os.environ.get("DOCKER_CONFIG", "").strip()
    if cfg_dir:
        paths.append(os.path.join(cfg_dir, "config.json"))
    # bot 容器把 NAS 根目录只读挂在 /host，NAS 上 docker login 存的凭据在那里。
    paths.append("/host/root/.docker/config.json")
    paths.append(os.path.expanduser("~/.docker/config.json"))
    paths.append("/root/.docker/config.json")
    return paths


def _registry_auth_header(name):
    """为私有仓库拉取拼 X-Registry-Auth；没有可用凭据返回 None。
    Engine API 拉取不会自动用 docker login 存的凭据，必须显式带上。
    凭据只在内存里拼请求头，绝不写日志。"""
    try:
        registry = _registry_of(name)
        wanted = {registry}
        if registry == "docker.io":
            wanted |= {"index.docker.io", "registry-1.docker.io", "docker.io"}
        auths = {}
        for path in _docker_config_paths():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    auths = (json.load(f) or {}).get("auths") or {}
                if auths:
                    break
            except Exception:
                continue
        for key, entry in auths.items():
            host = re.sub(r"^https?://", "", str(key)).split("/")[0]
            if host not in wanted or not isinstance(entry, dict):
                continue
            if entry.get("identitytoken"):
                payload = {"identitytoken": entry["identitytoken"]}
            else:
                user = entry.get("username", "")
                password = entry.get("password", "")
                if not user and entry.get("auth"):
                    import base64 as _b64
                    decoded = _b64.b64decode(entry["auth"]).decode("utf-8", "replace")
                    user, _, password = decoded.partition(":")
                if not user:
                    continue
                payload = {"username": user, "password": password,
                           "serveraddress": registry}
            import base64 as _b64
            return _b64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    except Exception:
        return None
    return None


def _image_has_repo_digest(image_id):
    """镜像是否来自仓库（有 RepoDigests）。本地构建的没有。
    查不到时返回 None（未知），调用方按可检查处理。"""
    if not image_id:
        return None
    try:
        img = docker_json("GET", "/images/" + quote(image_id, safe="") + "/json")
        return bool((img or {}).get("RepoDigests"))
    except Exception:
        return None


def docker_pull(image, timeout=600):
    """拉取镜像。返回 (ok, message)。流式读进度，遇到 error 即失败。"""
    name, tag = _split_image(image)
    path = f"/images/create?fromImage={quote(name, safe='')}&tag={quote(tag, safe='')}"
    headers = {}
    auth = _registry_auth_header(name)
    if auth:
        headers["X-Registry-Auth"] = auth
    conn = UnixHTTPConnection(DOCKER_SOCK, timeout=timeout)
    try:
        conn.request("POST", path, headers=headers)
        resp = conn.getresponse()
        if resp.status >= 400:
            return False, f"拉取失败 {resp.status}：{resp.read()[:200]!r}"
        last_status = ""
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("error"):
                return False, f"拉取失败：{ev['error']}"
            if ev.get("status"):
                last_status = ev["status"]
        return True, last_status or "拉取完成"
    finally:
        conn.close()


def prune_images():
    """只清悬空镜像（dangling=true），不碰正在用的镜像。返回 (个数, 释放字节)。"""
    result = docker_json("POST", "/images/prune?dangling=true", timeout=60)
    deleted = (result or {}).get("ImagesDeleted") or []
    reclaimed = (result or {}).get("SpaceReclaimed", 0)
    return len(deleted), reclaimed


def search_container_logs(name, keyword, tail=200, max_hits=30):
    """在容器最近 tail 行日志里找关键词（不区分大小写）。返回 (命中行, 命中总数)。"""
    text = container_logs(name, tail)
    kw = keyword.lower()
    hits = [ln for ln in text.splitlines() if kw in ln.lower()]
    return hits[:max_hits], len(hits)


def build_recreate_config(inspect_data, new_image):
    """从 inspect 构造 create 请求体（只保留可安全重建的字段）。

    关键：匿名卷不会自动跟到新容器，必须把 Mounts 里的 volume 显式绑回，
    否则数据会丢在孤卷里。环境变量会原样带过去（重建必需），但绝不显示。
    """
    cfg = inspect_data.get("Config") or {}
    host = inspect_data.get("HostConfig") or {}
    binds = list(host.get("Binds") or [])
    bound_dests = set()
    for b in binds:
        parts = b.split(":")
        if len(parts) >= 2:
            bound_dests.add(parts[1])
    for m in inspect_data.get("Mounts") or []:
        if m.get("Type") == "volume":
            dest = m.get("Destination")
            src = m.get("Name")
            if dest and src and dest not in bound_dests:
                mode = "rw" if m.get("RW") else "ro"
                binds.append(f"{src}:{dest}:{mode}")
                bound_dests.add(dest)
    network_mode = host.get("NetworkMode") or "default"
    if network_mode.startswith("container:"):
        raise ValueError("该容器用了 container: 网络模式，无法安全重建")
    endpoints = {}
    if network_mode not in ("host", "none"):
        nets = ((inspect_data.get("NetworkSettings") or {}).get("Networks") or {})
        for net_name, ep in nets.items():
            endpoints[net_name] = {"Aliases": (ep or {}).get("Aliases") or []}
    host_cfg = {
        "Binds": binds,
        "PortBindings": host.get("PortBindings") or {},
        "RestartPolicy": host.get("RestartPolicy") or {"Name": "no"},
        "NetworkMode": network_mode,
        "Privileged": bool(host.get("Privileged")),
        "CapAdd": host.get("CapAdd") or [],
        "CapDrop": host.get("CapDrop") or [],
        "Devices": host.get("Devices") or [],
        "Dns": host.get("Dns") or [],
        "ExtraHosts": host.get("ExtraHosts") or [],
        "LogConfig": host.get("LogConfig") or {},
        "Memory": host.get("Memory") or 0,
        "NanoCpus": host.get("NanoCpus") or 0,
        "PidMode": host.get("PidMode") or "",
        "IpcMode": host.get("IpcMode") or "",
        "Ulimits": host.get("Ulimits") or [],
        "Tmpfs": host.get("Tmpfs") or {},
        "Sysctls": host.get("Sysctls") or {},
    }
    if host.get("StopTimeout"):
        host_cfg["StopTimeout"] = host["StopTimeout"]
    create = {
        "Image": new_image,
        "User": cfg.get("User") or "",
        "AttachStdin": False, "AttachStdout": False, "AttachStderr": False,
        "Tty": bool(cfg.get("Tty")),
        "OpenStdin": bool(cfg.get("OpenStdin")),
        "StdinOnce": bool(cfg.get("StdinOnce")),
        "Env": list(cfg.get("Env") or []),
        "Cmd": cfg.get("Cmd"),
        "Entrypoint": cfg.get("Entrypoint"),
        "WorkingDir": cfg.get("WorkingDir") or "",
        "Labels": dict(cfg.get("Labels") or {}),
        "ExposedPorts": cfg.get("ExposedPorts") or {},
        "Volumes": cfg.get("Volumes") or {},
        "HostConfig": host_cfg,
    }
    if cfg.get("Hostname"):
        create["Hostname"] = cfg["Hostname"]
    if cfg.get("Domainname"):
        create["Domainname"] = cfg["Domainname"]
    if cfg.get("StopSignal"):
        create["StopSignal"] = cfg["StopSignal"]
    if endpoints:
        create["NetworkingConfig"] = {"EndpointsConfig": endpoints}
    return create


def _container_state(cid):
    try:
        data = inspect_container(cid)
        state = data.get("State") or {}
        return state.get("Status"), (state.get("Health") or {}).get("Status")
    except Exception:
        return None, None


_UPDATE_LOCK = threading.Lock()


def update_container_flow(query):
    """拉取新镜像并重建容器。旧容器保留到新容器跑起来后才删；失败则回滚。"""
    if not _UPDATE_LOCK.acquire(blocking=False):
        return False, "已有容器更新在进行中，稍后再试。"
    try:
        return _update_container_inner(query)
    finally:
        _UPDATE_LOCK.release()


def _update_container_inner(query):
    try:
        c = resolve_container(query)
    except Exception as e:
        return False, f"找不到容器：{e}"
    name = container_display_name(c)
    cid = c["Id"]
    if name == SELF_CONTAINER_NAME:
        return False, "3 号自己不能用这种方式更新：把新版 bot.py 文件发给我，我验证后问你装不装。"
    try:
        inspect_data = inspect_container(cid)
    except Exception as e:
        return False, f"读容器详情失败：{e}"
    image_ref = _container_image_ref(c, inspect_data)
    if not image_ref:
        return False, "读不到该容器的镜像名，无法更新。"
    companion_project = _companion_project_for(c, inspect_data, image_ref)
    if companion_project:
        return False, (f"“{name}”是 {companion_project} 项目的附属数据库，不单独升级："
                       "单独升数据库可能让主程序因版本不兼容起不来。"
                       "数据库版本由 Compose 决定，要升请改 compose 里的镜像标签后用 compose 更新。")
    if _is_image_id_ref(image_ref):
        return False, "该容器的镜像只剩 ID（标签已丢失），无法按名字拉取更新；请用 compose 更新。"
    old_image_id = inspect_data.get("Image", "")
    try:
        ok, pull_msg = docker_pull(image_ref)
    except Exception as e:
        return False, f"拉取镜像失败：{e}"
    if not ok:
        return False, pull_msg
    try:
        img = docker_json("GET", "/images/" + quote(image_ref, safe="") + "/json")
        if img.get("Id") and old_image_id and img["Id"] == old_image_id:
            return True, f"✅ {name} 的镜像已是最新（{image_ref}），无需重建。"
    except Exception:
        pass
    try:
        create_cfg = build_recreate_config(inspect_data, image_ref)
    except Exception as e:
        return False, f"无法安全重建：{e}"
    temp_name = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"{name}-upd-{int(time.time()) % 100000}")
    try:
        created = docker_json_body("POST", f"/containers/create?name={quote(temp_name, safe='')}",
                                   create_cfg, timeout=60)
    except Exception as e:
        return False, f"创建新容器失败（旧容器未动）：{e}"
    new_id = (created or {}).get("Id")
    if not new_id:
        return False, "创建新容器失败（旧容器未动）：Docker 没有返回新容器 ID。"
    try:
        docker_api("POST", f"/containers/{cid}/stop?t=10", timeout=30)
    except Exception as e:
        log("update: stop old failed:", e)
    status, body = docker_api("POST", f"/containers/{new_id}/start", timeout=30)
    if status >= 400:
        _rollback_update(new_id, cid)
        return False, f"新容器启动失败（已回滚到旧容器）：{body[:200]!r}"
    st, health = None, None
    running = False
    for _ in range(15):
        time.sleep(2)
        st, health = _container_state(new_id)
        if st == "running" and health != "unhealthy":
            running = True
            break
        if st in ("exited", "dead"):
            break
    if not running:
        _rollback_update(new_id, cid)
        return False, f"新容器没有跑起来（状态：{st}），已回滚到旧容器。请看新容器日志排查。"
    warnings = []
    status, _body = docker_api("DELETE", f"/containers/{cid}", timeout=30)
    if status >= 400:
        warnings.append(f"旧容器删除失败（{status}），请手动清理停止的旧容器。")
    status, _body = docker_api("POST", f"/containers/{new_id}/rename?name={quote(name, safe='')}", timeout=30)
    if status >= 400:
        warnings.append(f"新容器已在跑，但改回原名失败（现在叫 {temp_name}）。")
        return True, f"✅ {name} 已更新到新镜像。\n" + "\n".join(warnings)
    msg = f"✅ {name} 已更新到新镜像并重建完成（{image_ref}）。"
    if warnings:
        msg += "\n" + "\n".join(warnings)
    return True, msg


def _rollback_update(new_id, old_id):
    try:
        docker_api("POST", f"/containers/{new_id}/stop?t=5", timeout=20)
    except Exception:
        pass
    try:
        docker_api("DELETE", f"/containers/{new_id}?force=true", timeout=30)
    except Exception as e:
        log("update rollback: remove new failed:", e)
    try:
        docker_api("POST", f"/containers/{old_id}/start", timeout=30)
    except Exception as e:
        log("update rollback: start old failed:", e)


def _send_via_new_session(chat_id, text, reply_markup=None):
    """后台线程发消息：不用主线程的 session，避免并发复用。"""
    sess = requests.Session()
    if SOCKS_PROXY:
        sess.proxies = {"http": SOCKS_PROXY, "https": SOCKS_PROXY}
    payload = {"chat_id": chat_id, "text": truncate(text, MAX_REPLY)}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    resp = sess.post(f"{API}/sendMessage", json=payload, timeout=60)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendMessage 失败：{data}")


def _edit_via_new_session(chat_id, message_id, text, reply_markup=None):
    """后台线程改消息（把“开始更新”那条改回剩余更新列表）。"""
    sess = requests.Session()
    if SOCKS_PROXY:
        sess.proxies = {"http": SOCKS_PROXY, "https": SOCKS_PROXY}
    payload = {"chat_id": chat_id, "message_id": message_id,
               "text": truncate(text, MAX_REPLY)}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    resp = sess.post(f"{API}/editMessageText", json=payload, timeout=60)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram editMessageText 失败：{data}")


def update_container_async(query, chat_id, message_id=None):
    def _run():
        ok = False
        try:
            ok, msg = update_container_flow(query)
        except Exception:
            log("update thread error:", traceback.format_exc(limit=3))
            msg = "更新过程出错了，请看容器日志。"
        if ok:
            _mark_image_check_updated(query)
        try:
            _send_via_new_session(chat_id, msg)
        except Exception as e:
            log("update result send failed:", e)
        # 更新完把那条消息改回剩余更新列表，不用重新全量检测就能接着升下一个。
        cached = _cached_image_report()
        if cached:
            text, markup = cached
            try:
                if message_id:
                    _edit_via_new_session(chat_id, message_id, text, markup)
                else:
                    _send_via_new_session(chat_id, text, markup)
            except Exception as e:
                log("update report restore failed:", e)
                try:
                    _send_via_new_session(chat_id, text, markup)
                except Exception as e2:
                    log("update report send failed:", e2)
    threading.Thread(target=_run, daemon=True).start()


def update_containers_batch_async(cids, chat_id, message_id=None):
    """批量升级：逐个走单容器更新流程，进度写回那条消息，
    全部完成后发汇总并把消息改回剩余更新列表。"""
    def _run():
        entries = {r.get("cid"): r for r in _LAST_IMAGE_CHECK_RESULTS}
        done = []
        for idx, cid in enumerate(cids, 1):
            name = (entries.get(cid) or {}).get("name") or cid[:12]
            if message_id:
                try:
                    _edit_via_new_session(
                        chat_id, message_id,
                        f"⬆️ 批量升级中（{idx}/{len(cids)}）：正在更新“{name}”…")
                except Exception as e:
                    log("batch progress edit failed:", e)
            try:
                ok, msg = update_container_flow(cid[:12])
            except Exception:
                log("batch update thread error:", traceback.format_exc(limit=3))
                ok, msg = False, "更新过程出错了，请看容器日志。"
            if ok:
                _mark_image_check_updated(cid)
            done.append((name, ok, msg))
        ok_names = [n for n, ok, _ in done if ok]
        fail_lines = [f"⚠️ {n}：{m[:60]}" for n, ok, m in done if not ok]
        summary = f"批量升级完成：成功 {len(ok_names)} 个，失败 {len(fail_lines)} 个。"
        if ok_names:
            summary += "\n✅ " + "、".join(ok_names)
        if fail_lines:
            summary += "\n" + "\n".join(fail_lines)
        try:
            _send_via_new_session(chat_id, summary)
        except Exception as e:
            log("batch summary send failed:", e)
        cached = _cached_image_report()
        if cached:
            text, markup = cached
            try:
                if message_id:
                    _edit_via_new_session(chat_id, message_id, text, markup)
                else:
                    _send_via_new_session(chat_id, text, markup)
            except Exception as e:
                log("batch report restore failed:", e)
                try:
                    _send_via_new_session(chat_id, text, markup)
                except Exception as e2:
                    log("batch report send failed:", e2)
    threading.Thread(target=_run, daemon=True).start()


# ---------------- NAS 主机状态 ----------------

def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _meminfo():
    mem = {}
    text = _read_text("/host/proc/meminfo")
    if text:
        for line in text.splitlines():
            k, _, v = line.partition(":")
            parts = v.strip().split()
            if parts:
                try:
                    mem[k.strip()] = int(parts[0])
                except ValueError:
                    pass
    return mem


def _cpu_times():
    text = _read_text("/host/proc/stat")
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("cpu "):
            fields = [int(x) for x in line.split()[1:]]
            total = sum(fields)
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
            return total, idle
    return None


def cpu_percent():
    first = _cpu_times()
    if first is None:
        return None
    time.sleep(1.0)
    second = _cpu_times()
    if second is None:
        return None
    total_delta = second[0] - first[0]
    idle_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    return (1.0 - idle_delta / total_delta) * 100.0


def _loadavg():
    text = _read_text("/host/proc/loadavg")
    if not text:
        return None
    parts = text.split()
    return parts[:3] if len(parts) >= 3 else None


def _uptime_text():
    text = _read_text("/host/proc/uptime")
    if not text:
        return None
    try:
        seconds = int(float(text.split()[0]))
    except (ValueError, IndexError):
        return None
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days} 天 {hours} 小时 {minutes} 分"
    return f"{hours} 小时 {minutes} 分"


def _cpu_count():
    text = _read_text("/host/proc/cpuinfo")
    if text:
        n = text.count("processor\t:")
        if n:
            return n
    return os.cpu_count() or 1


def disk_usages():
    paths = ["/host"]
    for p in HOST_DATA_PATHS:
        hp = "/host" + p
        if os.path.exists(hp):
            paths.append(hp)
        else:
            log("磁盘路径不存在，已跳过（检查挂载）：", hp)
    try:
        r = subprocess.run(["df", "-h"] + paths, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    seen = set()
    out = []
    for line in r.stdout.strip().splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        fs, size, used, avail, pct_s, mount = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
        key = (fs, mount)
        if key in seen:
            continue
        seen.add(key)
        try:
            pct = float(pct_s.rstrip("%"))
        except ValueError:
            pct = 0.0
        out.append({"fs": fs, "size": size, "used": used, "avail": avail,
                    "pct": pct, "mount": mount})
    return out


def _net_rates():
    """1 秒采样网卡速率。返回 [(网卡, 累计下行字节, 累计上行字节, 下行速率, 上行速率)]。"""
    first = _read_net_dev()
    if not first:
        return []
    time.sleep(1.0)
    second = _read_net_dev()
    rows = []
    for iface, (rx2, tx2) in second.items():
        if iface == "lo":
            continue
        rx1, tx1 = first.get(iface, (rx2, tx2))
        rows.append((iface, rx2, tx2, max(0, rx2 - rx1), max(0, tx2 - tx1)))
    rows.sort(key=lambda r: r[1] + r[2], reverse=True)
    return rows


def fmt_status():
    lines = [f"💻 NAS 状态 · {cst_now()}"]
    up = _uptime_text()
    if up:
        lines.append(f"⏱ 已运行：{up}")
    cpu = cpu_percent()
    if cpu is not None:
        lines.append(f"🖥️ CPU：{color_bar(cpu)} {cpu:.0f}%")
    load = _loadavg()
    if load:
        lines.append(f"📊 负载：{' / '.join(load)}（{_cpu_count()} 线程）")
    mem = _meminfo()
    if mem.get("MemTotal"):
        total = mem["MemTotal"]
        avail = mem.get("MemAvailable", 0)
        used = total - avail
        pct = used / total * 100.0 if total else 0.0
        lines.append(f"🧠 内存：{color_bar(pct)} {pct:.0f}% · {fmt_gib_from_kib(used)} / {fmt_gib_from_kib(total)}")
        if mem.get("SwapTotal"):
            swap_used = mem["SwapTotal"] - mem.get("SwapFree", 0)
            lines.append(f"🔀 交换：{fmt_gib_from_kib(swap_used)} / {fmt_gib_from_kib(mem['SwapTotal'])}")
    for d in disk_usages():
        mount = d["mount"]
        label = mount[5:] if mount.startswith("/host") and len(mount) > 5 else "根分区"
        if mount == "/host":
            label = "根分区"
        lines.append(f"💾 {label}：{color_bar(d['pct'])} {d['pct']:.0f}% · 剩 {d['avail']} / 共 {d['size']}")
    for iface, rx, tx, rx_rate, tx_rate in _net_rates()[:3]:
        lines.append(f"🌍 {iface}：↓ {human_bytes(rx_rate)}/s ↑ {human_bytes(tx_rate)}/s · 累计 ↓ {human_bytes(rx)} ↑ {human_bytes(tx)}")
    temps, fans = esxi_temperature()
    if temps:
        lines.append("🌡️ ESXi 温度：" + " · ".join(f"{label} {v:.1f}°C" for label, v in temps[:4]))
    if fans:
        lines.append("🌀 ESXi 风扇：" + " · ".join(f"{label} {v:.0f} RPM" for label, v in fans[:4]))
    try:
        items = list_containers()
        running = sum(1 for c in items if c.get("State") == "running")
        lines.append(f"🐳 容器：运行 {running} / 共 {len(items)}")
    except Exception as e:
        lines.append(f"容器：查不到（{e}）")
    return "\n".join(lines)


def _read_procs():
    procs = {}
    try:
        pids = [d for d in os.listdir("/host/proc") if d.isdigit()]
    except OSError:
        return procs
    for pid in pids:
        stat = _read_text(f"/host/proc/{pid}/stat")
        if not stat:
            continue
        rparen = stat.rfind(")")
        if rparen < 0:
            continue
        name = stat[stat.find("(") + 1:rparen]
        parts = stat[rparen + 2:].split()
        if len(parts) < 13:
            continue
        try:
            total_jiffies = int(parts[11]) + int(parts[12])
        except ValueError:
            continue
        rss_kb = 0
        status = _read_text(f"/host/proc/{pid}/status")
        if status:
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    try:
                        rss_kb = int(line.split()[1])
                    except (ValueError, IndexError):
                        pass
                    break
        procs[pid] = (name, total_jiffies, rss_kb)
    return procs


def top_text():
    """进程 TOP：等宽 <pre> 列表（名字 / CPU% / 内存），前 10 名。调用方须用 parse_mode="HTML" 发送。"""
    try:
        clk = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        clk = 100
    first = _read_procs()
    if not first:
        return "进程读不到 /host/proc，请确认 /proc 已挂载到 /host/proc。"
    t0 = time.monotonic()
    time.sleep(1.0)
    second = _read_procs()
    elapsed = max(0.1, time.monotonic() - t0)
    rows = []
    for pid, (name, total2, rss) in second.items():
        if pid not in first:
            continue
        delta = total2 - first[pid][1]
        cpu = delta / (elapsed * clk) * 100.0 if delta > 0 else 0.0
        rows.append((cpu, rss, pid, name))
    rows.sort(reverse=True)
    lines = ["⬆️ 进程 TOP（按 CPU，1 秒采样）", "<pre>"]
    lines.append(f"{'NAME':<18}{'CPU%':>7}{'MEM':>9}")
    for cpu, rss, _pid, name in rows[:10]:
        lines.append(f"{html.escape(pad_name(name, 18))}{cpu:>6.1f}%{human_bytes(rss * 1024):>9}")
    lines.append("</pre>")
    lines.append(f"共 {len(second)} 个进程")
    return "\n".join(lines)

def top_mem_text():
    """内存 TOP5：按 VmRSS 从大到小，前 5 名。只读，立即执行，不用确认。"""
    procs = _read_procs()
    if not procs:
        return "进程读不到 /host/proc，请确认 /proc 已挂载到 /host/proc。"
    rows = sorted(((rss, pid, name) for pid, (name, _j, rss) in procs.items()),
                  reverse=True)
    total_kb = sum(rss for rss, _p, _n in rows)
    lines = ["🧠 内存 TOP5（按 VmRSS）", "<pre>"]
    lines.append(f"{'NAME':<18}{'MEM':>9}{'SHARE':>7}")
    for rss, _pid, name in rows[:5]:
        share = (rss / total_kb * 100.0) if total_kb else 0.0
        lines.append(f"{html.escape(pad_name(name, 18))}{human_bytes(rss * 1024):>9}{share:>6.1f}%")
    lines.append("</pre>")
    lines.append(f"共 {len(rows)} 个进程，总占用 {human_bytes(total_kb * 1024)}")
    return "\n".join(lines)


# ---------------- TrueNAS 系统服务（v2.5.0） ----------------
# 目标只能是 TrueNAS 自己的服务（ssh/cifs/nfs/ftp/rsync/smartd/snmp/ups/webdav/s3/iscsi…），
# 执行前先对实时服务列表校验名字，不在列表里的拒绝；middlewared 永远不碰。
# 传输：NAS 主机 SSH（midclt call service.*）优先；没配 SSH 才用 REST API
# （POST {TRUENAS_API_URL}/service/{start|stop|restart} {"service": name}，TrueNAS 26 已移除 REST）。

TRUENAS_BLOCKED_SERVICES = {"middlewared", "docker", "containerd", "systemd-journald"}


def truenas_ssh_configured():
    return bool(TRUENAS_HOST and (TRUENAS_SSH_PASSWORD or TRUENAS_SSH_KEY_FILE))


def truenas_api_configured():
    return bool(TRUENAS_API_URL and TRUENAS_API_KEY)


def truenas_configured():
    return truenas_ssh_configured() or truenas_api_configured()


def truenas_ssh_run(commands, connect_timeout=15, exec_timeout=30):
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {"hostname": TRUENAS_HOST, "port": TRUENAS_SSH_PORT, "username": TRUENAS_SSH_USER,
          "timeout": connect_timeout, "allow_agent": False, "look_for_keys": False}
    if TRUENAS_SSH_KEY_FILE:
        kw["key_filename"] = TRUENAS_SSH_KEY_FILE
    else:
        kw["password"] = TRUENAS_SSH_PASSWORD
    client.connect(**kw)
    try:
        results = []
        for cmd in commands:
            _, stdout, stderr = client.exec_command(cmd, timeout=exec_timeout)
            results.append((stdout.read().decode("utf-8", errors="replace"),
                            stderr.read().decode("utf-8", errors="replace")))
        return results
    finally:
        client.close()


def truenas_services():
    """实时服务列表：[(name, state)]。SSH 的 midclt 优先，API 备用。"""
    if truenas_ssh_configured():
        out, err = truenas_ssh_run(["midclt call service.query"])[0]
        if err.strip():
            raise RuntimeError(f"midclt 报错：{err.strip()[:200]}")
        items = json.loads(out)
        return [(str(i.get("service", "")), str(i.get("state", "?"))) for i in items if i.get("service")]
    if truenas_api_configured():
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        r = requests.get(f"{TRUENAS_API_URL}/service", timeout=20,
                         headers={"Authorization": f"Bearer {TRUENAS_API_KEY}"}, verify=False)
        if r.status_code == 404:
            raise RuntimeError("TrueNAS API 返回 404：这台 NAS 的版本已移除 REST v2.0，请改用 SSH 方式。")
        r.raise_for_status()
        items = r.json()
        return [(str(i.get("service", "")), str(i.get("state", "?"))) for i in items if i.get("service")]
    raise RuntimeError("TrueNAS 未配置：在 .env 里填 TRUENAS_SSH_KEY_FILE/TRUENAS_SSH_PASSWORD 或 TRUENAS_API_KEY 后重启容器。")


def truenas_service_action(name, action):
    """校验名字在实时列表里、动作合法、不是禁区服务，才执行。返回执行结果文字。"""
    if action not in ("start", "stop", "restart"):
        raise ValueError(f"不支持的操作：{action}")
    name = (name or "").strip().lower()
    if not name or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise ValueError(f"服务名不合法：{name!r}")
    if name in TRUENAS_BLOCKED_SERVICES:
        raise ValueError(f"服务 {name} 是关键服务，不允许经机器人重启/启停。")
    services = truenas_services()
    known = {n for n, _s in services}
    if name not in known:
        raise ValueError(f"没有这个服务：{name}。发“系统服务”看实时列表。")
    label = {"start": "启动", "stop": "停止", "restart": "重启"}[action]
    if truenas_ssh_configured():
        out, err = truenas_ssh_run([f"midclt call service.{action} {name}"])[0]
        if err.strip():
            raise RuntimeError(f"midclt 报错：{err.strip()[:200]}")
        ok = out.strip().lower() in ("true", "1")
        return f"✅ {label}服务 {name}：{'成功' if ok else '返回 ' + out.strip()[:100]}"
    # API 备用
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    r = requests.post(f"{TRUENAS_API_URL}/service/{action}", timeout=30,
                      headers={"Authorization": f"Bearer {TRUENAS_API_KEY}"},
                      json={"service": name}, verify=False)
    if r.status_code == 404:
        raise RuntimeError("TrueNAS API 返回 404：这台 NAS 的版本已移除 REST v2.0，请改用 SSH 方式。")
    r.raise_for_status()
    return f"✅ {label}服务 {name}：成功"


def truenas_services_text():
    try:
        services = truenas_services()
    except Exception as e:
        return f"系统服务查不到：{e}"
    lines = ["🖥️ TrueNAS 系统服务（实时）"]
    for name, state in sorted(services):
        dot = "🟢" if state.upper() == "RUNNING" else "🔴"
        lines.append(f"{dot} {name} {state}")
    lines.append(f"\n共 {len(services)} 个。说“重启 <服务名>”会先让你点 ✅ 确认再执行。")
    return "\n".join(lines)


# ---------------- 操作授权确认（v2.5.0） ----------------
# 打字发起的操作（意图可能被理解错）必须先报计划、点 ✅ 才执行；明确敲的 /docker、/esxi
# 命令和按钮里的 c:ask:/e:ask: 本来就是主人明确点的，走各自原有的确认流程。
# 待确认的操作只存内存，5 分钟过期，重启容器即作废；token 是随机数，猜不到。

_PENDING_OPS = {}
_OP_TTL_SEC = 300
_OP_LABEL = {"start": "启动", "stop": "停止", "restart": "重启"}


def request_op_approval(chat_id, message_id, kind, target, action, detail):
    token = secrets.token_hex(4)
    _PENDING_OPS[token] = (kind, target, action, time.monotonic() + _OP_TTL_SEC)
    label = _OP_LABEL.get(action, action)
    kind_label = {"container": "容器", "service": "系统服务", "esxi": "VM"}.get(kind, kind)
    send_reply(chat_id,
               f"⚠️ 确定要{label}{kind_label}“{target}”吗？\n{detail}\n"
               f"点 ✅ 才执行，❌ 取消；5 分钟内有效，超时作废。",
               reply_to=message_id,
               reply_markup=confirm_keyboard(f"op:do:{token}", f"op:cancel:{token}"))
    return token


def _take_pending_op(token):
    item = _PENDING_OPS.pop(token, None)
    if not item:
        return None
    kind, target, action, expiry = item
    if time.monotonic() > expiry:
        return None
    return kind, target, action


def execute_pending_op(token):
    """执行已确认的操作，返回结果文字。token 已在 _take_pending_op 里消费掉。"""
    item = _take_pending_op(token)
    if item is None:
        raise ValueError("这条确认已过期或已执行过，请重新发一次操作请求。")
    kind, target, action = item
    label = _OP_LABEL.get(action, action)
    if kind == "container":
        c = container_action(target, action)
        return f"✅ {label}已执行：{container_display_name(c)}"
    if kind == "service":
        return truenas_service_action(target, action)
    if kind == "esxi":
        v, out = esxi_power(target, action)
        return f"{v['name']}（{v['vmid']}）{label}：{out or '命令已发送'}"
    raise ValueError(f"未知的操作类型：{kind}")


_OP_WORDS = (("重启", "restart"), ("restart", "restart"),
             ("停止", "stop"), ("停掉", "stop"), ("关闭", "stop"), ("stop", "stop"),
             ("启动", "start"), ("开启", "start"), ("start", "start"))


def parse_op_request(text):
    """从一句话里解析（动作，目标），解析不出返回 (None, None)。只认固定动作词，目标只取一个词。"""
    low = text.strip().lower()
    action = None
    for word, act in _OP_WORDS:
        if word in low:
            action = act
            low = low.replace(word, " ", 1)
            break
    if not action:
        return None, None
    for junk in ("请", "帮我", "把", "一下", "吧", "的", "给", "要", "想", "需要"):
        low = low.replace(junk, " ")
    parts = [p for p in re.split(r"[\s,，。、]+", low) if p]
    if not parts:
        return None, None
    target = parts[0]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", target):
        return None, None
    return action, target


def _read_net_dev():
    text = _read_text("/host/proc/net/dev")
    out = {}
    if not text:
        return out
    for line in text.splitlines():
        if ":" not in line:
            continue
        iface, _, rest = line.partition(":")
        iface = iface.strip()
        fields = rest.split()
        if len(fields) < 16:
            continue
        try:
            out[iface] = (int(fields[0]), int(fields[8]))
        except ValueError:
            continue
    return out


def _fetch_ip(url, use_proxy):
    try:
        if use_proxy:
            resp = session.get(url, timeout=15)
        else:
            direct = requests.Session()
            direct.trust_env = False
            direct.proxies = {}
            resp = direct.get(url, timeout=8)
        data = resp.json()
        ip = data.get("ip", "") if isinstance(data, dict) else ""
        return ip.strip() or None
    except Exception:
        return None


def public_ip_text():
    direct4 = _fetch_ip("https://api.ipify.org?format=json", use_proxy=False)
    direct6 = _fetch_ip("https://api6.ipify.org?format=json", use_proxy=False)
    proxy4 = _fetch_ip("https://api.ipify.org?format=json", use_proxy=True)
    lines = ["🌐 公网 IP"]
    lines.append(f"直连出口 IPv4：{direct4 or '查不到'}")
    lines.append(f"直连出口 IPv6：{direct6 or '查不到（容器可能没有 IPv6 出口）'}")
    lines.append(f"代理出口 IPv4：{proxy4 or '查不到'}（走 OpenBox 代理）")
    return "\n".join(lines)


# ---------------- 主动告警 ----------------

_ALERT_STATE = {
    "running": None,       # {id: name} 上次检查时运行中的容器；None 表示尚未建立基线
    "disk_alerted": set(),
    "load_until": 0.0,
    "mem_until": 0.0,
    "last_check": None,
    "last_error": None,
}


def _alerts_file():
    return os.path.join(DATA_DIR, "alerts.json")


def alerts_enabled():
    try:
        with open(_alerts_file(), encoding="utf-8") as f:
            return bool(json.load(f).get("enabled", ALERT_ENABLED_DEFAULT))
    except (OSError, ValueError):
        return ALERT_ENABLED_DEFAULT


def set_alerts_enabled(enabled):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _alerts_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"enabled": bool(enabled)}, f)
    os.replace(tmp, _alerts_file())


def _alert_init_baseline():
    try:
        items = list_containers()
    except Exception as e:
        log("告警基线建立失败（下轮重试）：", e)
        return
    _ALERT_STATE["running"] = {c["Id"]: container_display_name(c)
                               for c in items if c.get("State") == "running"}


def health_check_once():
    """跑一轮检查，返回告警文本列表。只读检查，基线照常更新（关了告警也不攒旧账）。"""
    now = time.monotonic()
    enabled = alerts_enabled()
    alerts = []
    for cid in [k for k, exp in _INTENTIONAL_STOPS.items() if exp < now]:
        del _INTENTIONAL_STOPS[cid]
    try:
        items = list_containers()
    except Exception as e:
        _ALERT_STATE["last_error"] = f"容器列表读不到：{e}"
        items = None
    if items is not None:
        current = {c["Id"]: container_display_name(c)
                   for c in items if c.get("State") == "running"}
        prev = _ALERT_STATE["running"]
        if prev is None:
            _ALERT_STATE["running"] = current
        else:
            stopped = [prev[cid] for cid in prev if cid not in current
                       and _INTENTIONAL_STOPS.get(cid, 0) < now]
            if stopped and enabled:
                alerts.append("🛑 容器停了（非本机器人操作）：\n" + "\n".join(f"· {n}" for n in stopped))
            _ALERT_STATE["running"] = current
    if enabled:
        try:
            for u in disk_usages():
                mount = u["mount"]
                if u["pct"] >= ALERT_DISK_PCT and mount not in _ALERT_STATE["disk_alerted"]:
                    label = mount[5:] if mount.startswith("/host") and len(mount) > 5 else mount
                    alerts.append(f"💾 磁盘快满了：{label} 已用 {u['pct']:.0f}%（{u['used']}/{u['size']}，剩 {u['avail']}）")
                    _ALERT_STATE["disk_alerted"].add(mount)
                elif u["pct"] < ALERT_DISK_PCT - 5:
                    _ALERT_STATE["disk_alerted"].discard(mount)
        except Exception as e:
            _ALERT_STATE["last_error"] = f"磁盘检查失败：{e}"
        try:
            load = _loadavg()
            if load:
                load1 = float(load[0])
                threshold = _cpu_count() * ALERT_LOAD_MULT
                if load1 >= threshold and now >= _ALERT_STATE["load_until"]:
                    alerts.append(f"🔥 负载过高：1 分钟负载 {load1:.2f}（阈值 {threshold:.1f}，{_cpu_count()} 线程 ×{ALERT_LOAD_MULT:g}）")
                    _ALERT_STATE["load_until"] = now + ALERT_COOLDOWN_SEC
        except Exception as e:
            _ALERT_STATE["last_error"] = f"负载检查失败：{e}"
        try:
            mem = _meminfo()
            if mem.get("MemTotal"):
                used_pct = (mem["MemTotal"] - mem.get("MemAvailable", 0)) / mem["MemTotal"] * 100.0
                if used_pct >= ALERT_MEM_PCT and now >= _ALERT_STATE["mem_until"]:
                    alerts.append(f"🧠 内存占用过高：{used_pct:.0f}%（阈值 {ALERT_MEM_PCT:g}%）")
                    _ALERT_STATE["mem_until"] = now + ALERT_COOLDOWN_SEC
        except Exception as e:
            _ALERT_STATE["last_error"] = f"内存检查失败：{e}"
    _ALERT_STATE["last_check"] = cst_now()
    if alerts:
        text = f"🔔 NAS 告警（{cst_now()}）\n\n" + "\n\n".join(alerts)
        try:
            send_reply(OWNER_CHAT_ID, text, reply_markup=main_keyboard())
        except Exception as e:
            log("告警发送失败：", e)
    return alerts


def health_check_safe():
    try:
        health_check_once()
    except Exception:
        log("告警检查异常：", traceback.format_exc(limit=3))


def alerts_status_text():
    running = _ALERT_STATE["running"]
    lines = ["🔔 主动告警",
             f"开关：{'开启' if alerts_enabled() else '关闭'}",
             f"检查间隔：{ALERT_INTERVAL_SEC // 60} 分钟",
             f"磁盘阈值：{ALERT_DISK_PCT:g}%（低于阈值 5% 后重新布防）",
             f"负载阈值：{_cpu_count()} 线程 ×{ALERT_LOAD_MULT:g}",
             f"内存阈值：{ALERT_MEM_PCT:g}%",
             f"同一问题最快 {ALERT_COOLDOWN_SEC // 60} 分钟提醒一次",
             f"监控容器数：{len(running) if running is not None else '基线未建立'}",
             f"上次检查：{_ALERT_STATE['last_check'] or '还没有'}"]
    if _ALERT_STATE["disk_alerted"]:
        lines.append("已告警磁盘：" + "、".join(sorted(_ALERT_STATE["disk_alerted"])))
    if _ALERT_STATE["last_error"]:
        lines.append(f"最近错误：{_ALERT_STATE['last_error']}")
    return "\n".join(lines)


def alerts_keyboard():
    toggle = btn("🔕 关闭告警", "alerts:off") if alerts_enabled() else btn("🔔 开启告警", "alerts:on")
    return kb([[toggle], [btn("⬅️ 主菜单", "menu:main")]])


# ---------------- ESXi（SSH） ----------------

def esxi_configured():
    return bool(ESXI_HOST and (ESXI_PASSWORD or ESXI_KEY_FILE))


def esxi_run(commands, connect_timeout=15, exec_timeout=30):
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {"hostname": ESXI_HOST, "username": ESXI_USER, "timeout": connect_timeout,
          "allow_agent": False, "look_for_keys": False}
    if ESXI_KEY_FILE:
        kw["key_filename"] = ESXI_KEY_FILE
    else:
        kw["password"] = ESXI_PASSWORD
    client.connect(**kw)
    try:
        results = []
        for cmd in commands:
            _, stdout, stderr = client.exec_command(cmd, timeout=exec_timeout)
            results.append((stdout.read().decode("utf-8", errors="replace"),
                            stderr.read().decode("utf-8", errors="replace")))
        return results
    finally:
        client.close()


def esxi_temperature():
    """从 ESXi 读温度/风扇（esxcli hardware ipmi sensor list）。

    返回 (temps, fans)，各为 [(标签, 数值)]；温度单位 °C，风扇单位 RPM。
    ESXi 未配置、命令失败、超时或解析不到读数时返回 ([], [])，
    调用方直接不显示温度段，绝不显示残缺内容。
    """
    if not esxi_configured():
        return [], []
    try:
        (out, _err) = esxi_run(["esxcli hardware ipmi sensor list"],
                               connect_timeout=5, exec_timeout=10)[0]
    except Exception as e:
        log("ESXi 温度读取失败：", e)
        return [], []
    temps, fans = [], []
    cur = {}

    def flush():
        nonlocal cur
        name = (cur.get("name") or "").strip()
        reading = cur.get("reading")
        units = (cur.get("units") or "").strip().lower()
        stype = (cur.get("type") or "").strip().lower()
        cur = {}
        if not name or reading is None:
            return
        try:
            val = float(reading)
        except ValueError:
            return
        if "rpm" in units or "fan" in stype or "fan" in name.lower():
            fans.append((name, val))
        elif "c" in units or "temperature" in stype or "temp" in name.lower():
            temps.append((name, val))

    for line in out.splitlines():
        s = line.strip()
        if line and not line[0].isspace() and s.lower().startswith("sensor"):
            flush()
            continue
        if ":" not in s:
            continue
        key, _, val = s.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "name":
            flush()
            cur["name"] = val
        elif key == "reading":
            cur["reading"] = val
        elif key in ("base units", "units"):
            cur["units"] = val
        elif key == "sensor type":
            cur["type"] = val
    flush()
    return temps, fans


VM_LINE = re.compile(r"^(\d+)\s+(.+?)\s+\[([^\]]+)\]")


def esxi_vms():
    (out, _err) = esxi_run(["vim-cmd vmsvc/getallvms"])[0]
    vms = []
    for line in out.splitlines()[1:]:
        m = VM_LINE.match(line)
        if m:
            vms.append({"vmid": m.group(1), "name": m.group(2).strip()})
    if vms:
        states = esxi_run([f"vim-cmd vmsvc/power.getstate {v['vmid']}" for v in vms])
        for v, (sout, _) in zip(vms, states):
            v["state"] = "on" if "Powered on" in sout else "off"
    return vms


def esxi_resolve(query):
    vms = esxi_vms()
    exact = [v for v in vms if v["vmid"] == query or v["name"] == query]
    if len(exact) == 1:
        return exact[0]
    partial = [v for v in vms if query in v["name"]]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise ValueError("匹配到多个 VM：" + "、".join(v["name"] for v in partial))
    raise ValueError(f"找不到 VM：{query}")


def esxi_power(query, op):
    v = esxi_resolve(query)
    cmd = {"on": "power.on", "off": "power.shutdown", "kill": "power.off"}[op]
    (out, err) = esxi_run([f"vim-cmd vmsvc/{cmd} {v['vmid']}"])[0]
    return v, (out + err).strip()


def esxi_reboot_nas():
    query = NAS_VM_ID or NAS_VM_NAME
    if not query:
        raise ValueError("未配置 NAS_VM_NAME / NAS_VM_ID")
    v = esxi_resolve(query)
    (out, err) = esxi_run([f"vim-cmd vmsvc/power.reboot {v['vmid']}"])[0]
    return v, (out + err).strip()


# ---------------- 快捷按钮（inline keyboard） ----------------

# ---------------- 自我更新 ----------------

def _app_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _app_file():
    return os.path.join(_app_dir(), "bot.py")


def parse_version(s):
    parts = []
    for p in str(s).strip().split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def extract_version(source_text):
    m = re.search(r"^BOT_VERSION\s*=\s*[\"']([^\"']+)[\"']", source_text, re.M)
    return m.group(1) if m else None


def _smoke_test_candidate(path):
    """子进程里导入候选文件（不执行 main），检查版本与关键函数。"""
    script = (
        "import importlib.util, sys; "
        "spec = importlib.util.spec_from_file_location('candidate', sys.argv[1]); "
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
        "assert getattr(m, 'BOT_VERSION', None), 'no version'; "
        "assert callable(getattr(m, 'main', None)); "
        "assert callable(getattr(m, 'handle', None)); "
        "assert callable(getattr(m, 'handle_callback', None)); "
        "print(m.BOT_VERSION)"
    )
    env = dict(os.environ)
    env.update({"BOT_TOKEN": "smoke-test", "OWNER_CHAT_ID": "123",
                "DATA_DIR": "/tmp/bot3-smoke", "SOCKS_PROXY": ""})
    try:
        r = subprocess.run([sys.executable, "-c", script, path],
                           capture_output=True, text=True, timeout=25, env=env)
    except subprocess.SubprocessError as e:
        return False, f"冒烟测试超时或失败：{e}"
    if r.returncode != 0:
        return False, f"冒烟测试失败：{(r.stderr or r.stdout)[-300:]}"
    return True, r.stdout.strip()


def validate_candidate(source_bytes):
    """返回 (ok, version_or_error)。只验证，不安装；验证时会把候选写进 staging。"""
    if not (10 * 1024 <= len(source_bytes) <= 500 * 1024):
        return False, f"文件大小不对（{len(source_bytes)} 字节），拒绝安装。"
    try:
        text = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return False, "文件不是 UTF-8 文本，拒绝安装。"
    version = extract_version(text)
    if not version:
        return False, "文件里找不到 BOT_VERSION，拒绝安装。"
    for marker in ("def main(", "def handle_callback(", "def handle("):
        if marker not in text:
            return False, f"文件里缺少 {marker}，拒绝安装。"
    try:
        compile(text, "candidate.py", "exec")
    except SyntaxError as e:
        return False, f"语法错误：{e}"
    os.makedirs(DATA_DIR, exist_ok=True)
    staging = os.path.join(DATA_DIR, "update_candidate.py")
    with open(staging, "wb") as f:
        f.write(source_bytes)
    ok, msg = _smoke_test_candidate(staging)
    if not ok:
        return False, msg
    return True, version


def _staging_meta_path():
    return os.path.join(DATA_DIR, "update_pending.json")


def _save_staging_meta(meta):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _staging_meta_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(tmp, _staging_meta_path())


def _load_staging_meta():
    try:
        with open(_staging_meta_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _clear_staging():
    for p in (os.path.join(DATA_DIR, "update_candidate.py"), _staging_meta_path()):
        try:
            os.remove(p)
        except OSError:
            pass


def self_container_ok():
    """确认能通过 Docker API 找到并重启自己；找不到就拒绝自我更新。"""
    try:
        data = docker_json("GET", f"/containers/{SELF_CONTAINER_NAME}/json", timeout=15)
        return bool(data.get("Id"))
    except Exception as e:
        log("self check failed:", e)
        return False


def install_staged_update():
    """验证 staging 里的候选文件并安装。返回 (ok, version_or_error)。成功后调用方负责重启。"""
    meta = _load_staging_meta()
    staging = os.path.join(DATA_DIR, "update_candidate.py")
    if not meta or not os.path.exists(staging):
        return False, "没有待安装的更新，请先发新版 bot.py 文件。"
    if time.time() - float(meta.get("saved_at", 0)) > 1800:
        _clear_staging()
        return False, "待安装的更新已过期（超过 30 分钟），请重新检查更新。"
    with open(staging, "rb") as f:
        source = f.read()
    ok, version_or_err = validate_candidate(source)
    if not ok:
        _clear_staging()
        return False, f"验证失败，拒绝安装：{version_or_err}"
    version = version_or_err
    app_file = _app_file()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(app_file, "rb") as f:
            current = f.read()
        with open(os.path.join(DATA_DIR, "bot.py.bak"), "wb") as f:
            f.write(current)
        # bot.py 是 bind 挂载的文件，不能 rename 覆盖，只能原地写。
        with open(app_file, "wb") as f:
            f.write(source)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        return False, f"写入失败（旧版未动）：{e}"
    _save_staging_meta({"version": version, "source": meta.get("source", "?"),
                        "installed_at": time.time(), "installed": True})
    return True, version


def restart_self():
    try:
        status, _body = docker_api("POST", f"/containers/{SELF_CONTAINER_NAME}/restart?t=5", timeout=15)
        log("restart self status:", status)
    except Exception as e:
        # 容器停掉时连接会断，报错是预期的
        log("restart self issued (connection drop expected):", e)


def restart_self_delayed():
    def _run():
        time.sleep(2)
        restart_self()
    threading.Thread(target=_run, daemon=True).start()


def handle_document_update(msg):
    """主人发来的 .py 文件：下载、验证、问是否安装。"""
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    doc = msg.get("document") or {}
    filename = doc.get("file_name", "")
    if not filename.endswith(".py"):
        send_reply(chat_id, "3 号看不了这个文件。只有新版 bot.py 文件发过来，我才会验证安装；参考图请发给 2 号。",
                   reply_to=message_id, reply_markup=main_keyboard())
        return
    if doc.get("file_size", 0) > 500 * 1024:
        send_reply(chat_id, "文件太大了（超过 500KB），拒绝安装。", reply_to=message_id, reply_markup=main_keyboard())
        return
    if not self_container_ok():
        send_reply(chat_id, f"自我更新不可用：Docker 里找不到名为 {SELF_CONTAINER_NAME} 的容器，无法重启自己。",
                   reply_to=message_id, reply_markup=main_keyboard())
        return
    try:
        info = tg_call("getFile", {"file_id": doc["file_id"]}, timeout=30)
        file_path = info["file_path"]
        dl_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        resp = session.get(dl_url, timeout=60)
        resp.raise_for_status()
        source = resp.content
    except Exception as e:
        send_reply(chat_id, f"文件下载失败：{e}", reply_to=message_id, reply_markup=main_keyboard())
        return
    ok, version_or_err = validate_candidate(source)
    if not ok:
        send_reply(chat_id, f"验证失败，拒绝安装：{version_or_err}", reply_to=message_id, reply_markup=main_keyboard())
        return
    version = version_or_err
    _save_staging_meta({"version": version, "source": f"document:{filename}", "saved_at": time.time()})
    note = "" if parse_version(version) > parse_version(BOT_VERSION) else "\n⚠️ 这个版本不高于当前版本，装了等于重装同一版。"
    send_reply(chat_id,
               f"📄 收到 {filename}（{len(source)} 字节），验证通过：版本 v{version}，当前 v{BOT_VERSION}。"
               "旧版会备份到 data/bot.py.bak，点确定后机器人会重启。" + note,
               reply_to=message_id,
               reply_markup=confirm_keyboard("menu:update_do", "menu:update_cancel", "✅ 确认安装"))


def btn(text, data):
    return {"text": text, "callback_data": data}


def kb(rows):
    return {"inline_keyboard": rows}


# ---------------- 容器镜像更新检测 ----------------
# 每 6 小时自动检查全部容器的镜像：拉取最新镜像，比对镜像 Id。
# 有新镜像就通知主人，点按钮确认后才重建（走 update_container_flow）。
# 3 号自己的容器跳过（走 GitHub 自我更新）；同一个新版本只通知一次。

_IMAGE_CHECK_LOCK = threading.Lock()


def _image_check_state_path():
    return os.path.join(DATA_DIR, "image_check.json")


def _load_image_check_state():
    try:
        with open(_image_check_state_path(), "r", encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict):
            state.setdefault("notified", {})
            state.setdefault("ignored", {})
            return state
    except (OSError, ValueError):
        pass
    return {"notified": {}, "ignored": {}}


# 最近一次镜像检查的完整结果（内存里，供“忽略/恢复”按钮点完后重绘报告用，
# 也供升级完直接返回剩余列表、主菜单“容器更新”秒回上次结果，不用重新全量检测）。
_LAST_IMAGE_CHECK_RESULTS = []
_LAST_IMAGE_CHECK_TIME = ""
# 批量勾选状态：存容器完整 Id；新一轮检查后按最新结果修剪。
_IMAGE_CHECK_SELECTED = set()


def _save_image_check_state(state):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _image_check_state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, _image_check_state_path())
    except OSError as e:
        log("保存镜像检查状态失败：", e)


_DB_IMAGE_BASENAMES = {
    "postgres", "postgresql", "redis", "mysql", "mariadb", "mongo", "mongodb",
    "memcached", "rabbitmq", "elasticsearch", "opensearch", "clickhouse",
    "influxdb", "cassandra", "neo4j", "valkey", "dragonfly", "etcd",
}
_DB_NAME_TOKENS = {
    "postgres", "postgresql", "pgsql", "redis", "mysql", "mariadb", "mongo",
    "mongodb", "memcached", "rabbitmq", "valkey", "etcd",
}


def _is_db_like(image_ref, service, name):
    """是不是数据库/缓存类镜像：先看镜像仓库名，再看服务名/容器名分词。"""
    ref = (image_ref or "").strip().lower()
    base = ref.split("@")[0].rsplit("/", 1)[-1].split(":")[0]
    if base in _DB_IMAGE_BASENAMES:
        return True
    for raw in (service or "", name or ""):
        for tok in re.split(r"[-_]", raw.lower()):
            if tok.rstrip("0123456789") in _DB_NAME_TOKENS:
                return True
    return False


def _compose_labels(c, inspect_data=None):
    labels = {}
    if inspect_data:
        labels.update(((inspect_data.get("Config") or {}).get("Labels") or {}))
    labels.update(c.get("Labels") or {})
    return labels


def _companion_db_projects(entries):
    """entries: [{cid, name, image, project, service}]。
    返回 {cid: project}：多容器 Compose 项目里的数据库类服务算附属，
    单独升级可能让主程序因版本不兼容起不来，版本应由 Compose 决定。"""
    by_project = {}
    for e in entries:
        if e.get("project"):
            by_project.setdefault(e["project"], []).append(e)
    companions = {}
    for project, group in by_project.items():
        if len(group) < 2:
            continue
        for e in group:
            if _is_db_like(e.get("image", ""), e.get("service", ""), e.get("name", "")):
                companions[e["cid"]] = project
    return companions


def _companion_project_for(c, inspect_data, image_ref):
    """单容器更新时判断它是不是附属数据库；是则返回项目名。"""
    labels = _compose_labels(c, inspect_data)
    project = labels.get("com.docker.compose.project", "")
    if not project:
        return None
    if not _is_db_like(image_ref, labels.get("com.docker.compose.service", ""),
                       container_display_name(c)):
        return None
    try:
        items = list_containers()
    except Exception:
        return None
    group = [i for i in items
             if (i.get("Labels") or {}).get("com.docker.compose.project") == project]
    return project if len(group) >= 2 else None


def check_container_image_updates():
    """检查全部容器：返回每容器的检查结果（update/current/error/skipped/companion）。"""
    try:
        items = list_containers()
    except Exception as e:
        return [{"status": "fatal", "error": str(e)}]
    # 第一遍先收集详情，按 Compose 项目识别附属数据库（不单独升级）。
    entries = []
    results = []
    for c in sorted(items, key=container_display_name):
        name = container_display_name(c)
        cid = c["Id"]
        if name == SELF_CONTAINER_NAME:
            continue
        try:
            inspect_data = inspect_container(cid)
        except Exception as e:
            results.append({"cid": cid, "name": name,
                            "image": (c.get("Image") or "").strip(),
                            "status": "error", "error": f"读详情失败：{e}"})
            continue
        labels = _compose_labels(c, inspect_data)
        entries.append({"c": c, "cid": cid, "name": name,
                        "inspect": inspect_data,
                        "image": _container_image_ref(c, inspect_data),
                        "project": labels.get("com.docker.compose.project", ""),
                        "service": labels.get("com.docker.compose.service", "")})
    companions = _companion_db_projects(entries)
    for e in entries:
        c, cid, name = e["c"], e["cid"], e["name"]
        inspect_data = e["inspect"]
        image_ref = e["image"]
        base = {"cid": cid, "name": name, "image": image_ref}
        if cid in companions:
            results.append({**base, "status": "companion",
                            "error": f"附属数据库（{companions[cid]} 项目），随主程序/Compose 管理"})
            continue
        if not image_ref:
            results.append({**base, "status": "error", "error": "读不到镜像名"})
            continue
        if _is_image_id_ref(image_ref):
            results.append({**base, "status": "skipped",
                            "error": "镜像只剩 ID（标签已丢失），无法检查更新"})
            continue
        old_id = inspect_data.get("Image", "")
        if _image_has_repo_digest(old_id) is False:
            results.append({**base, "status": "skipped",
                            "error": "本地构建镜像，没有仓库可检查"})
            continue
        try:
            ok, pull_msg = docker_pull(image_ref, timeout=300)
        except ValueError as e:
            # digest 固定的镜像拉了也没用，直接跳过。
            results.append({**base, "status": "skipped", "error": str(e)})
            continue
        except Exception as e:
            results.append({**base, "status": "error", "error": f"拉取失败：{e}"})
            continue
        if not ok:
            results.append({**base, "status": "error", "error": pull_msg})
            continue
        try:
            img = docker_json("GET", "/images/" + quote(image_ref, safe="") + "/json")
            new_id = img.get("Id", "")
        except Exception as e:
            results.append({**base, "status": "error", "error": f"读镜像信息失败：{e}"})
            continue
        if old_id and new_id and new_id != old_id:
            results.append({**base, "status": "update", "old_id": old_id, "new_id": new_id})
        else:
            results.append({**base, "status": "current", "old_id": old_id, "new_id": new_id})
    results.sort(key=lambda r: r.get("name", ""))
    return results


def _short_id(image_id):
    """sha256:xxx… 截成短 id 方便看。"""
    if not image_id:
        return "?"
    if image_id.startswith("sha256:"):
        return image_id[7:19]
    return image_id[:12]


def _is_ignored(ignored, result):
    """这条更新是否已被忽略（只忽略这一个镜像版本；出了更新的版本会再提醒）。"""
    entry = (ignored or {}).get(result.get("cid", ""))
    return bool(entry) and entry.get("new_id") == result.get("new_id")


def _cached_image_report():
    """上次检查结果的报告（文本+键盘）；还没查过返回 None。"""
    if not _LAST_IMAGE_CHECK_RESULTS:
        return None
    return format_image_check_report(_LAST_IMAGE_CHECK_RESULTS)


def _selected_updates():
    """当前勾选且仍有更新的缓存条目（按列表顺序）。"""
    state = _load_image_check_state()
    ignored = state.get("ignored", {})
    return [r for r in _LAST_IMAGE_CHECK_RESULTS
            if r.get("status") == "update" and not _is_ignored(ignored, r)
            and r.get("cid") in _IMAGE_CHECK_SELECTED]


def _mark_image_check_updated(query):
    """某个容器升级成功后，把缓存结果里那条标成已最新，
    这样返回列表时不用重新检测、也不会再催升同一个。"""
    for r in _LAST_IMAGE_CHECK_RESULTS:
        if r.get("cid", "").startswith(query) or r.get("name") == query:
            old_cid = r.get("cid", "")
            _IMAGE_CHECK_SELECTED.discard(old_cid)
            r["status"] = "current"
            if r.get("new_id"):
                r["old_id"] = r["new_id"]
            try:
                fresh = resolve_container(r["name"])
                r["cid"] = fresh["Id"]
            except Exception:
                pass
            try:
                state = _load_image_check_state()
                state.get("notified", {}).pop(old_cid, None)
                state.get("ignored", {}).pop(old_cid, None)
                _save_image_check_state(state)
            except Exception:
                pass
            return


def format_image_check_report(results, checked_at=None):
    """检查结果文本 + 键盘。

    每个有更新的容器一行说明 + [⬆️ 升级] [🚫 忽略] 两个按钮；
    已忽略的单独列在下面，可点 [↩️ 恢复]。
    """
    state = _load_image_check_state()
    ignored = state.get("ignored", {})
    updates = [r for r in results if r["status"] == "update" and not _is_ignored(ignored, r)]
    ignored_rows = [r for r in results if r["status"] == "update" and _is_ignored(ignored, r)]
    current = sum(1 for r in results if r["status"] == "current")
    problems = [r for r in results if r["status"] in ("error", "skipped")]
    companions = [r for r in results if r["status"] == "companion"]
    lines = [f"🔍 容器更新（{checked_at or _LAST_IMAGE_CHECK_TIME or cst_now()}）"]
    if not updates and ignored_rows:
        lines.append("\n没有新的更新（有更新的都已忽略）。")
    elif not updates and problems:
        lines.append("\n没有检测到新镜像（有容器没检查成，见下）。")
    elif not updates:
        lines.append("\n全部容器都已是最新。")
    lines.append(f"\n共 {len(results)} 个容器：{len(updates)} 个有更新，{current} 个已是最新，"
                 f"{len(problems)} 个检查失败/跳过{'' if not ignored_rows else f'，{len(ignored_rows)} 个已忽略'}"
                 f"{'' if not companions else f'，{len(companions)} 个附属数据库不单独升级'}。")
    for r in problems[:5]:
        lines.append(f"⚠️ {r['name']}：{r.get('error', '')[:40]}")
    if len(problems) > 5:
        lines.append(f"…还有 {len(problems) - 5} 个未列出。")
    if companions:
        lines.append("🗄️ 附属数据库（随主程序/Compose 管理）：" +
                     "、".join(r["name"] for r in companions))
    selected = [r for r in updates if r.get("cid") in _IMAGE_CHECK_SELECTED]
    if updates:
        lines.append(f"\n点容器名勾选（可多选），已选 {len(selected)} 个；选好点 [⬆️ 升级所选]。")
    rows = []
    # 容器列表只放在按钮里（每行两个勾选按钮），正文不再重复列一遍。
    for i in range(0, len(updates), 2):
        row = []
        for r in updates[i:i + 2]:
            mark = "☑️" if r.get("cid") in _IMAGE_CHECK_SELECTED else "⬜"
            row.append(btn(f"{mark} {r['name']}", f"img:sel:{r['cid'][:12]}"))
        rows.append(row)
    if updates:
        rows.append([btn("☑️ 全选", "img:selall"), btn("⬜ 清空", "img:selnone")])
        rows.append([btn(f"⬆️ 升级所选 ({len(selected)})", "img:ask:batch"),
                     btn("🚫 忽略所选", "img:ignore:batch")])
    for i in range(0, len(ignored_rows), 2):
        row = []
        for r in ignored_rows[i:i + 2]:
            row.append(btn(f"↩️ {r['name']}", f"img:unignore:{r['cid'][:12]}"))
        rows.append(row)
    rows.append([btn("🔄 重新检查", "menu:container_updates_recheck"),
                 btn("⬅️ 主菜单", "menu:main")])
    return truncate("\n".join(lines)), kb(rows)


def run_image_check(manual, chat_id):
    """跑一次全量检查。手动触发总是回报告；自动触发只在有“新”更新时通知。"""
    global _LAST_IMAGE_CHECK_RESULTS, _LAST_IMAGE_CHECK_TIME
    results = check_container_image_updates()
    if results and results[0].get("status") == "fatal":
        if manual:
            send_reply(chat_id, f"🔍 镜像更新检查失败：{results[0].get('error')}", reply_markup=back_keyboard())
        else:
            log("镜像更新自动检查失败：", results[0].get("error"))
        return
    _LAST_IMAGE_CHECK_RESULTS = results
    _LAST_IMAGE_CHECK_TIME = cst_now()
    _IMAGE_CHECK_SELECTED.intersection_update(
        {r["cid"] for r in results if r.get("status") == "update"})
    state = _load_image_check_state()
    notified = state.get("notified", {})
    ignored = state.get("ignored", {})
    present = {r["cid"] for r in results}
    has_new = False
    for r in results:
        if r["status"] == "update":
            if _is_ignored(ignored, r):
                notified.pop(r["cid"], None)
                continue
            if notified.get(r["cid"]) != r["new_id"]:
                notified[r["cid"]] = r["new_id"]
                has_new = True
        elif r["status"] == "current":
            notified.pop(r["cid"], None)
            ignored.pop(r["cid"], None)
    for cid in list(notified):
        if cid not in present:
            del notified[cid]
    for cid in list(ignored):
        if cid not in present:
            del ignored[cid]
    state["notified"] = notified
    state["ignored"] = ignored
    state["last_check"] = time.time()
    _save_image_check_state(state)
    if manual or has_new:
        text, markup = format_image_check_report(results)
        send_reply(chat_id, text, reply_markup=markup)


def start_image_check(manual, chat_id):
    """在后台线程跑检查。返回 False 表示已有检查在进行中。"""
    if not _IMAGE_CHECK_LOCK.acquire(blocking=False):
        return False

    def _run():
        try:
            run_image_check(manual, chat_id)
        except Exception:
            log("镜像检查出错：", traceback.format_exc(limit=3))
        finally:
            _IMAGE_CHECK_LOCK.release()

    threading.Thread(target=_run, daemon=True).start()
    return True


def main_keyboard():
    return kb([
        [btn("📊 系统状态", "menu:status"), btn("🌐 公网 IP", "menu:ip")],
        [btn("⬆️ 进程 TOP", "menu:top"), btn("🛠️ 容器管理", "menu:manage:0")],
        [btn("🖥️ ESXi VM", "menu:esxi"), btn("🔄 重启 NAS", "menu:reboot")],
        [btn("🔍 容器更新", "menu:container_updates"), btn("🔔 告警", "menu:alerts")],
    ])


def back_keyboard():
    return kb([[btn("⬅️ 主菜单", "menu:main")]])


def refresh_back_keyboard(action_data):
    return kb([[btn("🔄 刷新", action_data), btn("⬅️ 主菜单", "menu:main")]])


WELCOME = f"""👋 Muse × NAS 控制台（v{BOT_VERSION}）

点下面的按钮就能直接查 NAS、管容器（本机秒回）。
NAS、容器、ESXi 的事都在这里执行。

命令也一样能用：/status /docker /esxi /container_updates /alerts /help"""


HELP = """3 号机器人：NAS + Docker + ESXi 管理

发 /start 打开快捷按钮菜单，点按钮就能查 NAS、管容器。
直接打字说“系统状态”“进程”“内存 TOP5”也能查；说“重启 <容器/服务名>”会先让你点 ✅ 确认再执行。

/status NAS 状态（CPU/内存/磁盘/网络/ESXi 温度风扇/容器数）
/docker 打开容器管理（或 logs <容器> [行数] [关键词] | start|stop|restart <容器> | prune | update <容器>）
/esxi 列出 VM 和电源状态（或 on|off|kill <VM 名或 ID>）
/container_updates 检查全部容器镜像更新（可忽略、可升级）
/topmem 内存占用 TOP5（只读）
/services TrueNAS 系统服务列表（或说“系统服务”）
/alerts 告警状态（on/off 开关）
/menu 打开快捷按钮菜单
/help 帮助

只认主人一个人，没有任意命令执行。危险操作要点两次确认。删除容器只删容器本身，不删数据卷。"""


# ---------- 按钮文字等宽对齐 ----------
# Telegram 会把同一行按钮里的文字居中显示：名字长短不一时，开头的 🟢/🔴
# 横向位置各不相同，看起来就没对齐。把名字按显示宽度补齐到固定宽度，
# 同一行每个按钮的文字总宽度一致，圆点就对齐了（中文算 2 格，超长截断）。
MANAGE_NAME_WIDTH = 12


def display_width(text):
    w = 0
    for ch in text:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def pad_name(name, width=MANAGE_NAME_WIDTH):
    if display_width(name) > width:
        out = ""
        w = 0
        for ch in name:
            cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if w + cw > width - 1:
                break
            out += ch
            w += cw
        return out + "…"
    return name + " " * (width - display_width(name))


def status_button_label(emoji, name):
    return f"{emoji} {pad_name(name)}"


def manage_keyboard(page):
    items = sorted(list_containers(), key=container_display_name)
    total_pages = max(1, (len(items) + MANAGE_PAGE_SIZE - 1) // MANAGE_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    rows = []
    page_items = items[page * MANAGE_PAGE_SIZE:(page + 1) * MANAGE_PAGE_SIZE]
    for i in range(0, len(page_items), 3):
        row = []
        for c in page_items[i:i + 3]:
            emoji = "🟢" if c.get("State") == "running" else "🔴"
            row.append(btn(status_button_label(emoji, container_display_name(c)), f"c:detail:{c['Id'][:12]}"))
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(btn("⬅️ 上一页", f"menu:manage:{page - 1}"))
    if page < total_pages - 1:
        nav.append(btn("下一页 ➡️", f"menu:manage:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("🧹 清理无用镜像", "prune:ask"), btn("⬅️ 主菜单", "menu:main")])
    return kb(rows), page, total_pages, len(items)


def container_detail_keyboard(shortid):
    return kb([
        [btn("▶️ 启动", f"c:ask:start:{shortid}"),
         btn("⏹️ 停止", f"c:ask:stop:{shortid}"),
         btn("🔄 重启", f"c:ask:restart:{shortid}")],
        [btn("📋 看日志", f"c:logs:{shortid}"), btn("🔍 搜日志", f"c:search:{shortid}")],
        [btn("⬆️ 更新容器", f"c:ask:update:{shortid}")],
        [btn("⬅️ 容器列表", "menu:manage:0"), btn("⬅️ 主菜单", "menu:main")],
    ])


def confirm_keyboard(confirm_data, cancel_data, confirm_label="✅ 确定"):
    return kb([[btn(confirm_label, confirm_data), btn("❌ 取消", cancel_data)]])


def esxi_list_keyboard(vms):
    rows = []
    for v in vms:
        emoji = "🟢" if v.get("state") == "on" else "🔴"
        rows.append([btn(status_button_label(emoji, v['name']), f"e:detail:{v['vmid']}")])
    rows.append([btn("⬅️ 主菜单", "menu:main")])
    return kb(rows)


def vm_detail_keyboard(vmid):
    return kb([
        [btn("▶️ 开机", f"e:ask:on:{vmid}"),
         btn("⏻ 优雅关机", f"e:ask:off:{vmid}"),
         btn("⚡ 强制断电", f"e:ask:kill:{vmid}")],
        [btn("⬅️ VM 列表", "menu:esxi"), btn("⬅️ 主菜单", "menu:main")],
    ])


def container_detail_text(c, inspect=None):
    name = container_display_name(c)
    ports = c.get("Ports") or []
    port_s = ", ".join(f"{p.get('PublicPort', '')}->{p.get('PrivatePort', '')}" for p in ports if p.get("PrivatePort")) or "无"
    text = (f"🐳 {name}\n"
            f"镜像：{_container_image_ref(c, inspect) or '?'}\n"
            f"状态：{c.get('State', '?')} {c.get('Status', '')}\n"
            f"ID：{c.get('Id', '')[:12]}\n"
            f"端口：{port_s}")
    if inspect:
        created = (inspect.get("Created", "") or "")[:19].replace("T", " ")
        restart = ((inspect.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name", "?")
        mounts = inspect.get("Mounts") or []
        mount_lines = []
        for m in mounts:
            mount_lines.append(f"{m.get('Source', '?')} → {m.get('Destination', '?')}{'（只读）' if not m.get('RW') else ''}")
        networks = list(((inspect.get("NetworkSettings") or {}).get("Networks") or {}).keys())
        text += (f"\n创建时间：{created or '？'}"
                 f"\n重启策略：{restart}"
                 f"\n网络：{', '.join(networks) or '无'}"
                 f"\n挂载：\n" + ("\n".join(mount_lines) if mount_lines else "无"))
    return text


def detail_text_for(c):
    try:
        return container_detail_text(c, inspect_container(c["Id"]))
    except Exception:
        return container_detail_text(c)


# 日志搜索待输入状态：chat_id -> (shortid, 过期时间戳)
_PENDING_SEARCH = {}


def _set_pending_search(chat_id, shortid):
    _PENDING_SEARCH[chat_id] = (shortid, time.monotonic() + 300)


def _take_pending_search(chat_id):
    item = _PENDING_SEARCH.pop(chat_id, None)
    if not item:
        return None
    shortid, expiry = item
    if time.monotonic() > expiry:
        return None
    return shortid


def _clear_pending_search(chat_id):
    _PENDING_SEARCH.pop(chat_id, None)


# ---------------- 命令处理 ----------------

def handle_plain_text(raw, chat_id, message_id):
    """直接打字：只读查询直接在本机执行；操作类请求引导到按钮确认。

    不把服务器维护推给 2 号：NAS、容器、ESXi 的事 3 号自己执行。
    只有生图、生视频、查资料这类非服务器的事才去 2 号。
    """
    pending = _take_pending_search(chat_id)
    if pending:
        try:
            c = resolve_container(pending)
            hits, total = search_container_logs(pending, raw.strip(), tail=200)
            if hits:
                reply = f"🔍 {container_display_name(c)} 日志搜索“{raw.strip()}”：命中 {total} 行，显示前 {len(hits)} 行\n" + "\n".join(hits)
            else:
                reply = f"🔍 {container_display_name(c)} 最近 200 行日志里没有“{raw.strip()}”。"
            markup = container_detail_keyboard(c["Id"][:12])
        except Exception as e:
            reply, markup = f"搜索出错了：{e}", back_keyboard()
        send_reply(chat_id, reply, reply_to=message_id, reply_markup=markup)
        return
    text = raw.strip().lower()
    parse_mode = None
    try:
        if any(k in text.replace(" ", "") for k in ("内存top5", "top5内存", "内存占用", "内存前5", "内存最高")):
            reply, markup = top_mem_text(), refresh_back_keyboard("menu:top")
            parse_mode = "HTML"
        elif any(k in text for k in ("系统服务", "服务列表")):
            reply, markup = truenas_services_text(), back_keyboard()
        elif any(k in text for k in ("重启", "停止", "启动", "关机", "开机", "断电")):
            action, target = parse_op_request(raw)
            if action and target:
                # 先看是不是容器，再看是不是系统服务；都不是就报出来，不猜。
                detail = ""
                kind = None
                try:
                    c = resolve_container(target)
                    kind, detail = "container", f"容器：{container_display_name(c)}（{c.get('State', '?')}）"
                except Exception:
                    kind = None
                if kind is None and truenas_configured():
                    try:
                        names = {n for n, _s in truenas_services()}
                        if target in names:
                            kind, detail = "service", f"系统服务：{target}（TrueNAS，{'SSH' if truenas_ssh_configured() else 'API'}）"
                    except Exception as e:
                        log("查服务列表失败：", e)
                if kind is None:
                    if not truenas_configured():
                        reply = (f"找不到容器“{target}”，系统服务也没配置。\n"
                                 "系统服务要在 .env 里填 TRUENAS_SSH_KEY_FILE/TRUENAS_SSH_PASSWORD 或 TRUENAS_API_KEY 后重启容器。")
                    else:
                        reply = f"找不到容器或系统服务“{target}”。发“系统服务”看实时列表，或发“容器”看容器列表。"
                    markup = main_keyboard()
                else:
                    request_op_approval(chat_id, message_id, kind, target, action,
                                        detail + "\n执行前请确认目标没认错。")
                    return
            else:
                reply = ("要执行操作，请点下面的按钮选容器/VM 再点确认，或直接发命令（/docker、/esxi），"
                         "我直接在 NAS 上执行，不用去 2 号。只看状态的话直接说“系统状态”就行。")
                markup = main_keyboard()
        elif any(k in text for k in ("容器更新", "镜像更新", "更新容器", "有更新", "检查更新")):
            if start_image_check(manual=True, chat_id=chat_id):
                reply = "🔍 正在检查全部容器的镜像更新（约 1-2 分钟），查完会发消息。"
            else:
                reply = "正在检查镜像更新，稍等。"
            markup = back_keyboard()
        elif any(k in text for k in ("系统状态", "状态", "负载", "内存", "cpu",
                                     "磁盘", "空间", "硬盘", "温度", "流量", "网络")):
            # 磁盘、温度、网络流量都已并入系统状态，直接回系统状态。
            reply, markup = fmt_status(), refresh_back_keyboard("menu:status")
        elif "公网" in text or "出口" in text or text.strip() == "ip":
            reply, markup = public_ip_text(), refresh_back_keyboard("menu:ip")
        elif "容器" in text:
            keyboard, page, total_pages, total = manage_keyboard(0)
            reply = (f"🛠️ 容器管理（共 {total} 个，第 {page + 1}/{total_pages} 页）\n"
                     "点容器查看详情、启停、看日志。")
            markup = keyboard
        elif "进程" in text or "top" in text:
            reply, markup = top_text(), refresh_back_keyboard("menu:top")
            parse_mode = "HTML"
        elif "esxi" in text or "虚拟机" in text or " vm" in text or text.strip() == "vm":
            if not esxi_configured():
                reply, markup = "ESXi 未配置：在 .env 里填 ESXI_HOST/ESXI_USER/ESXI_PASSWORD 后重启容器。", back_keyboard()
            else:
                vms = esxi_vms()
                reply = "🖥️ ESXi VM\n" + ("\n".join(f"{v['vmid']} {v['name']} {v['state']}" for v in vms) or "没有 VM。")
                markup = esxi_list_keyboard(vms)
        else:
            reply = ("3 号只管 NAS、容器、ESXi，点按钮或发命令，我直接在本机执行。"
                     "生图、生视频、查资料这些才去 2 号。")
            markup = main_keyboard()
    except Exception as e:
        reply, markup = f"出错了：{e}", back_keyboard()
        log("plain-text handler error:", traceback.format_exc(limit=3))
    send_reply(chat_id, reply, reply_to=message_id, reply_markup=markup, parse_mode=parse_mode)


def handle(text, chat_id, message_id):
    raw = text.strip()
    if not raw:
        return
    if not raw.startswith("/"):
        handle_plain_text(raw, chat_id, message_id)
        return
    _clear_pending_search(chat_id)
    parts = raw.split()
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    args = parts[1:]
    try:
        if cmd == "start" or cmd == "menu":
            send_reply(chat_id, WELCOME, reply_to=message_id, reply_markup=main_keyboard())
            return
        if cmd == "help":
            reply = HELP
            send_reply(chat_id, reply, reply_to=message_id, reply_markup=main_keyboard())
            return
        if cmd == "status":
            reply = fmt_status()
        elif cmd == "topmem":
            reply = top_mem_text()
            send_reply(chat_id, reply, reply_to=message_id, parse_mode="HTML")
            return
        elif cmd == "services":
            reply = truenas_services_text()
            send_reply(chat_id, reply, reply_to=message_id)
            return
        elif cmd == "docker" and not args:
            keyboard, page, total_pages, total = manage_keyboard(0)
            send_reply(chat_id,
                       f"🛠️ 容器管理（共 {total} 个，第 {page + 1}/{total_pages} 页）\n点容器查看详情、启停、看日志。",
                       reply_to=message_id, reply_markup=keyboard)
            return
        elif cmd == "docker" and args and args[0] == "logs" and len(args) >= 2:
            tail, keyword = 50, None
            if len(args) >= 3 and args[2].isdigit():
                tail = int(args[2])
                keyword = " ".join(args[3:]) or None
            elif len(args) >= 3:
                keyword = " ".join(args[2:])
            if keyword:
                hits, total = search_container_logs(args[1], keyword, min(tail, 200))
                reply = (f"🔍 日志搜索“{keyword}”：命中 {total} 行\n" + "\n".join(hits)) if hits else f"🔍 最近 {min(tail, 200)} 行日志里没有“{keyword}”。"
            else:
                reply = container_logs(args[1], min(tail, 200))
        elif cmd == "docker" and args and args[0] in ("start", "stop", "restart") and len(args) >= 2:
            c = container_action(args[1], args[0])
            reply = f"{args[0]} 已执行：{container_display_name(c)}"
        elif cmd == "docker" and args and args[0] == "prune":
            send_reply(chat_id, "确定要清理悬空镜像吗？只删没有被任何容器引用的悬空镜像，不删正在用的镜像。",
                       reply_to=message_id, reply_markup=confirm_keyboard("prune:do", "menu:main"))
            return
        elif cmd == "docker" and args and args[0] == "update" and len(args) >= 2:
            c = resolve_container(args[1])
            send_reply(chat_id, f"确定要更新容器“{container_display_name(c)}”吗？会拉取新镜像并重建容器，旧容器保留到新容器跑起来后才删。常驻服务容器才能这样更新。",
                       reply_to=message_id, reply_markup=confirm_keyboard(f"c:do:update:{c['Id'][:12]}", f"c:detail:{c['Id'][:12]}"))
            return
        elif cmd == "docker":
            reply = "用法：/docker [logs <容器> [行数] [关键词] | start|stop|restart <容器> | prune | update <容器>]（不带参数打开容器管理）"
        elif cmd == "esxi" and not args:
            if not esxi_configured():
                reply = "ESXi 未配置：在 .env 里填 ESXI_HOST/ESXI_USER/ESXI_PASSWORD 后重启容器。"
            else:
                vms = esxi_vms()
                reply = "\n".join(f"{v['vmid']} {v['name']} {v['state']}" for v in vms) or "没有 VM。"
        elif cmd == "esxi" and args and args[0] in ("on", "off", "kill") and len(args) >= 2:
            if not esxi_configured():
                reply = "ESXi 未配置。"
            else:
                v, out = esxi_power(args[1], args[0])
                reply = f"{v['name']}（{v['vmid']}）{args[0]}：{out or '命令已发送'}"
        elif cmd == "esxi":
            reply = "用法：/esxi [on|off|kill <VM 名或 ID>]"
        elif cmd == "container_updates":
            if start_image_check(manual=True, chat_id=chat_id):
                send_reply(chat_id, "🔍 正在检查全部容器的镜像更新（约 1-2 分钟），查完会发消息。",
                           reply_to=message_id, reply_markup=back_keyboard())
            else:
                send_reply(chat_id, "正在检查镜像更新，稍等。",
                           reply_to=message_id, reply_markup=back_keyboard())
            return
        elif cmd == "alerts":
            if args and args[0] == "on":
                set_alerts_enabled(True)
            elif args and args[0] == "off":
                set_alerts_enabled(False)
            send_reply(chat_id, alerts_status_text(), reply_to=message_id, reply_markup=alerts_keyboard())
            return
        else:
            send_reply(chat_id, "不认识的命令，发 /help 看看能做什么，或直接点下面的按钮。",
                       reply_to=message_id, reply_markup=main_keyboard())
            return
    except Exception as e:
        reply = f"出错了：{e}"
        log("handler error:", traceback.format_exc(limit=3))
    send_reply(chat_id, reply, reply_to=message_id)


# ---------------- 按钮回调处理 ----------------

ACTION_LABEL = {"start": "启动", "stop": "停止", "restart": "重启",
                "on": "开机", "off": "优雅关机", "kill": "强制断电"}


def _edit_or_send(chat_id, message_id, text, reply_markup=None, parse_mode=None):
    try:
        edit_message(chat_id, message_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        log("edit 失败，改发新消息：", e)
        send_reply(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)


def handle_callback(query):
    data = query.get("data", "") or ""
    from_id = (query.get("from") or {}).get("id")
    msg = query.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    query_id = query.get("id")
    if from_id != OWNER_CHAT_ID:
        if query_id:
            answer_callback(query_id, "不是主人，忽略。", show_alert=True)
        return
    if not chat_id or not message_id or not query_id:
        return
    answer_callback(query_id)
    try:
        _clear_pending_search(chat_id)
        if data == "menu:main":
            _edit_or_send(chat_id, message_id, WELCOME, main_keyboard())
        elif data.startswith("op:do:"):
            token = data[len("op:do:"):]
            try:
                result = execute_pending_op(token)
                _edit_or_send(chat_id, message_id, result, back_keyboard())
            except Exception as e:
                _edit_or_send(chat_id, message_id, f"执行失败：{e}", back_keyboard())
        elif data.startswith("op:cancel:"):
            token = data[len("op:cancel:"):]
            _PENDING_OPS.pop(token, None)
            _edit_or_send(chat_id, message_id, "已取消，没有执行任何操作。", back_keyboard())
        elif data == "menu:status":
            _edit_or_send(chat_id, message_id, fmt_status(), refresh_back_keyboard("menu:status"))
        elif data == "menu:ip":
            _edit_or_send(chat_id, message_id, public_ip_text(), refresh_back_keyboard("menu:ip"))
        elif data == "menu:top":
            _edit_or_send(chat_id, message_id, top_text(), refresh_back_keyboard("menu:top"),
                          parse_mode="HTML")
        elif data.startswith("menu:manage:"):
            try:
                page = int(data.rsplit(":", 1)[1])
            except ValueError:
                page = 0
            keyboard, page, total_pages, total = manage_keyboard(page)
            _edit_or_send(chat_id, message_id,
                          f"🛠️ 容器管理（共 {total} 个，第 {page + 1}/{total_pages} 页）\n点容器查看详情、启停、看日志。",
                          keyboard)
        elif data == "menu:esxi":
            if not esxi_configured():
                _edit_or_send(chat_id, message_id,
                              "ESXi 未配置：在 .env 里填 ESXI_HOST/ESXI_USER/ESXI_PASSWORD 后重启容器。",
                              back_keyboard())
            else:
                vms = esxi_vms()
                text = "🖥️ ESXi VM\n" + ("\n".join(f"{v['vmid']} {v['name']} {v['state']}" for v in vms) or "没有 VM。")
                _edit_or_send(chat_id, message_id, text, esxi_list_keyboard(vms))
        elif data == "menu:reboot":
            if not esxi_configured():
                _edit_or_send(chat_id, message_id,
                              "重启 NAS 需要走 ESXi，但 ESXi 未配置。先在 .env 里填好 ESXi，再填 NAS_VM_NAME（NAS 这个 VM 的名字）后重启容器。",
                              back_keyboard())
            elif not (NAS_VM_NAME or NAS_VM_ID):
                _edit_or_send(chat_id, message_id,
                              "重启 NAS 还差一步：在 .env 里填 NAS_VM_NAME（NAS 这个 VM 在 ESXi 里的名字，或用 NAS_VM_ID 填 ID），然后 docker compose up -d --build 重启容器。填好后这个按钮才能用。",
                              back_keyboard())
            else:
                _edit_or_send(chat_id, message_id,
                              "⚠️ 确定要重启 NAS 吗？\n重启后所有容器和这个机器人都会断开，等 NAS 起来后机器人自动恢复。\n要点两次确认才会执行。",
                              confirm_keyboard("menu:reboot_do", "menu:main", "⚠️ 确定重启"))
        elif data == "menu:reboot_do":
            if not esxi_configured() or not (NAS_VM_NAME or NAS_VM_ID):
                _edit_or_send(chat_id, message_id, "重启 NAS 未配置，无法执行。", back_keyboard())
            else:
                v, out = esxi_reboot_nas()
                _edit_or_send(chat_id, message_id,
                              f"🔄 重启命令已发送：{v['name']}（{v['vmid']}）\n{out or '（ESXi 无回显）'}\nNAS 重启中，机器人稍后自动恢复。",
                              back_keyboard())
        elif data == "menu:update_do":
            ok, result = install_staged_update()
            if not ok:
                _edit_or_send(chat_id, message_id, f"更新失败：{result}", back_keyboard())
            else:
                _edit_or_send(chat_id, message_id,
                              f"✅ v{result} 已安装，正在重启…",
                              back_keyboard())
                restart_self_delayed()
        elif data == "menu:update_cancel":
            _clear_staging()
            _edit_or_send(chat_id, message_id, "已取消更新。", back_keyboard())
        elif data in ("menu:container_updates", "img:list"):
            cached = _cached_image_report()
            if cached:
                text, markup = cached
                _edit_or_send(chat_id, message_id, text, markup)
            elif start_image_check(manual=True, chat_id=chat_id):
                _edit_or_send(chat_id, message_id, "🔍 正在检查全部容器的镜像更新（约 1-2 分钟），查完会发消息。", back_keyboard())
            else:
                _edit_or_send(chat_id, message_id, "正在检查镜像更新，稍等。", back_keyboard())
        elif data == "menu:container_updates_recheck":
            if start_image_check(manual=True, chat_id=chat_id):
                _edit_or_send(chat_id, message_id, "🔍 正在重新检查全部容器的镜像更新（约 1-2 分钟），查完会发消息。", back_keyboard())
            else:
                _edit_or_send(chat_id, message_id, "正在检查镜像更新，稍等。", back_keyboard())
        elif data.startswith("img:sel:"):
            shortid = data.rsplit(":", 1)[1]
            for r in _LAST_IMAGE_CHECK_RESULTS:
                if r.get("cid", "").startswith(shortid) and r.get("status") == "update":
                    if r["cid"] in _IMAGE_CHECK_SELECTED:
                        _IMAGE_CHECK_SELECTED.discard(r["cid"])
                    else:
                        _IMAGE_CHECK_SELECTED.add(r["cid"])
                    break
            cached = _cached_image_report()
            if cached:
                text, markup = cached
                _edit_or_send(chat_id, message_id, text, markup)
        elif data == "img:selall":
            state = _load_image_check_state()
            ignored = state.get("ignored", {})
            for r in _LAST_IMAGE_CHECK_RESULTS:
                if r.get("status") == "update" and not _is_ignored(ignored, r):
                    _IMAGE_CHECK_SELECTED.add(r["cid"])
            cached = _cached_image_report()
            if cached:
                text, markup = cached
                _edit_or_send(chat_id, message_id, text, markup)
        elif data == "img:selnone":
            _IMAGE_CHECK_SELECTED.clear()
            cached = _cached_image_report()
            if cached:
                text, markup = cached
                _edit_or_send(chat_id, message_id, text, markup)
        elif data == "img:ask:batch":
            selected = _selected_updates()
            if not selected:
                cached = _cached_image_report()
                if cached:
                    text, markup = cached
                    _edit_or_send(chat_id, message_id,
                                  text + "\n\n⚠️ 还没勾选，先点容器名勾选再升级。", markup)
            else:
                names = "、".join(r["name"] for r in selected)
                _edit_or_send(chat_id, message_id,
                              f"确定要批量升级这 {len(selected)} 个容器吗？\n{names}\n会逐个拉取新镜像并重建，失败的会自动回滚并在汇总里说明。",
                              confirm_keyboard("img:do:batch", "img:list"))
        elif data == "img:do:batch":
            selected = _selected_updates()
            if not selected:
                cached = _cached_image_report()
                if cached:
                    text, markup = cached
                    _edit_or_send(chat_id, message_id,
                                  text + "\n\n⚠️ 还没勾选，先点容器名勾选再升级。", markup)
            else:
                _edit_or_send(chat_id, message_id,
                              f"⬆️ 开始批量升级 {len(selected)} 个容器…逐个更新，完成后会发汇总并回到剩余列表。",
                              kb([[btn("⬅️ 返回更新列表", "img:list"),
                                   btn("⬅️ 主菜单", "menu:main")]]))
                update_containers_batch_async([r["cid"] for r in selected], chat_id, message_id)
        elif data == "img:ignore:batch":
            selected = _selected_updates()
            if selected:
                state = _load_image_check_state()
                ignored = state.get("ignored", {})
                for r in selected:
                    ignored[r["cid"]] = {"name": r["name"], "image": r["image"],
                                         "new_id": r.get("new_id", "")}
                    _IMAGE_CHECK_SELECTED.discard(r["cid"])
                state["ignored"] = ignored
                _save_image_check_state(state)
            cached = _cached_image_report()
            if cached:
                text, markup = cached
                _edit_or_send(chat_id, message_id, text, markup)
        elif data.startswith("img:ignore:"):
            shortid = data.rsplit(":", 1)[1]
            state = _load_image_check_state()
            notified = state.get("notified", {})
            ignored = state.get("ignored", {})
            full_cid, entry = None, None
            for r in _LAST_IMAGE_CHECK_RESULTS:
                if r.get("cid", "").startswith(shortid) and r.get("status") == "update":
                    entry, full_cid = r, r["cid"]
                    break
            if entry is not None:
                ignored[full_cid] = {"name": entry["name"], "image": entry["image"],
                                     "new_id": entry.get("new_id", "")}
            elif any(c.startswith(shortid) for c in notified):
                full_cid = next(c for c in notified if c.startswith(shortid))
                try:
                    c = resolve_container(full_cid)
                    try:
                        _ign_image = _container_image_ref(c, inspect_container(c["Id"]))
                    except Exception:
                        _ign_image = (c.get("Image") or "").strip()
                    ignored[full_cid] = {"name": container_display_name(c),
                                         "image": _ign_image,
                                         "new_id": notified[full_cid]}
                except Exception:
                    ignored[full_cid] = {"name": shortid, "image": "",
                                         "new_id": notified[full_cid]}
            if full_cid is None:
                _edit_or_send(chat_id, message_id, "找不到这条更新记录，请重新点“容器更新”检查。", back_keyboard())
            else:
                state["ignored"] = ignored
                _save_image_check_state(state)
                if _LAST_IMAGE_CHECK_RESULTS:
                    text, markup = format_image_check_report(_LAST_IMAGE_CHECK_RESULTS)
                    _edit_or_send(chat_id, message_id, text, markup)
                else:
                    _edit_or_send(chat_id, message_id,
                                  f"已忽略 {ignored[full_cid].get('name') or shortid} 的这次更新，下次有新镜像时会再提醒。",
                                  back_keyboard())
        elif data.startswith("img:unignore:"):
            shortid = data.rsplit(":", 1)[1]
            state = _load_image_check_state()
            ignored = state.get("ignored", {})
            full_cid = next((c for c in ignored if c.startswith(shortid)), None)
            if full_cid is None:
                _edit_or_send(chat_id, message_id, "这条忽略记录已经不在了。", back_keyboard())
            else:
                name = (ignored[full_cid] or {}).get("name") or shortid
                del ignored[full_cid]
                state["ignored"] = ignored
                _save_image_check_state(state)
                cached = _cached_image_report()
                if cached:
                    text, markup = cached
                    _edit_or_send(chat_id, message_id, text, markup)
                else:
                    _edit_or_send(chat_id, message_id,
                                  f"✅ 已恢复 {name}：下次检查到新镜像时会再提醒。",
                                  kb([[btn("🔍 容器更新", "menu:container_updates"),
                                       btn("⬅️ 主菜单", "menu:main")]]))
        elif data == "menu:alerts":
            _edit_or_send(chat_id, message_id, alerts_status_text(), alerts_keyboard())
        elif data in ("alerts:on", "alerts:off"):
            set_alerts_enabled(data == "alerts:on")
            _edit_or_send(chat_id, message_id, alerts_status_text(), alerts_keyboard())
        elif data == "prune:ask":
            _edit_or_send(chat_id, message_id,
                          "确定要清理悬空镜像吗？只删没有被任何容器引用的悬空镜像，不删正在用的镜像。",
                          confirm_keyboard("prune:do", "menu:manage:0"))
        elif data == "prune:do":
            count, reclaimed = prune_images()
            keyboard, page, total_pages, total = manage_keyboard(0)
            _edit_or_send(chat_id, message_id,
                          f"🧹 清理完成：删了 {count} 个悬空镜像，释放 {human_bytes(reclaimed)}。",
                          keyboard)
        elif data.startswith("c:detail:"):
            shortid = data.rsplit(":", 1)[1]
            c = resolve_container(shortid)
            _edit_or_send(chat_id, message_id, detail_text_for(c),
                          container_detail_keyboard(c["Id"][:12]))
        elif data.startswith("c:logs:"):
            shortid = data.rsplit(":", 1)[1]
            c = resolve_container(shortid)
            logs = container_logs(c["Id"][:12], 50)
            send_reply(chat_id, f"📋 {container_display_name(c)} 日志（最近 50 行）\n{logs or '（空）'}")
        elif data.startswith("c:ask:update:"):
            shortid = data.rsplit(":", 1)[1]
            c = resolve_container(shortid)
            try:
                _upd_image = _container_image_ref(c, inspect_container(c["Id"]))
            except Exception:
                _upd_image = (c.get("Image") or "").strip()
            _cancel = "img:list" if _LAST_IMAGE_CHECK_RESULTS else f"c:detail:{c['Id'][:12]}"
            _edit_or_send(chat_id, message_id,
                          f"确定要更新容器“{container_display_name(c)}”吗？\n会拉取新镜像（{_upd_image or '?'}）并重建容器，旧容器保留到新容器跑起来后才删，失败会自动回滚。\n只适合常驻服务容器；compose 管理的栈建议用 compose 更新。",
                          confirm_keyboard(f"c:do:update:{c['Id'][:12]}", _cancel))
        elif data.startswith("c:do:update:"):
            shortid = data.rsplit(":", 1)[1]
            c = resolve_container(shortid)
            _start_kb = (kb([[btn("⬅️ 返回更新列表", "img:list"),
                              btn("⬅️ 主菜单", "menu:main")]])
                         if _LAST_IMAGE_CHECK_RESULTS else back_keyboard())
            _edit_or_send(chat_id, message_id,
                          f"⬆️ 开始更新“{container_display_name(c)}”…正在拉取镜像，完成后会发新消息并回到剩余更新列表，过程可能要几分钟。",
                          _start_kb)
            update_container_async(shortid, chat_id, message_id)
        elif data.startswith("c:search:"):
            shortid = data.rsplit(":", 1)[1]
            c = resolve_container(shortid)
            _set_pending_search(chat_id, c["Id"][:12])
            _edit_or_send(chat_id, message_id,
                          f"🔍 请直接回复关键词，搜索“{container_display_name(c)}”最近 200 行日志（5 分钟内有效）。",
                          kb([[btn("❌ 取消", f"c:detail:{c['Id'][:12]}")]]))
        elif data.startswith("c:ask:"):
            _action, shortid = data[len("c:ask:"):].split(":", 1)
            c = resolve_container(shortid)
            _edit_or_send(chat_id, message_id,
                          f"确定要{ACTION_LABEL.get(_action, _action)}容器“{container_display_name(c)}”吗？",
                          confirm_keyboard(f"c:do:{_action}:{c['Id'][:12]}", f"c:detail:{c['Id'][:12]}"))
        elif data.startswith("c:do:"):
            _action, shortid = data[len("c:do:"):].split(":", 1)
            c = container_action(shortid, _action)
            fresh = resolve_container(c["Id"][:12])
            _edit_or_send(chat_id, message_id,
                          f"✅ {ACTION_LABEL.get(_action, _action)}已执行。\n\n" + detail_text_for(fresh),
                          container_detail_keyboard(fresh["Id"][:12]))
        elif data.startswith("e:detail:"):
            vmid = data.rsplit(":", 1)[1]
            v = esxi_resolve(vmid)
            _edit_or_send(chat_id, message_id,
                          f"🖥️ {v['name']}\nID：{v['vmid']}\n电源：{v['state']}",
                          vm_detail_keyboard(v["vmid"]))
        elif data.startswith("e:ask:"):
            op, vmid = data[len("e:ask:"):].split(":", 1)
            v = esxi_resolve(vmid)
            _edit_or_send(chat_id, message_id,
                          f"确定要对 VM“{v['name']}”执行{ACTION_LABEL.get(op, op)}吗？",
                          confirm_keyboard(f"e:do:{op}:{v['vmid']}", f"e:detail:{v['vmid']}"))
        elif data.startswith("e:do:"):
            op, vmid = data[len("e:do:"):].split(":", 1)
            v, out = esxi_power(vmid, op)
            _edit_or_send(chat_id, message_id,
                          f"✅ {v['name']}（{v['vmid']}）{ACTION_LABEL.get(op, op)}：{out or '命令已发送'}",
                          vm_detail_keyboard(v["vmid"]))
        else:
            _edit_or_send(chat_id, message_id, "不认识的按钮，回到主菜单。", back_keyboard())
    except Exception as e:
        log("callback error:", traceback.format_exc(limit=3))
        try:
            _edit_or_send(chat_id, message_id, f"出错了：{e}", back_keyboard())
        except Exception:
            log("callback 错误回复也失败")


# ---------------- 主循环 ----------------

def load_offset():
    try:
        with open(os.path.join(DATA_DIR, "offset.txt")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def save_offset(offset):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = os.path.join(DATA_DIR, "offset.txt.tmp")
    with open(tmp, "w") as f:
        f.write(str(offset))
    os.replace(tmp, os.path.join(DATA_DIR, "offset.txt"))


def main():
    if not BOT_TOKEN or not OWNER_CHAT_ID:
        log("缺少 BOT_TOKEN 或 OWNER_CHAT_ID，退出。")
        sys.exit(1)
    me = None
    for attempt in range(5):
        try:
            me = tg_call("getMe", {})
            break
        except Exception as e:
            log(f"getMe 失败（{attempt + 1}/5）：{e}，10 秒后重试")
            time.sleep(10)
    if me is None:
        log("getMe 多次失败，退出。请检查 SOCKS_PROXY 和 BOT_TOKEN。")
        sys.exit(1)
    log("启动：@", me.get("username"), "主人：", OWNER_CHAT_ID,
        "代理：", SOCKS_PROXY or "直连",
        "ESXi：", "已配置" if esxi_configured() else "未配置",
        "磁盘路径：", ",".join(HOST_DATA_PATHS),
        "镜像检查：", ("开/" + str(IMAGE_CHECK_INTERVAL_SEC) + "s") if IMAGE_CHECK_ENABLED else "关",
        "版本：", BOT_VERSION)
    _alert_init_baseline()
    last_health = time.monotonic()
    next_image_check = time.monotonic() + 60  # 启动 60 秒后先查一次，之后按间隔自动查
    offset = load_offset()
    while True:
        try:
            if time.monotonic() - last_health >= ALERT_INTERVAL_SEC:
                last_health = time.monotonic()
                health_check_safe()
            if IMAGE_CHECK_ENABLED and time.monotonic() >= next_image_check:
                next_image_check = time.monotonic() + IMAGE_CHECK_INTERVAL_SEC
                start_image_check(manual=False, chat_id=OWNER_CHAT_ID)
            payload = {"timeout": 30, "allowed_updates": ["message", "callback_query"], "limit": 20}
            if offset is not None:
                payload["offset"] = offset
            updates = tg_call("getUpdates", payload, timeout=45)
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    if "callback_query" in upd:
                        query = upd["callback_query"]
                        sender = (query.get("from") or {}).get("id")
                        if sender == OWNER_CHAT_ID:
                            handle_callback(query)
                        else:
                            answer_callback(query.get("id"), "不是主人，忽略。", show_alert=True)
                    else:
                        msg = upd.get("message") or {}
                        sender = (msg.get("from") or {}).get("id")
                        if sender == OWNER_CHAT_ID:
                            text = msg.get("text", "")
                            if text:
                                handle(text, msg["chat"]["id"], msg["message_id"])
                            elif msg.get("document"):
                                handle_document_update(msg)
                            else:
                                send_reply(msg["chat"]["id"],
                                           "3 号看不了照片和文件，参考图请发给 2 号机器人。点下面的按钮查 NAS、管容器。",
                                           reply_to=msg["message_id"], reply_markup=main_keyboard())
                except Exception:
                    # 回复失败也不重放：宁可丢一条结果，不重复执行管理操作。
                    log("处理失败：", traceback.format_exc(limit=3))
                save_offset(offset)
        except requests.exceptions.RequestException as e:
            log("网络错误，5 秒后重试：", e)
            time.sleep(5)
        except Exception:
            log("主循环异常：", traceback.format_exc(limit=3))
            time.sleep(5)


if __name__ == "__main__":
    main()
