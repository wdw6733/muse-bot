"""bot3 v2.4.0 tests (durable copy: /tmp is wiped on VM restart).

Covers: container/VM list button labels are equal display width so the
status dots align in the 3-column grid, plus version bump regression.
Run: BOT_TOKEN=<redacted>
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot

bot.DATA_DIR = tempfile.mkdtemp(prefix="bot3v231-")
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" {extra}" if extra and not cond else ""))


# ---------- version ----------
check("version", bot.BOT_VERSION == "2.4.0")
check("welcome_version", "v2.4.0" in bot.WELCOME)

# ---------- display width / padding ----------
check("width_ascii", bot.display_width("tg-bot3") == 7)
check("width_cjk", bot.display_width("群晖NAS") == 7)
check("width_mixed", bot.display_width("nas-群晖") == 8)
check("pad_short", bot.pad_name("ab") == "ab" + " " * 10 and bot.display_width(bot.pad_name("ab")) == 12)
check("pad_exact", bot.pad_name("123456789012") == "123456789012")
check("pad_cjk", bot.display_width(bot.pad_name("群晖NAS")) == 12)
check("pad_truncate", bot.display_width(bot.pad_name("a" * 20)) == 12 and bot.pad_name("a" * 20).endswith("…"))
check("pad_truncate_cjk", bot.display_width(bot.pad_name("群晖NAS群晖NAS群晖")) == 12
      and bot.pad_name("群晖NAS群晖NAS群晖").endswith("…"))

# ---------- labels equal width -> dots align ----------
labels = [bot.status_button_label("🟢", n) for n in
          ["tg-bot3", "openbox", "magpie", "群晖", "a-very-long-container-name"]]
check("labels_equal_width", len({bot.display_width(l) for l in labels}) == 1,
      str([bot.display_width(l) for l in labels]))
check("labels_start_emoji", all(l.startswith("🟢 ") for l in labels))

# ---------- manage grid regression (3 columns, dots aligned) ----------
orig_list = bot.list_containers


def fake_containers(n):
    return [{"Id": f"{i:012d}abcdef", "Names": [f"/c{i:02d}"],
             "Image": "img:1", "State": "running" if i % 2 == 0 else "exited",
             "Status": "Up" if i % 2 == 0 else "Exited"} for i in range(n)]


bot.list_containers = lambda: fake_containers(9)
kb0, page0, total_pages, total = bot.manage_keyboard(0)
rows0 = kb0["inline_keyboard"]
texts = [b["text"] for r in rows0[:3] for b in r]
check("manage_grid_3col", total == 9 and total_pages == 1 and [len(r) for r in rows0[:3]] == [3, 3, 3],
      str([len(r) for r in rows0]))
check("manage_labels_aligned", len({bot.display_width(t) for t in texts}) == 1,
      str([bot.display_width(t) for t in texts]))
check("manage_first_btn", texts[0].startswith("🟢 c00"))
check("manage_stopped_emoji", texts[1].startswith("🔴 c01"))
bot.list_containers = orig_list

# ---------- esxi list uses the same helper ----------
vms = [{"name": "NAS", "state": "on", "vmid": "1"}, {"name": "OpenWrt-soft-router", "state": "off", "vmid": "2"}]
ekb = bot.esxi_list_keyboard(vms)
etexts = [r[0]["text"] for r in ekb["inline_keyboard"][:2]]
check("esxi_labels_aligned", len({bot.display_width(t) for t in etexts}) == 1,
      str([bot.display_width(t) for t in etexts]))
check("esxi_first_btn", etexts[0].startswith("🟢 NAS"))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
