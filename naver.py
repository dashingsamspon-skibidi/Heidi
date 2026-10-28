#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
naver.py — chuyển bài đăng MỚI của Naver Game Lounge (Trickcal Revive) lên Discord webhook.

Vì sao cần file này: link game.naver.com dán thẳng vào Discord không tự embed, nên bot gọi API nội bộ
(công khai, không cần đăng nhập) của lounge để lấy nội dung bài, rồi TỰ dựng tin nhắn (Components V2:
ảnh banner + tiêu đề + nút "Naver Lounge" + phần thân có ảnh xen giữa chữ). Bài dài sẽ được chia thành
nhiều tin nhắn vì Discord giới hạn 4000 ký tự chữ / 40 component mỗi tin.

Chạy cùng workflow với bot.py (không cần thêm job cron). Trạng thái lưu ở naver_state.json.

Biến môi trường:
  DISCORD_WEBHOOK / NAVER_DISCORD_WEBHOOK  webhook (nhiều cái ngăn cách bằng dấu phẩy); NAVER_ ưu tiên nếu có
  NAVER_LOUNGE        slug lounge (mặc định Trickcal)
  NAVER_BOARD_IDS     id bảng cần theo dõi, ngăn cách bằng dấu phẩy (mặc định 11 = bảng "Update")
  NAVER_DRY_RUN       1/true = chỉ in ra, KHÔNG gửi Discord, KHÔNG ghi state
  NAVER_FORCE_FEED_ID id bài muốn ép xử lý (để test giao diện), vd 8373557
  NAVER_LIST_BOARDS   1/true = in danh sách bảng của lounge rồi thoát
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import requests

# ─────────────────────────── CẤU HÌNH ───────────────────────────
API_BASE = os.environ.get("NAVER_API_BASE", "https://comm-api.game.naver.com/nng_main/v1").rstrip("/")
LOUNGE = os.environ.get("NAVER_LOUNGE", "Trickcal")
BOARD_IDS = [b.strip() for b in os.environ.get("NAVER_BOARD_IDS", "11").split(",") if b.strip()]
STATE_FILE = os.environ.get("NAVER_STATE_FILE", "naver_state.json")
BOT_NAME = os.environ.get("NAVER_BOT_NAME", "Heidi")
SITE = "https://game.naver.com"

LOUNGE_TITLE = "트릭컬 리바이브"      # dòng tiêu đề lớn ở đầu mỗi bài
BUTTON_LABEL = "네이버 라운지"        # chữ trên nút link
ACCENT_COLOR = 0x7B68EE               # màu vạch bên trái

# Khối link cuối bài (공식 홈페이지 - [바로 가기] ... + ảnh icon loại bài) lặp ở mọi bài → tự bỏ.
STRIP_FOOTER = True       # False = giữ nguyên, không cắt gì
DROP_THANKS_LINE = True   # bỏ luôn dòng "감사합니다." ngay trước khối link đó
# Dòng có dạng "<nhãn> - [link]" với nhãn nằm trong danh sách này mới bị coi là footer (thêm nhãn mới ở đây).
FOOTER_LABELS = ["공식 홈페이지", "아이시움 라운지", "트릭컬 노트", "Google Play", "App Store"]

MAX_POSTS_PER_RUN = 5     # chặn flood: 1 lần chạy tối đa đăng ngần này bài (lấy bài mới nhất)
KEEP_SEEN = 60            # nhớ tối đa ngần này id/bảng
MAX_MESSAGES_PER_POST = 4 # bài quá dài thì cắt, kèm ghi chú "xem đầy đủ trên Naver"
TEXT_BUDGET = 3700        # Discord cho 4000 ký tự chữ/tin — chừa dư để an toàn
COMP_BUDGET = 30          # Discord cho 40 component/tin — chừa dư để an toàn
GALLERY_MAX = 10          # tối đa ảnh trong 1 media gallery
MIN_SPLIT = 400           # còn trống ít hơn ngần này ký tự thì sang tin mới thay vì cắt nhỏ
CONT_RESERVE = 170        # chừa chỗ cho dòng "↳ tiếp theo" ở đầu các tin tiếp
NOTE_RESERVE = 220        # chừa chỗ cho ghi chú cắt bớt ở tin cuối

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
API_HEADERS = {"Referer": SITE + "/", "User-Agent": UA, "Accept": "application/json"}


def env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "y", "on")


