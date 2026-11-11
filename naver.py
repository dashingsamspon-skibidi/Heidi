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
  NAVER_BOARD_IDS     id bảng cần theo dõi, ngăn cách bằng dấu phẩy; mặc định "auto" = tự chọn các bảng chính thức
                      (공지사항, 업데이트, 테마극장, 진행 이벤트, 공식 웹툰…) theo tên bảng, xem AUTO_BOARD_INCLUDE/EXCLUDE
  NAVER_DRY_RUN       1/true = chỉ in ra, KHÔNG gửi Discord, KHÔNG ghi state
  NAVER_FORCE_FEED_ID id bài muốn ép xử lý (để test giao diện), vd 8373557
  NAVER_LIST_BOARDS   1/true = in danh sách bảng của lounge rồi thoát
"""
import copy
import html
import json
import os
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import requests

# ─────────────────────────── CẤU HÌNH ───────────────────────────
API_BASE = os.environ.get("NAVER_API_BASE", "https://comm-api.game.naver.com/nng_main/v1").rstrip("/")
LOUNGE = os.environ.get("NAVER_LOUNGE", "Trickcal")
FALLBACK_BOARD_IDS = ["11"]   # dùng tạm nếu không lấy được/khớp được danh sách bảng
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

# Bài có TIÊU ĐỀ khớp 1 trong các mẫu regex này sẽ bị bỏ qua (X đã đăng rồi), không phân biệt hoa/thường.
# Hiện bỏ các bài webtoon của BOGO. Muốn bỏ thêm: thêm mẫu, vd r"^\s*[\[［]\s*판토\s*[\]］]" (Panto).
SKIP_TITLE_PATTERNS = [r"^\s*[\[［]\s*BOGO\s*[\]］]"]

# Chế độ NAVER_BOARD_IDS=auto: theo dõi mọi bảng có TÊN khớp INCLUDE và không khớp EXCLUDE (regex, không phân biệt hoa/thường).
# Bài mới của Naver nằm rải ở nhiều bảng (공지사항, 테마극장 PV, 진행 이벤트, 공식 웹툰…), không chỉ bảng 11. Log mỗi lần chạy
# in rõ bảng nào được theo dõi / bị bỏ để bạn chỉnh hai danh sách này cho hợp.
AUTO_BOARD_INCLUDE = [r"공지", r"업데이트", r"개발자", r"테마극장", r"이벤트", r"당첨", r"공식",
                      r"notice", r"update", r"developer", r"theme", r"event", r"winner", r"official"]
AUTO_BOARD_EXCLUDE = [r"인증", r"증명", r"verify", r"proof"]

MAX_POSTS_PER_RUN = 5     # chặn flood: 1 lần chạy tối đa đăng ngần này bài (lấy bài mới nhất)
KEEP_SEEN = 100           # nhớ tối đa ngần này id/bảng
MAX_POST_AGE_HOURS = float(os.environ.get("NAVER_MAX_AGE_HOURS", "72"))  # bài 'mới' nhưng cũ hơn mức này → chỉ ghi nhớ, không đăng
MAX_MESSAGES_PER_POST = int(os.environ.get("NAVER_MAX_MESSAGES", "8"))  # bài update dài/nhiều ảnh: tối đa ngần này tin, quá thì cắt kèm ghi chú
MB = 1024 * 1024
# ── Dung lượng media (GIF/ảnh nặng): bot HEAD từng file để biết nặng bao nhiêu rồi
MEDIA_MAX_MB_EACH = float(os.environ.get("NAVER_MEDIA_MAX_MB", "10"))          # file nặng hơn → KHÔNG nhúng, chỉ để link mở file
MEDIA_MAX_MB_PER_MSG = float(os.environ.get("NAVER_MEDIA_MAX_MB_PER_MSG", "20"))  # tổng dung lượng nhúng trong 1 tin; vượt → sang tin mới
PROBE_MEDIA = os.environ.get("NAVER_PROBE_MEDIA", "1") != "0"                  # 0 = không đo dung lượng
PROBE_MAX = 60                                                                  # đo tối đa ngần này file/bài
VERIFY_MEDIA = os.environ.get("NAVER_VERIFY_MEDIA", "1") != "0"                # 1 = gửi xong đợi rồi hỏi Discord file nào tải lỗi
VERIFY_WAIT_SEC = float(os.environ.get("NAVER_VERIFY_WAIT", "12"))
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
    """Danh sách (boardId, tên bảng, tên nhóm) của lounge — tự dò trong JSON nên chịu được đổi cấu trúc nhẹ."""
    c = api_get(f"/lounge/{LOUNGE}/board")
    out, group, seen_ids = [], [None], set()

    def walk(n):
        if isinstance(n, dict):
            if n.get("groupName"):
                group[0] = n.get("groupName")
            bid = n.get("boardId")
            if bid is not None and n.get("boardName") and str(bid) not in seen_ids:
                seen_ids.add(str(bid))
                out.append((str(bid), str(n["boardName"]), group[0]))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(c)
    return out


def resolve_board_ids():
    raw = os.environ.get("NAVER_BOARD_IDS", "auto").strip()
    if raw.lower() != "auto":
        ids = [b.strip() for b in raw.split(",") if b.strip()]
        print(f"Bảng theo dõi (cố định theo NAVER_BOARD_IDS): {ids}")
        return ids
    try:
        boards = fetch_boards()
    except ApiError as e:
        print(f"⚠️ không lấy được danh sách bảng ({e}) → tạm dùng bảng {FALLBACK_BOARD_IDS}")
        return list(FALLBACK_BOARD_IDS)
    chosen, rest = [], []
    for bid, name, group in boards:
        hit = any(re.search(p, name, re.I) for p in AUTO_BOARD_INCLUDE)
        bad = any(re.search(p, name, re.I) for p in AUTO_BOARD_EXCLUDE)
        (chosen if hit and not bad else rest).append((bid, name, group))
    print("Bảng theo dõi (tự chọn theo tên): " + (", ".join(f"{b}·{n}" for b, n, _ in chosen) or "—"))
    print("Bảng bỏ qua: " + (", ".join(f"{b}·{n}" for b, n, _ in rest) or "—"))
    if not chosen:
        print(f"⚠️ không bảng nào khớp AUTO_BOARD_INCLUDE → tạm dùng bảng {FALLBACK_BOARD_IDS}")
        return list(FALLBACK_BOARD_IDS)
    return [b for b, _, _ in chosen]


def find_feed_lists(node, skip=()):
    """Tìm trong JSON mọi danh sách bài (phần tử có 'feedId', hoặc phần tử['feed'] có 'feedId').
    Trả {đường_dẫn: [dict bài]} — không phụ thuộc tên key nên chịu được việc Naver đổi cấu trúc nhẹ."""
    found = {}

    def walk(n, path, depth):
        if depth > 5:
            return
        if isinstance(n, dict):
            for k, v in n.items():
                if any(sk in str(k).lower() for sk in skip):
                    continue
                walk(v, path + [str(k)], depth + 1)
        elif isinstance(n, list):
            items = []
            for el in n:
                f = el.get("feed") if isinstance(el, dict) and isinstance(el.get("feed"), dict) else el
                if isinstance(f, dict) and f.get("feedId") is not None:
                    items.append(f)
            if items:
                found[".".join(path) or "(gốc)"] = items

    walk(node, [], 0)
    return found


def fetch_board_feeds(board_id):
    """Danh sách bài của 1 bảng, mới nhất trước.
    1) /community/lounge/{slug}/feed — chính endpoint trang board/{id}?order=new dùng
    2) /lounge/{slug}/new/popularFeeds/board — widget 'bài mới', chỉ gọi khi (1) lỗi hoặc rỗng"""
    merged, notes, answered = {}, [], False

    def take(groups, label, keep=None):
        total = 0
        for path, items in groups.items():
            if keep and not keep(path):
                continue
            for f in items:
                total += 1
                fid = str(f["feedId"])
                if fid not in merged:
                    merged[fid] = {"id": fid, "title": html.unescape(f.get("title") or "").strip(),
                                   "created": f.get("createdDate")}
        notes.append(f"{label}: {total} bài")

    try:
        c = api_get(f"/community/lounge/{LOUNGE}/feed",
                    {"boardId": board_id, "order": "NEW", "limit": 20, "offset": 0, "buffFilteringYN": "N"})
        take(find_feed_lists(c, skip=("popular",)), "feed")
        answered = True
    except ApiError as e:
        notes.append(f"feed: LỖI ({e})")
    if not merged:   # endpoint 1 lỗi hoặc rỗng → thử endpoint dự phòng (đỡ gọi thừa khi bot theo dõi nhiều bảng)
        try:
            c = api_get(f"/lounge/{LOUNGE}/new/popularFeeds/board", {"boardId": board_id, "limit": 20})
            take(find_feed_lists(c), "popularFeeds/board",
                 keep=lambda p: p.split(".")[-1].lower().startswith("recent") or "notice" in p.lower())
            answered = True
        except ApiError as e:
            notes.append(f"popularFeeds/board: LỖI ({e})")
    print("   endpoint → " + " · ".join(notes))
    if not answered:
        raise ApiError(f"bảng {board_id}: cả 2 endpoint danh sách đều lỗi")
    return sorted(merged.values(), key=lambda f: fid_key(f["id"]), reverse=True)


def fetch_detail(feed_id):
    c = api_get(f"/community/lounge/{LOUNGE}/feed/{feed_id}") or {}
    feed = c.get("feed") or {}
    return {
        "title": html.unescape(feed.get("title") or "").strip(),
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
def human_mb(n):
    return f"{n / MB:.1f} MB"


def media_kind(url, info=None):
    ct = ((info or {}).get("ctype") or "").lower()
    u = url.lower().split("?")[0]
    if "gif" in ct or u.endswith(".gif"):
        return "GIF"
    if ct.startswith("video") or u.endswith((".mp4", ".webm", ".mov")):
        return "Video"
    return "Ảnh"


def probe_media(urls):
    """Đo dung lượng từng file media bằng HEAD (rơi về GET Range 0-0 nếu HEAD không cho Content-Length).
    Trả {url: {"size": bytes|None, "ctype": str|None}}; không đo được → size None (vẫn nhúng bình thường)."""
    urls = list(dict.fromkeys(urls))[:PROBE_MAX]
    out = {u: {"size": None, "ctype": None} for u in urls}
    if not PROBE_MEDIA or not urls:
        return out
    hdr = {"Referer": SITE + "/", "User-Agent": UA, "Accept": "*/*"}

    def one(u):
        size, ctype = None, None
        try:
            r = requests.head(u, headers=hdr, timeout=5, allow_redirects=True)
            ctype = r.headers.get("Content-Type")
            cl = r.headers.get("Content-Length", "")
            if r.status_code == 200 and cl.isdigit():
                size = int(cl)
            else:
                g = requests.get(u, headers={**hdr, "Range": "bytes=0-0"}, timeout=5, stream=True, allow_redirects=True)
                try:
                    ctype = ctype or g.headers.get("Content-Type")
                    m = re.search(r"/(\d+)\s*$", g.headers.get("Content-Range", ""))
                    if m:
                        size = int(m.group(1))
                    elif g.status_code == 200 and g.headers.get("Content-Length", "").isdigit():
                        size = int(g.headers["Content-Length"])
                finally:
                    g.close()
        except requests.RequestException:
            pass
        return {"size": size, "ctype": ctype}

    ex = ThreadPoolExecutor(max_workers=8)
    futs = {ex.submit(one, u): u for u in urls}
    try:
        for f in as_completed(futs, timeout=45):
            out[futs[f]] = f.result()
    except Exception:   # quá hạn: các file chưa đo coi như không rõ dung lượng
        pass
    ex.shutdown(wait=False, cancel_futures=True)
    return out


def log_media(media):
    if not media:
        return
    known = [(u, i) for u, i in media.items() if i.get("size")]
    print(f"   🖼️ {len(media)} file media · đo được {len(known)} · tổng {human_mb(sum(i['size'] for _, i in known))}"
          f" (nhúng tối đa {MEDIA_MAX_MB_EACH:g} MB/file, {MEDIA_MAX_MB_PER_MSG:g} MB/tin)")
    for u, i in sorted(known, key=lambda x: -x[1]["size"])[:8]:
        print(f"      {human_mb(i['size']):>9} {media_kind(u, i):<5} {u[:95]}")


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


def blocks_to_units(blocks, media=None):
    """Khối → đơn vị đóng gói. Ảnh gom thành gallery (<=10 ảnh VÀ <= MEDIA_MAX_MB_PER_MSG); file nặng hơn
    MEDIA_MAX_MB_EACH không nhúng mà thành dòng link."""
    media = media or {}
    each, per_msg = MEDIA_MAX_MB_EACH * MB, MEDIA_MAX_MB_PER_MSG * MB
    units, imgs, links, wsum = [], [], [], 0

    def flush():
        nonlocal imgs, links, wsum
        if imgs:
            units.append(("gallery", imgs))
        if links:
            units.append(("text", "\n".join(links)))
        imgs, links, wsum = [], [], 0

    for b in blocks:
        if b[0] != "img":
            flush()
            if b[0] == "text":
                units.append(("text", b[1]))
            elif b[0] == "hr" and units and units[-1][0] != "hr":
                units.append(("hr", None))
            continue
        url = b[1]
        info = media.get(url) or {}
        sz = info.get("size") or 0
        if sz > each:
            if imgs:
                flush()
            links.append(f"🎞️ {media_kind(url, info)} · {human_mb(sz)} (quá nặng để nhúng) — [mở file]({url})")
            continue
        if links:
            flush()
        if imgs and (len(imgs) >= GALLERY_MAX or wsum + sz > per_msg):
            units.append(("gallery", imgs))
            imgs, wsum = [], 0
        imgs.append((url, sz))
        wsum += sz
    flush()
    while units and units[-1][0] == "hr":
        units.pop()
    return units


def short(s, n):
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def build_messages(post):
    """post = {id, title, epoch, url, blocks} → list payload (mỗi payload = 1 tin nhắn Discord)."""
    blocks = list(post["blocks"])
    media = post.get("media") or {}
    banner, banner_sz = None, 0
    if blocks and blocks[0][0] == "img":
        sz0 = (media.get(blocks[0][1]) or {}).get("size") or 0
        if sz0 <= MEDIA_MAX_MB_EACH * MB:       # banner quá nặng thì để thành link trong thân bài
            banner, banner_sz = blocks.pop(0)[1], sz0
    units = blocks_to_units(blocks, media)

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
        m = {"containers": [], "text": 0, "comps": 0, "body": None, "label": None, "first": first, "media": 0}
        if first:
            m["containers"].append(c_container(hdr_children))
            m["text"], m["comps"] = hdr_cost_text, hdr_cost_comp
            m["media"] = banner_sz
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
        need_media = sum(sz for _, sz in val) if kind == "gallery" else 0
        fits_comp = m["comps"] + comp_need(m) <= COMP_BUDGET
        fits_media = m["media"] == 0 or m["media"] + need_media <= MEDIA_MAX_MB_PER_MSG * MB
        room = budget(m) - m["text"]
        if fits_comp and fits_media and need_text <= room:
            body = ensure_body(m)
            body["components"].append(
                c_text(val) if kind == "text" else c_gallery([u for u, _ in val]) if kind == "gallery" else c_sep())
            m["comps"] += 1
            m["text"] += need_text
            m["media"] += need_media
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
def send(webhook, payload, sent=None):
    """Trả về 'ok' | 'transient' (lỗi tạm: 5xx/429/mạng) | 'permanent' (4xx: webhook sai/xoá, payload sai).
    sent: list tùy chọn — mỗi tin gửi OK được ghi {webhook, id, payload} để lát nữa kiểm tra media."""
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
            if sent is not None:
                try:
                    sent.append({"webhook": webhook, "id": r.json().get("id"), "payload": payload})
                except (ValueError, AttributeError):
                    pass
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


def payload_media_urls(payload):
    urls = []

    def walk(c):
        if c.get("type") == 12:
            urls.extend(it["media"]["url"] for it in c.get("items", []))
        for k in c.get("components") or []:
            walk(k)
    for c in payload.get("components") or []:
        walk(c)
    return urls


def has_media(payload):
    return bool(payload_media_urls(payload))


def strip_media(payload):
    """Bản không nhúng media: bỏ mọi gallery, thay bằng 1 dòng link [1](url) [2](url)… ở cuối (vừa ngân sách chữ)."""
    p = copy.deepcopy(payload)
    urls = payload_media_urls(payload)

    def walk(children):
        out = []
        for c in children:
            if c.get("type") == 12:
                continue
            if c.get("components") is not None and c.get("type") == 17:
                c["components"] = walk(c["components"])
                if not c["components"]:
                    continue
            out.append(c)
        return out

    p["components"] = walk(p["components"])
    if urls and p["components"]:
        used = check_limits(p)[1]["text"]
        room = 3800 - used - 120
        line, shown = "🖼️ Ảnh/GIF không nhúng được: ", 0
        for i, u in enumerate(urls, 1):
            piece = f"[{i}]({u}) "
            if ulen(line) + ulen(piece) > room:
                break
            line += piece
            shown += 1
        if shown < len(urls):
            line += f"… +{len(urls) - shown} file nữa (xem bằng nút Naver ở đầu bài)"
        if shown or shown < len(urls):
            p["components"][-1]["components"].append(c_text(line.strip()))
    return p


def collect_media_states(node, out):
    """Gom (url, loading_state) của mọi media item trong JSON tin nhắn Discord trả về."""
    if isinstance(node, dict):
        if "loading_state" in node and (node.get("url") or node.get("proxy_url")):
            out.append((node.get("url") or "", node.get("loading_state")))
        for v in node.values():
            collect_media_states(v, out)
    elif isinstance(node, list):
        for v in node:
            collect_media_states(v, out)


def verify_media(sent):
    """Đợi VERIFY_WAIT_SEC rồi hỏi lại Discord từng tin có media: item nào loading_state=3 (không tải được) thì đăng thêm
    1 tin liệt kê link trực tiếp. loading_state: 0 chưa rõ, 1 đang tải, 2 tải xong, 3 không tải được (theo Discord API)."""
    todo = [x for x in sent if x.get("id") and has_media(x["payload"])]
    if not todo:
        return 0
    print(f"🔎 đợi {VERIFY_WAIT_SEC:g}s rồi kiểm tra Discord đã tải được media của {len(todo)} tin chưa…")
    time.sleep(VERIFY_WAIT_SEC)
    hist, failed = {}, {}
    for x in todo:
        try:
            r = requests.get(f"{x['webhook']}/messages/{x['id']}", params={"with_components": "true"}, timeout=15)
            if r.status_code != 200:
                continue
            states = []
            collect_media_states(r.json(), states)
        except (requests.RequestException, ValueError):
            continue
        for url, st in states:
            hist[st] = hist.get(st, 0) + 1
            if st == 3 and url not in failed.setdefault(x["webhook"], []):
                failed[x["webhook"]].append(url)
    print(f"   trạng thái media (loading_state → số file): {hist or 'Discord không trả loading_state'}")
    nfail = 0
    for wh, urls in failed.items():
        nfail += len(urls)
        lines, used = [], 0
        for i, u in enumerate(urls, 1):
            piece = f"[{i}]({u})"
            if used + ulen(piece) > 3000:
                lines.append(f"… +{len(urls) - i + 1} file nữa")
                break
            lines.append(piece)
            used += ulen(piece) + 3
        text = f"⚠️ Discord không tải được {len(urls)} file media ở tin trên (thường do quá nặng) — mở trực tiếp:\n" + " · ".join(lines)
        res = send(wh, {"username": BOT_NAME, "flags": 1 << 15, "components": [c_container([c_text(text)])],
                        "allowed_mentions": {"parse": []}})
        print(f"   ⚠️ {len(urls)} file lỗi → tin bổ sung link: {res}")
    return nfail


# ─────────────────────────── TRẠNG THÁI ───────────────────────────
def load_state():
    if not os.path.exists(STATE_FILE):
        print(f"ℹ️ chưa có {STATE_FILE} (lần đầu — hoặc file không được commit lại sau lần chạy trước)")
        return {"boards": {}}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        if isinstance(s, dict) and isinstance(s.get("boards"), dict):
            return s
    except ValueError:
        pass
    print(f"⚠️ {STATE_FILE} hỏng/sai định dạng → coi như chưa có")
    return {"boards": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def is_skipped_title(title):
    return any(re.search(p, title or "", re.I) for p in SKIP_TITLE_PATTERNS)


def fmt_created(v):
    ep = parse_created(v)
    return datetime.fromtimestamp(ep, KST).strftime("%Y-%m-%d %H:%M KST") if ep else "?"


# ─────────────────────────── LUỒNG CHÍNH ───────────────────────────
def prepare_post(feed_id):
    d = fetch_detail(feed_id)
    try:
        blocks = contents_to_blocks(d["contents"])
    except Exception:
        traceback.print_exc()
        print(f"   ⚠️ đọc nội dung bài {feed_id} lỗi → chỉ gửi tiêu đề + nút link")
        blocks = []
    media = probe_media([b[1] for b in blocks if b[0] == "img"])
    log_media(media)
    return {
        "id": str(feed_id), "title": d["title"], "epoch": parse_created(d["created"]),
        "url": f"{SITE}/lounge/{LOUNGE}/board/detail/{feed_id}", "blocks": blocks, "media": media,
        "author": d["author"], "raw_len": len(d["contents"]), "feed_keys": d["feed_keys"],
        "raw_head": d["contents"][:300],
    }


def build_for(meta, feed_id):
    """Lấy + dựng 1 bài. Lỗi API → ApiError (để lần sau thử lại). Lỗi dựng nội dung → bản rút gọn (tiêu đề + nút)."""
    post = prepare_post(feed_id)
    if not post["title"]:
        post["title"] = meta.get("title", "")
    if not post["epoch"]:
        post["epoch"] = parse_created(meta.get("created"))
    try:
        payloads = build_messages(post)
        bad = [pr for p in payloads for pr in check_limits(p)[0]]
        if bad:
            raise ValueError(f"payload vi phạm giới hạn Discord: {bad}")
        return post, payloads
    except Exception:
        traceback.print_exc()
        print(f"   ⚠️ dựng nội dung bài {feed_id} lỗi → gửi bản rút gọn (tiêu đề + nút link)")
        post = dict(post, blocks=[])
        return post, build_messages(post)


def deliver(post, payloads, webhooks, dry, sent=None):
    """Gửi 1 bài. Trả về 'done' | 'done_with_errors' (đã xử lý xong) hoặc 'retry' (để lần sau thử lại).
    Tin bị Discord từ chối (4xx): thử lại bản KHÔNG nhúng media (link thay ảnh); tin đầu vẫn hỏng thì gửi bản rút gọn.
    Một tin hỏng không làm bỏ các tin sau."""
    print(f"📰 [{post['id']}] {post['title']}")
    print(f"   tác giả={post['author']} · khối={len(post['blocks'])} · {len(payloads)} tin nhắn")
    if not post["blocks"]:
        print(f"   ⚠️ không có nội dung trích được. Đầu contents: {post['raw_head']!r}")
    media = post.get("media") or {}
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
        urls = payload_media_urls(p)
        mb = sum((media.get(u) or {}).get("size") or 0 for u in urls)
        print(f"   tin {i}/{len(payloads)}: {st['comps']} component · {st['text']} ký tự · {len(urls)} ảnh"
              + (f" · {human_mb(mb)} media" if mb else "") + (f" · ⚠️ {problems}" if problems else ""))
        if dry:
            for t in texts:
                print("      │ " + t[:400].replace("\n", "\n      │ ") + (" …" if len(t) > 400 else ""))
    if dry:
        return "done"
    results = []
    for wi, wh in enumerate(webhooks, 1):
        oks, last_bad = 0, None
        for pi, p in enumerate(payloads):
            res = send(wh, p, sent)
            if res == "permanent" and has_media(p):
                print(f"   ↪ tin {pi + 1}: Discord từ chối bản có media → gửi lại bản không nhúng (link thay ảnh)")
                res = send(wh, strip_media(p), sent)
            if res == "permanent" and pi == 0:
                print("   ↪ tin đầu vẫn bị từ chối → thử bản rút gọn (tiêu đề + nút link)")
                res = send(wh, build_messages(dict(post, blocks=[]))[0], sent)
            if res == "ok":
                oks += 1
                time.sleep(0.8)
            else:
                last_bad = res
        status = "ok" if oks == len(payloads) else ("partial" if oks else last_bad)
        print(f"   → webhook #{wi}: {status} ({oks}/{len(payloads)} tin)")
        results.append(status)
    if any(r in ("ok", "partial") for r in results) or all(r == "permanent" for r in results):
        return "done" if all(r == "ok" for r in results) else "done_with_errors"
    return "retry"


def main():
    dry = env_flag("NAVER_DRY_RUN")
    raw_wh = os.environ.get("NAVER_DISCORD_WEBHOOK") or os.environ.get("DISCORD_WEBHOOK") or ""
    webhooks = [w.strip() for w in raw_wh.split(",") if w.strip()]
    which = "NAVER_DISCORD_WEBHOOK" if os.environ.get("NAVER_DISCORD_WEBHOOK") else "DISCORD_WEBHOOK"
    print(f"Naver lounge '{LOUNGE}' · {len(webhooks)} webhook (lấy từ {which})"
          + (" · DRY-RUN (không gửi, không ghi state)" if dry else ""))
    if not webhooks and not dry:
        print("❌ Thiếu DISCORD_WEBHOOK / NAVER_DISCORD_WEBHOOK")
        return 1

    if env_flag("NAVER_LIST_BOARDS"):
        for bid, name, group in fetch_boards():
            print(f"  {bid:>6} | {name} | nhóm: {group}")
        return 0

    board_ids = resolve_board_ids()
    state = load_state()
    seen_before = set()          # id đã thấy ở BẤT KỲ bảng nào (chốt từ state trước lần chạy này)
    for bs in state["boards"].values():
        seen_before |= set(bs.get("seen", []))
    changed, errors, posted, seeded = False, 0, 0, 0
    sent_log = []
    cand, listed, any_feeds, empty = {}, {}, False, []

    # ── Giai đoạn 1: đọc danh sách từng bảng, gom các bài chưa thấy (1 bài hiện ở nhiều bảng chỉ tính 1 lần)
    for bid in board_ids:
        print(f"── bảng {bid}")
        try:
            feeds = fetch_board_feeds(bid)
        except ApiError as e:
            print(f"❌ {e}")
            errors += 1
            continue
        if not feeds:
            print(f"⚠️ bảng {bid}: danh sách trống")
            empty.append(bid)
            continue
        any_feeds = True
        newest = feeds[0]
        print(f"   mới nhất trong danh sách: {newest['id']} · {fmt_created(newest['created'])} · "
              f"{short(newest['title'], 40)!r} (tổng {len(feeds)} bài)")
        ids = [f["id"] for f in feeds]
        bs = state["boards"].get(bid)
        if bs is None:
            print(f"🌱 bảng {bid}: lần đầu → ghi nhớ {len(ids)} bài hiện có, KHÔNG đăng. "
                  "(Nếu dòng này lặp lại ở MỌI lần chạy theo lịch = naver_state.json không được commit lại → xem bước Save last_id)")
            state["boards"][bid] = {"seen": ids[:KEEP_SEEN]}
            changed = True
            seeded += 1
            continue
        listed[bid] = ids
        for f in feeds:
            if f["id"] not in seen_before:
                cand.setdefault(f["id"], dict(f, boards=[]))["boards"].append(bid)
    if any_feeds:   # API đang trả bài bình thường → bảng trống là trống thật; ghi nhận để bài ĐẦU TIÊN của bảng đó vẫn được đăng
        for bid in empty:
            if bid not in state["boards"]:
                state["boards"][bid] = {"seen": []}
                changed = True
                seeded += 1
                print(f"🌱 bảng {bid}: đang trống → ghi nhận bảng rỗng (bài đầu tiên của bảng này sẽ được đăng)")
    if board_ids and not any_feeds and not errors:
        print("❌ không thấy bài nào ở bảng nào → sai id bảng, hoặc Naver đổi API "
              "(chạy với NAVER_LIST_BOARDS=1 để xem id các bảng)")
        errors += 1

    # ── Giai đoạn 2: lọc, giới hạn số bài mỗi lần, gửi
    if listed:
        print(f"state: {len(seen_before)} id đã thấy (mọi bảng), lớn nhất "
              f"{max(seen_before, key=fid_key) if seen_before else '—'}")
        unseen = sorted(cand.values(), key=lambda f: fid_key(f["id"]))
        now = time.time()
        stale = [f for f in unseen if (parse_created(f["created"]) or now) < now - MAX_POST_AGE_HOURS * 3600]
        fresh = [f for f in unseen if f not in stale]
        print(f"{len(unseen)} bài chưa thấy → {len(fresh)} mới, {len(stale)} quá cũ (>{MAX_POST_AGE_HOURS:g}h)")
        if stale:
            print(f"   ⏭️ chỉ ghi nhớ, không đăng: {[f['id'] for f in stale][:10]}")
        filtered = [f for f in fresh if is_skipped_title(f["title"])]
        fresh = [f for f in fresh if f not in filtered]
        if filtered:
            print(f"   ⏭️ bỏ qua theo bộ lọc tiêu đề: {[(f['id'], short(f['title'], 30)) for f in filtered]}")
        if len(fresh) > MAX_POSTS_PER_RUN:
            print(f"⚠️ {len(fresh)} bài mới > {MAX_POSTS_PER_RUN} — chỉ đăng {MAX_POSTS_PER_RUN} bài mới nhất")
        handled = [f["id"] for f in stale] + [f["id"] for f in filtered]
        skipped = [f["id"] for f in fresh[:-MAX_POSTS_PER_RUN]] if len(fresh) > MAX_POSTS_PER_RUN else []
        for f in fresh[-MAX_POSTS_PER_RUN:]:
            try:
                post, payloads = build_for(f, f["id"])
            except ApiError as e:
                print(f"❌ bài {f['id']}: {e} → để lần sau thử lại")
                errors += 1
                continue
            if is_skipped_title(post["title"]):
                print(f"   ⏭️ bài {f['id']} khớp bộ lọc tiêu đề ({short(post['title'], 30)!r}) → bỏ qua")
                handled.append(f["id"])
                continue
            outcome = deliver(post, payloads, webhooks, dry, sent_log)
            if outcome == "retry":
                errors += 1
                print(f"   ↻ bài {f['id']} chưa gửi được ở webhook nào → lần sau thử lại")
            else:
                handled.append(f["id"])
                posted += 1
                if outcome == "done_with_errors":
                    errors += 1
        newly = set(handled) | set(skipped)
        if newly and not dry:
            for bid, ids in listed.items():
                add = newly & set(ids)
                if add:
                    bs = state["boards"][bid]
                    bs["seen"] = sorted(set(bs.get("seen", [])) | add, key=fid_key, reverse=True)[:KEEP_SEEN]
                    changed = True

    for fid in [x.strip() for x in os.environ.get("NAVER_FORCE_FEED_ID", "").split(",") if x.strip()]:
        print(f"🔧 ép xử lý bài {fid}")
        try:
            post, payloads = build_for({}, fid)
        except ApiError as e:
            print(f"❌ bài {fid}: {e}")
            errors += 1
            continue
        deliver(post, payloads, webhooks, dry, sent_log)

    if sent_log and not dry and VERIFY_MEDIA:
        try:
            verify_media(sent_log)
        except Exception:
            traceback.print_exc()
    if changed and not dry:
        save_state(state)
        print("💾 đã lưu naver_state.json")
    print(f"Tổng kết Naver: {'sẽ đăng' if dry else 'đã đăng'} {posted} bài · ghi nhớ lần đầu {seeded} bảng · lỗi {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