def ulen(s):
    """Độ dài theo UTF-16 (>= số code point) — đếm dư còn hơn thiếu so với cách Discord đếm."""
    return len(s.encode("utf-16-le")) // 2


# ─────────────────────────── GỌI API NAVER ───────────────────────────
class ApiError(Exception):
    pass


def api_get(path, params=None):
    url = API_BASE + path
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=API_HEADERS, params=params, timeout=15)
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        else:
            if r.status_code == 200:
                try:
                    j = r.json()
                except ValueError:
                    raise ApiError(f"{path}: không phải JSON ({r.text[:120]!r})")
                if not isinstance(j, dict):
                    raise ApiError(f"{path}: JSON không phải object")
                if j.get("code") not in (None, 200):
                    raise ApiError(f"{path}: code={j.get('code')} {j.get('message')}")
                return j.get("content")
            if r.status_code < 500 and r.status_code != 429:
                raise ApiError(f"{path}: HTTP {r.status_code} {r.text[:120]!r}")
            last = f"HTTP {r.status_code}"
        time.sleep(2 * (attempt + 1))
    raise ApiError(f"{path}: hết lần thử ({last})")


def fid_key(fid):
    s = str(fid)
    return (0, int(s)) if s.isdigit() else (1, s)


def fetch_boards():
    c = api_get(f"/lounge/{LOUNGE}/board") or {}
    out, group = [], None
    for v in c.get("boardViews") or []:
        if v.get("viewType") == "BOARD_GROUP":
            group = v.get("groupName")
        elif isinstance(v.get("board"), dict):
            b = v["board"]
            out.append((str(b.get("boardId")), b.get("boardName"), group))
    return out


def fetch_board_feeds(board_id):
    c = api_get(f"/lounge/{LOUNGE}/new/popularFeeds/board", {"boardId": board_id, "limit": 20}) or {}
    feeds = c.get("recentFeeds")
    if feeds is None:
        raise ApiError(f"bảng {board_id}: thiếu 'recentFeeds' (các key nhận được: {sorted(c.keys())})")
    out = []
    for f in feeds:
        if isinstance(f, dict) and f.get("feedId") is not None:
            out.append({"id": str(f["feedId"]), "title": (f.get("title") or "").strip(),
                        "created": f.get("createdDate")})
    return out


def fetch_detail(feed_id):
    c = api_get(f"/community/lounge/{LOUNGE}/feed/{feed_id}") or {}
    feed = c.get("feed") or {}
    return {
        "title": (feed.get("title") or "").strip(),
        "contents": feed.get("contents") or "",
        "created": feed.get("createdDate"),
        "author": (c.get("user") or {}).get("nickname"),
        "feed_keys": sorted(feed.keys()),
    }


def parse_created(v):
    """createdDate của Naver dạng YYYYMMDDHHmmss (giờ Hàn). Chấp nhận thêm epoch/ISO cho chắc."""
    if v is None:
        return None
    s = str(v).strip()
    try:
        if s.isdigit() and len(s) == 14:
            return int(datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=KST).timestamp())
        if s.isdigit() and len(s) == 13:
            return int(s) // 1000
        if s.isdigit() and len(s) == 10:
            return int(s)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int((dt if dt.tzinfo else dt.replace(tzinfo=KST)).timestamp())
    except ValueError:
        return None


# ─────────────────────────── HTML → KHỐI NỘI DUNG ───────────────────────────
ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "aside", "ul", "ol", "li", "table",
              "thead", "tbody", "tfoot", "tr", "blockquote", "pre", "figure", "figcaption", "dl", "dt", "dd",
              "center", "h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_TAGS = {"script", "style", "noscript", "head", "template", "svg"}
VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "source", "wbr", "col", "area", "base", "embed",
             "param", "track"}
URL_RE = re.compile(r"https?://[^\s<>\]\)]+")
BOLD_STYLE_RE = re.compile(r"font-weight\s*:\s*(bold|bolder|[6-9]00)", re.I)
VIDEO_RE = re.compile(
    r"https?:(?:\\?/){2}(?:www\.|m\.)?(?:youtube\.com(?:\\?/)(?:watch\?v=|shorts(?:\\?/)|embed(?:\\?/))|youtu\.be(?:\\?/))"
    r"([A-Za-z0-9_-]{6,15})")


def esc_plain(s):
    s = s.replace("\\", "\\\\")
    s = re.sub(r"([*_`])", r"\\\1", s)
    s = s.replace("~~", "\\~\\~").replace("||", "\\|\\|")
    return s


def esc(s):
    """Escape markdown của Discord nhưng để nguyên URL (backslash trong URL làm hỏng link)."""
    out, last = [], 0
    for m in URL_RE.finditer(s):
        out.append(esc_plain(s[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(esc_plain(s[last:]))
    return "".join(out)


LABEL_MAP = str.maketrans({"[": "［", "]": "］", "*": "＊", "_": "＿", "`": "｀", "~": "～", "|": "｜", "\\": "＼"})


def esc_label(s):
    """Nhãn của link [nhãn](url): Discord KHÔNG xử lý backslash ở đây (hiện nguyên \\[ \\]) → đổi sang ký tự full-width."""
    return s.translate(LABEL_MAP)


def norm_url(u):
    u = (u or "").strip()
    if not u or u.startswith(("data:", "javascript:", "#", "blob:")):
        return None
    if u.startswith("//"):
        u = "https:" + u
    elif u.startswith("/"):
        u = SITE + u
    if not u.lower().startswith(("http://", "https://")):
        return None
    if re.match(r"http://[^/]*(naver\.com|pstatic\.net)", u, re.I):
        u = "https://" + u[len("http://"):]
    return u.replace(" ", "%20") if len(u) <= 2000 else None


class Html2Blocks(HTMLParser):
    """Đổi HTML bài viết thành danh sách khối: ("text", markdown) | ("img", url) | ("hr",)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.lines, self.chunks = [], [], []
        self.skip = 0
        self.fmt = {"b": 0, "i": 0, "u": 0, "s": 0}
        self.hrefs, self.stack, self.lists = [], [], []
        self.p_marks = []
        self.quote = 0
        self.heading = 0
        self.prefix_next = None
        self.cell_idx = 0

    # ----- tiện ích
    def state(self):
        return (self.fmt["b"] > 0, self.fmt["i"] > 0, self.fmt["u"] > 0, self.fmt["s"] > 0,
                self.hrefs[-1] if self.hrefs else None)

    def compose(self, chunks):
        merged = []
        for text, st in chunks:
            if merged and merged[-1][1] == st:
                merged[-1][0] += text
            else:
                merged.append([text, st])
        out = []
        for text, st in merged:
            text = re.sub(r"\s+", " ", text.replace("\xa0", " "))
            core = text.strip()
            if not core:
                out.append(" " if text else "")
                continue
            lead = " " if text.startswith(" ") else ""
            trail = " " if text.endswith(" ") else ""
            b, i, u, s, href = st
            if href and core == href:
                piece = core
            else:
                piece = esc_label(core) if href else esc(core)
                if b:
                    piece = f"**{piece}**"
                if i:
                    piece = f"*{piece}*"
                if u:
                    piece = f"__{piece}__"
                if s:
                    piece = f"~~{piece}~~"
                if href:
                    piece = f"[{piece}]({href.replace(')', '%29')})"
            out.append(lead + piece + trail)
        return re.sub(r" {2,}", " ", "".join(out)).strip()

    def flush_line(self):
        text = self.compose(self.chunks)
        self.chunks = []
        if not text:
            return False
        prefix = ""
        if self.prefix_next:
            prefix, self.prefix_next = self.prefix_next, None
        elif self.heading:
            prefix = "#" * min(self.heading, 3) + " "
        elif re.match(r"^(#{1,3} |-# |> )", text):
            text = "\\" + text
        if self.quote:
            prefix = "> " + prefix
        self.lines.append(prefix + text)
        return True

    def close_text(self):
        self.flush_line()
        lines, self.lines = self.lines, []
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        out, blank = [], False
        for ln in lines:
            if ln.strip():
                out.append(ln.rstrip())
                blank = False
            elif not blank:
                out.append("")
                blank = True
        if out:
            self.blocks.append(("text", "\n".join(out)))

    def add_media_line(self, url):
        url = norm_url(url)
        if url:
            self.flush_line()
            self.chunks.append((url, (False, False, False, False, None)))
            self.flush_line()

    # ----- parser callbacks
    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in VOID_TAGS:
            self.handle_void(tag, a)
            return
        fmt = None
        if tag in ("b", "strong"):
            fmt = "b"
        elif tag in ("i", "em"):
            fmt = "i"
        elif tag in ("u", "ins"):
            fmt = "u"
        elif tag in ("s", "strike", "del"):
            fmt = "s"
        elif BOLD_STYLE_RE.search(a.get("style", "")):
            fmt = "b"
        href_pushed = False
        if tag == "a":
            h = norm_url(a.get("href"))
            if h:
                self.hrefs.append(h)
                href_pushed = True
        if fmt:
            self.fmt[fmt] += 1
        self.stack.append((tag, fmt, href_pushed))

        if tag in ("ul", "ol"):
            self.flush_line()
            self.lists.append([tag, 0])
        elif tag == "li":
            self.flush_line()
            if self.lists:
                lst = self.lists[-1]
                lst[1] += 1
                marker = f"{lst[1]}. " if lst[0] == "ol" else "- "
                self.prefix_next = "  " * (len(self.lists) - 1) + marker
            else:
                self.prefix_next = "- "
        elif tag == "blockquote":
            self.flush_line()
            self.quote += 1
        elif tag == "tr":
            self.flush_line()
            self.cell_idx = 0
        elif tag in ("td", "th"):
            if self.cell_idx > 0:
                self.chunks.append((" | ", (False, False, False, False, None)))
            self.cell_idx += 1
        elif tag == "p":
            self.flush_line()
            self.p_marks.append(len(self.lines))
        elif re.fullmatch(r"h[1-6]", tag):
            self.flush_line()
            self.heading = int(tag[1])
        elif tag in BLOCK_TAGS:
            self.flush_line()
        elif tag in ("iframe", "video", "audio"):
            self.add_media_line(a.get("src"))

    def handle_void(self, tag, a):
        if tag == "br":
            if not self.flush_line():
                self.lines.append("")
        elif tag == "hr":
            self.close_text()
            if not self.blocks or self.blocks[-1][0] != "hr":
                self.blocks.append(("hr",))
        elif tag == "img":
            url = None
            for k in ("data-src", "data-lazy-src", "data-original", "src"):
                url = norm_url(a.get(k))
                if url:
                    break
            if not url:
                return
            w, h = re.sub(r"\D", "", a.get("width", "")), re.sub(r"\D", "", a.get("height", ""))
            if w and h and int(w) <= 48 and int(h) <= 48:
                return  # icon/emoji nhỏ
            self.close_text()
            if not self.blocks or self.blocks[-1] != ("img", url):
                self.blocks.append(("img", url))
        elif tag in ("source", "embed"):
            self.add_media_line(a.get("src"))

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag in VOID_TAGS:
            return
        idx = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if idx is None:
            return
        popped = self.stack[idx:]
        del self.stack[idx:]
        for _, fmt, href_pushed in popped:
            if fmt:
                self.fmt[fmt] = max(0, self.fmt[fmt] - 1)
            if href_pushed and self.hrefs:
                self.hrefs.pop()

        if tag in ("ul", "ol"):
            self.flush_line()
            if self.lists:
                self.lists.pop()
        elif tag == "li":
            self.flush_line()
            self.prefix_next = None
        elif tag == "blockquote":
            self.flush_line()
            self.quote = max(0, self.quote - 1)
        elif tag == "p":
            self.flush_line()
            if self.p_marks:
                mark = self.p_marks.pop()
                if len(self.lines) == mark:
                    self.lines.append("")
        elif re.fullmatch(r"h[1-6]", tag):
            self.flush_line()
            self.heading = 0
        elif tag in BLOCK_TAGS:
            self.flush_line()

    def handle_data(self, data):
        if self.skip:
            return
        data = data.translate(ZERO_WIDTH)
        if data:
            self.chunks.append((data, self.state()))


def _label_re(label):
    return r"\s*".join(re.escape(w) for w in label.split())


FOOTER_LINE_RE = re.compile(
    r"^(?:" + "|".join(_label_re(l) for l in FOOTER_LABELS) + r")[^\-–—:：]{0,24}[-–—:：]", re.I)
THANKS_RE = re.compile(r"^[\s*_~]*감사합니다[.!~\s]*[*_~\s]*$")


def plain_md(line):
    s = re.sub(r"\[([^\]]*)\]\([^)\s]*\)", r"\1", line)      # [nhãn](url) → nhãn
    s = re.sub(r"[*_~`\\]", "", s)                               # bỏ ký hiệu markdown
    return re.sub(r"\s+", " ", s).strip()


def is_footer_line(line):
    return "](" in line and bool(FOOTER_LINE_RE.match(plain_md(line)))


def strip_footer(blocks):
    """Cắt khối link cuối bài: các dòng '<nhãn> - [link]' (>= 2 dòng) + dòng '감사합니다.' kèm theo + ảnh/đường kẻ đuôi."""
    ti = max((i for i, b in enumerate(blocks) if b[0] == "text"), default=None)
    if ti is None:
        return blocks
    lines = blocks[ti][1].split("\n")
    foot = [i for i, l in enumerate(lines) if is_footer_line(l)]
    if len(foot) < 2:
        return blocks
    start = foot[0]
    footer_is_tail = all(is_footer_line(l) or not l.strip() for l in lines[start:])
    kept = lines[:start] if footer_is_tail else [l for i, l in enumerate(lines) if i not in set(foot)]
    while kept and not kept[-1].strip():
        kept.pop()
    if footer_is_tail and DROP_THANKS_LINE and kept and THANKS_RE.match(kept[-1]):
        kept.pop()
        while kept and not kept[-1].strip():
            kept.pop()
    out = blocks[:ti]
    if kept:
        out.append(("text", "\n".join(kept)))
    rest = blocks[ti + 1:]
    if footer_is_tail:
        rest = [b for b in rest if b[0] not in ("img", "hr")]   # icon loại bài / đường kẻ nằm sau link
    out += rest
    while out and out[-1][0] == "hr":
        out.pop()
    return out


def contents_to_blocks(contents):
    """contents của bài (HTML; hoặc JSON SmartEditor nếu API trả kiểu đó) → danh sách khối."""
    raw = contents or ""
    s = raw.lstrip()
    if s.startswith("{"):
        blocks = json_contents_to_blocks(s)
    else:
        p = Html2Blocks()
        p.feed(raw)
        p.close()
        p.close_text()
        blocks = p.blocks
    if STRIP_FOOTER:
        blocks = strip_footer(blocks)
    # link video (YouTube) có thể nằm trong thuộc tính/script nên parser bỏ sót → nhặt thêm cho đủ
    seen_text = "\n".join(b[1] for b in blocks if b[0] == "text")
    extra, ids = [], set()
    for m in VIDEO_RE.finditer(raw):
        vid = m.group(1)
        if vid not in ids:
            ids.add(vid)
            if vid not in seen_text:
                extra.append(f"▶ https://youtu.be/{vid}")
    if extra:
        if blocks and blocks[-1][0] == "text":
            blocks[-1] = ("text", blocks[-1][1] + "\n\n" + "\n".join(extra))
        else:
            blocks.append(("text", "\n".join(extra)))
    return blocks


def json_contents_to_blocks(s):
    """Phương án dự phòng nếu contents là JSON SmartEditor: chỉ lấy chữ + ảnh nhận ra được."""
    try:
        doc = json.loads(s)
    except ValueError:
        return []
    texts, blocks = [], []

    def flush():
        t = re.sub(r"[ \t]+", " ", " ".join(texts)).strip()
        texts.clear()
        if t:
            blocks.append(("text", esc(t)))

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            ct = n.get("@ctype")
            if ct == "textNode" and isinstance(n.get("value"), str):
                texts.append(n["value"].translate(ZERO_WIDTH))
            elif ct in ("paragraph", "text"):
                walk([n.get(k) for k in ("nodes", "value") if n.get(k)])
                texts.append("\n")
            elif ct == "image":
                u = norm_url(n.get("src") or n.get("path"))
                if u:
                    flush()
                    blocks.append(("img", u))
            else:
                for k in ("value", "nodes", "components"):
                    if n.get(k):
                        walk(n[k])

    root = (doc.get("document") or doc) if isinstance(doc, dict) else doc
    walk(root.get("components") if isinstance(root, dict) else root)
    flush()
    return blocks


# ─────────────────────────── DỰNG TIN NHẮN DISCORD (COMPONENTS V2) ───────────────────────────
def c_text(content):
    return {"type": 10, "content": content}


def c_gallery(urls):
    return {"type": 12, "items": [{"media": {"url": u}} for u in urls[:GALLERY_MAX]]}


def c_sep():
    return {"type": 14, "divider": True, "spacing": 1}


def c_button(label, url):
    return {"type": 2, "style": 5, "label": label[:80], "url": url[:512]}


def c_section(text, button):
    return {"type": 9, "components": [c_text(text)], "accessory": button}


def c_container(children):
    return {"type": 17, "accent_color": ACCENT_COLOR, "components": children}


def split_at(text, limit):
    """Cắt text thành (đầu <= limit, phần còn lại), ưu tiên ranh giới đoạn > dòng > khoảng trắng."""
    if ulen(text) <= limit:
        return text, ""
    cut_limit = limit
    while ulen(text[:cut_limit]) > limit:
        cut_limit -= max(1, (ulen(text[:cut_limit]) - limit) // 2)
    head_zone = text[:cut_limit]
    for sep in ("\n\n", "\n", " "):
        i = head_zone.rfind(sep)
        if i >= cut_limit * 0.3:
            return text[:i].rstrip(), text[i + len(sep):].lstrip("\n ")
    return head_zone, text[cut_limit:]


def blocks_to_units(blocks):
    units, imgs = [], []

    def flush_imgs():
        nonlocal imgs
        while imgs:
            units.append(("gallery", imgs[:GALLERY_MAX]))
            imgs = imgs[GALLERY_MAX:]

    for b in blocks:
        if b[0] == "img":
            imgs.append(b[1])
            continue
        flush_imgs()
        if b[0] == "text":
            units.append(("text", b[1]))
        elif b[0] == "hr" and units and units[-1][0] != "hr":
            units.append(("hr", None))
    flush_imgs()
    while units and units[-1][0] == "hr":
        units.pop()
    return units


def short(s, n):
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def build_messages(post):
    """post = {id, title, epoch, url, blocks} → list payload (mỗi payload = 1 tin nhắn Discord)."""
    blocks = list(post["blocks"])
    banner = None
    if blocks and blocks[0][0] == "img":
        banner = blocks.pop(0)[1]
    units = blocks_to_units(blocks)

    # ----- header (chỉ ở tin đầu)
    title = short(esc(post["title"] or "(không có tiêu đề)"), 300)
    meta = f"<t:{post['epoch']}:f> | ID: {post['id']}" if post.get("epoch") else f"ID: {post['id']}"
    hdr_text = f"# {LOUNGE_TITLE}\n**{title}**"
    hdr_children = ([c_gallery([banner])] if banner else []) + [
        c_text(hdr_text), c_sep(), c_section(meta, c_button(BUTTON_LABEL, post["url"]))]
    hdr_cost_text = ulen(hdr_text) + ulen(meta) + ulen(BUTTON_LABEL)
    hdr_cost_comp = 1 + len(hdr_children) + 2  # container + children + (text trong section) + nút

    messages = []

    def new_message(first):
        m = {"containers": [], "text": 0, "comps": 0, "body": None, "label": None, "first": first}
        if first:
            m["containers"].append(c_container(hdr_children))
            m["text"], m["comps"] = hdr_cost_text, hdr_cost_comp
        messages.append(m)
        return m

    def budget(m):
        return TEXT_BUDGET - (NOTE_RESERVE if len(messages) >= MAX_MESSAGES_PER_POST else 0)

    def ensure_body(m):
        if m["body"] is None:
            m["body"] = c_container([])
            m["containers"].append(m["body"])
            m["comps"] += 1
            if not m["first"]:
                m["label"] = c_text("-# ↳")
                m["body"]["components"].append(m["label"])
                m["comps"] += 1
                m["text"] += CONT_RESERVE
        return m["body"]

    def comp_need(m):
        return 1 + (0 if m["body"] is not None else (1 if m["first"] else 2))

    m = new_message(True)
    truncated = False
    queue = list(units)
    while queue:
        kind, val = queue.pop(0)
        need_text = ulen(val) if kind == "text" else 0
        fits_comp = m["comps"] + comp_need(m) <= COMP_BUDGET
        room = budget(m) - m["text"]
        if fits_comp and need_text <= room:
            body = ensure_body(m)
            body["components"].append(
                c_text(val) if kind == "text" else c_gallery(val) if kind == "gallery" else c_sep())
            m["comps"] += 1
            m["text"] += need_text
            continue
        if kind == "text" and fits_comp and room >= MIN_SPLIT:
            head, tail = split_at(val, room)
            if head:
                body = ensure_body(m)
                body["components"].append(c_text(head))
                m["comps"] += 1
                m["text"] += ulen(head)
                if tail:
                    queue.insert(0, ("text", tail))
                continue
        # không vừa → sang tin mới (hoặc cắt bớt nếu đã đủ số tin cho phép)
        if len(messages) >= MAX_MESSAGES_PER_POST:
            truncated = True
            break
        m = new_message(False)
        queue.insert(0, (kind, val))

    if truncated:
        body = ensure_body(m)
        body["components"].append(c_text("-# … Bài còn dài, xem đầy đủ bằng nút Naver ở đầu bài."))
        m["comps"] += 1

    total = len(messages)
    payloads = []
    for n, m in enumerate(messages, 1):
        if m["label"] is not None:
            m["label"]["content"] = f"-# ↳ {short(title, 80)} · {n}/{total}"
        payloads.append({
            "username": BOT_NAME,
            "flags": 1 << 15,
            "components": m["containers"],
            "allowed_mentions": {"parse": []},
        })
    return payloads


def check_limits(payload):
    """Kiểm tra payload theo giới hạn cứng của Discord (dùng để log cảnh báo + test)."""
    problems, stats = [], {"comps": 0, "text": 0}

    def walk(c):
        stats["comps"] += 1
        t = c.get("type")
        if t == 10:
            if not (c.get("content") or "").strip():
                problems.append("TextDisplay rỗng")
            stats["text"] += ulen(c.get("content") or "")
        elif t == 2:
            stats["text"] += ulen(c.get("label") or "")
            if len(c.get("label") or "") > 80 or len(c.get("url") or "") > 512:
                problems.append("nút vượt giới hạn label/url")
        elif t == 12 and not 1 <= len(c.get("items") or []) <= 10:
            problems.append("media gallery phải có 1-10 ảnh")
        for k in c.get("components") or []:
            walk(k)
        if c.get("accessory"):
            walk(c["accessory"])

    comps = payload.get("components") or []
    for c in comps:
        walk(c)
    if len(comps) > 10:
        problems.append(f"{len(comps)} component cấp cao nhất (tối đa 10)")
    if stats["comps"] > 40:
        problems.append(f"{stats['comps']} component (tối đa 40)")
    if stats["text"] > 4000:
        problems.append(f"{stats['text']} ký tự chữ (tối đa 4000)")
    return problems, stats


# ─────────────────────────── GỬI DISCORD ───────────────────────────
def send(webhook, payload):
    """Trả về 'ok' | 'transient' (lỗi tạm: 5xx/429/mạng) | 'permanent' (4xx: webhook sai/xoá, payload sai)."""
    kind = "transient"
    for attempt in range(4):
        try:
            r = requests.post(webhook, params={"with_components": "true", "wait": "true"},
                              json=payload, timeout=15)
        except requests.RequestException as e:
            print(f"   ⚠️ lỗi mạng: {type(e).__name__}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (200, 204):
            return "ok"
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except (ValueError, AttributeError):
                wait = float(r.headers.get("Retry-After", 2) or 2)
            print(f"   ⏳ rate limit, chờ {wait:.1f}s")
            time.sleep(min(wait, 15) + 0.5)
            continue
        if r.status_code >= 500:
            print(f"   ⚠️ Discord {r.status_code}, thử lại")
            time.sleep(2 * (attempt + 1))
            continue
        print(f"   ❌ Discord {r.status_code}: {r.text[:300]}")
        return "permanent"
    return kind


# ─────────────────────────── TRẠNG THÁI ───────────────────────────
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        if isinstance(s, dict) and isinstance(s.get("boards"), dict):
            return s
    except (FileNotFoundError, ValueError):
        pass
    return {"boards": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


# ─────────────────────────── LUỒNG CHÍNH ───────────────────────────
def prepare_post(feed_id):
    d = fetch_detail(feed_id)
    blocks = contents_to_blocks(d["contents"])
    return {
        "id": str(feed_id), "title": d["title"], "epoch": parse_created(d["created"]),
        "url": f"{SITE}/lounge/{LOUNGE}/board/detail/{feed_id}", "blocks": blocks,
        "author": d["author"], "raw_len": len(d["contents"]), "feed_keys": d["feed_keys"],
        "raw_head": d["contents"][:300],
    }


def deliver(post, webhooks, dry):
    """Dựng + gửi 1 bài. Trả về 'done' (coi như đã xử lý xong) hoặc 'retry' (để lần sau thử lại)."""
    payloads = build_messages(post)
    print(f"📰 [{post['id']}] {post['title']}")
    print(f"   tác giả={post['author']} · khối={len(post['blocks'])} · {len(payloads)} tin nhắn")
    if not post["blocks"]:
        print(f"   ⚠️ không trích được nội dung. Đầu contents: {post['raw_head']!r}")
    for i, p in enumerate(payloads, 1):
        problems, st = check_limits(p)
        texts = []

        def grab(c):
            if c.get("type") == 10:
                texts.append(c["content"])
            for k in c.get("components") or []:
                grab(k)
        for c in p["components"]:
            grab(c)
        imgs = json.dumps(p, ensure_ascii=False).count('"media"')
        print(f"   tin {i}/{len(payloads)}: {st['comps']} component · {st['text']} ký tự · {imgs} ảnh"
              + (f" · ⚠️ {problems}" if problems else ""))
        if dry:
            for t in texts:
                print("      │ " + t[:400].replace("\n", "\n      │ ") + (" …" if len(t) > 400 else ""))
    if dry:
        return "done"
    results = []
    for wi, wh in enumerate(webhooks, 1):
        res = "ok"
        for p in payloads:
            res = send(wh, p)
            if res != "ok":
                break
            time.sleep(0.8)
        print(f"   → webhook #{wi}: {res}")
        results.append(res)
    if "ok" in results or all(r == "permanent" for r in results):
        return "done" if all(r == "ok" for r in results) else "done_with_errors"
    return "retry"


def main():
    dry = env_flag("NAVER_DRY_RUN")
    raw_wh = os.environ.get("NAVER_DISCORD_WEBHOOK") or os.environ.get("DISCORD_WEBHOOK") or ""
    webhooks = [w.strip() for w in raw_wh.split(",") if w.strip()]
    print(f"Naver lounge '{LOUNGE}' · bảng {BOARD_IDS} · webhook: {len(webhooks)}"
          + (" · DRY-RUN (không gửi, không ghi state)" if dry else ""))
    if not webhooks and not dry:
        print("❌ Thiếu DISCORD_WEBHOOK / NAVER_DISCORD_WEBHOOK")
        return 1

    if env_flag("NAVER_LIST_BOARDS"):
        for bid, name, group in fetch_boards():
            print(f"  {bid:>6} | {name} | nhóm: {group}")
        return 0

    state, changed, errors = load_state(), False, 0

    for bid in BOARD_IDS:
        try:
            feeds = fetch_board_feeds(bid)
        except ApiError as e:
            print(f"❌ {e}")
            errors += 1
            continue
        if not feeds:
            print(f"⚠️ bảng {bid}: danh sách trống — bỏ qua (không seed để tránh flood lần sau)")
            continue
        ids_new_first = [f["id"] for f in sorted(feeds, key=lambda f: fid_key(f["id"]), reverse=True)]
        bs = state["boards"].get(bid)
        if bs is None:
            print(f"🌱 bảng {bid}: lần đầu → ghi nhớ {len(ids_new_first)} bài hiện có, KHÔNG đăng")
            state["boards"][bid] = {"seen": ids_new_first[:KEEP_SEEN]}
            changed = True
            continue
        seen = set(bs.get("seen", []))
        unseen = sorted((f for f in feeds if f["id"] not in seen), key=lambda f: fid_key(f["id"]))
        print(f"bảng {bid}: {len(feeds)} bài trong danh sách, {len(unseen)} bài mới")
        if len(unseen) > MAX_POSTS_PER_RUN:
            print(f"⚠️ {len(unseen)} bài mới > {MAX_POSTS_PER_RUN} — chỉ đăng {MAX_POSTS_PER_RUN} bài mới nhất")
        handled = []
        for f in unseen[-MAX_POSTS_PER_RUN:]:
            try:
                post = prepare_post(f["id"])
            except ApiError as e:
                print(f"❌ bài {f['id']}: {e} → để lần sau thử lại")
                errors += 1
                continue
            if not post["title"]:
                post["title"] = f["title"]
            outcome = deliver(post, webhooks, dry)
            if outcome == "retry":
                errors += 1
                print(f"   ↻ bài {f['id']} chưa gửi được ở webhook nào → lần sau thử lại")
            else:
                handled.append(f["id"])
                if outcome == "done_with_errors":
                    errors += 1
        skipped = [f["id"] for f in unseen[:-MAX_POSTS_PER_RUN]] if len(unseen) > MAX_POSTS_PER_RUN else []
        newly_seen = handled + skipped
        if newly_seen and not dry:
            merged = sorted(set(bs.get("seen", [])) | set(newly_seen), key=fid_key, reverse=True)
            bs["seen"] = merged[:KEEP_SEEN]
            changed = True

    for fid in [x.strip() for x in os.environ.get("NAVER_FORCE_FEED_ID", "").split(",") if x.strip()]:
        print(f"🔧 ép xử lý bài {fid}")
        try:
            post = prepare_post(fid)
        except ApiError as e:
            print(f"❌ bài {fid}: {e}")
            errors += 1
            continue
        deliver(post, webhooks, dry)

    if changed and not dry:
        save_state(state)
        print("💾 đã lưu naver_state.json")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
