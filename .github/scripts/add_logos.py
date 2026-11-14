#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
add_logos.py —— 为 CHINA-IPTV 直播源批量补全频道图标，生成 M3U / TXT / EPG 产物。

只依赖 Python 标准库，可在 GitHub Actions 或本地直接运行。

输入：
  Unicast/<省>/<运营商>.txt      rtp/rtsp/udp 组播源
  Multicast/<省>/<运营商>.txt    （上游目录名如此，内容为组播）
  TV/live.txt                    上游合并的多源清单
  logo/*.png                     频道图标库
  TV/channel_mapping.txt         频道别名 -> 标准名
  e.xml                          节目单（用于 tvg-id 关联）

输出（默认 dist/）：
  iptv.m3u            推荐使用：全量源，每频道最多 --max-urls 个地址
  iptv-full.m3u       全量源，不做数量限制
  iptv.txt            DIYP / TVBox 风格的 txt（分类 + 名称,地址）
  unicast/<省>/<运营商>.m3u
  multicast/<省>/<运营商>.m3u
  logo-coverage.txt   图标覆盖率统计 + 未匹配频道清单（方便补别名）

用法：
  python3 add_logos.py --root . --out dist \
      --logo-base https://cdn.jsdelivr.net/gh/owner/repo@main/logo
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path
from urllib.parse import quote

# --------------------------------------------------------------------------- #
# 频道名归一化 / 匹配
# --------------------------------------------------------------------------- #

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")

PUNCT_RE = re.compile(r"[()（）\[\]【】{}<>《》\s_\-‑–—·•.,，、/\\|:：;；\"'“”‘’!！?？*#]+")

QUALITY_SUFFIX = (
    "高清测试", "超高清", "高清", "超清", "标清", "普清", "蓝光", "原画",
    "4K", "8K", "2K", "1080P", "720P", "HD", "SD",
)
TRAILING_WORD = ("频道", "电视台", "台", "卫视")
BROADCASTER_PREFIX = (
    "CCTV", "CETV", "CGTN", "IPTV", "NEWTV", "BESTV", "GITV", "CIBN", "SITV", "SCTV",
    # 图标库里常见的品牌 / 版权方前缀：`CHC家庭影院`、`IHOT爱体育`、`百视通` 等
    "IHOT", "CHC", "CBN", "百视通", "百事通", "央视", "中央新影", "爱上", "移动",
)
PROVINCE_PREFIX = (
    "黑龙江", "内蒙古", "新疆", "西藏", "宁夏", "广西", "香港", "澳门", "台湾",
    "北京", "天津", "河北", "山西", "辽宁", "吉林", "上海", "江苏", "浙江", "安徽",
    "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "重庆", "四川",
    "贵州", "云南", "陕西", "甘肃", "青海", "中国",
)


def base_key(s: str) -> str:
    """去掉全部标点/空白并转大写，用于宽松比较。"""
    return PUNCT_RE.sub("", s).upper()


def _strip_quality(s: str) -> str:
    out = s.strip()
    changed = True
    while changed:
        changed = False
        for q in QUALITY_SUFFIX:
            if len(out) > len(q) and out.upper().endswith(q.upper()):
                out = out[: -len(q)]
                changed = True
        out = out.strip(" -_·.()（）[]【】")
    return out


def _strip_trailing_word(s: str) -> str:
    out = s.strip()
    changed = True
    while changed:
        changed = False
        for w in TRAILING_WORD:
            if len(out) > len(w) and out.endswith(w):
                out = out[: -len(w)]
                changed = True
        out = out.strip(" -_·.()（）[]【】")
    return out


def _strip_broadcaster(s: str) -> str:
    out = s.strip()
    for p in BROADCASTER_PREFIX:
        if out.upper().startswith(p):
            rest = out[len(p):].lstrip(" -_·:：")
            if rest:
                return rest
    return ""


def _strip_province(s: str) -> str:
    out = s.strip()
    for p in PROVINCE_PREFIX:
        if out.startswith(p):
            rest = out[len(p):].lstrip(" -_·:：")
            if rest:
                return rest
    return ""


def _strip_plus(s: str) -> str:
    """`IPTV5＋`（全角）/ `IPTV5+` 去掉加号后彼此可比。"""
    out = s.replace("＋", "+").replace("+", "")
    return out if out != s else ""


TRANSFORMS = (_strip_quality, _strip_trailing_word, _strip_broadcaster, _strip_province, _strip_plus)


def candidates(name: str, depth: int = 3) -> list[str]:
    """生成候选名（最多 depth 次变换的闭包），保持优先级顺序。"""
    seen = {name}
    ordered = [name]
    frontier = [name]
    for _ in range(depth):
        nxt: list[str] = []
        for cur in frontier:
            for fn in TRANSFORMS:
                val = fn(cur)
                if not val or val in seen or len(val) < 2 or val.isdigit():
                    continue
                seen.add(val)
                ordered.append(val)
                nxt.append(val)
        frontier = nxt
        if not frontier:
            break
    return ordered


class NameMatcher:
    """在 {名称: 值} 上做精确 / 忽略大小写 / 宽松标点 三级匹配。

    derive=True 时，额外把每个名称做候选变换后也登记为键（如 logo 库里的
    `NEWTV军旅剧场` 可以被 `军旅剧场` 命中）；多个名称派生出同一个键时视为歧义而丢弃，
    避免 `湖南电影` / `湖南电视剧` 之类互相抢匹配。
    """

    def __init__(self, items: dict[str, str], derive: bool = False):
        self.exact: dict[str, str] = {}
        self.lower: dict[str, str] = {}
        self.base: dict[str, str] = {}
        for key, value in items.items():
            self.exact.setdefault(key, value)
            self.lower.setdefault(key.lower(), value)
            self.base.setdefault(base_key(key), value)
        self.derived: dict[str, str] = {}
        self.affix: dict[str, str] = {}
        if derive:
            buckets: dict[str, set[str]] = {}
            for key, value in items.items():
                for cand in candidates(key):
                    buckets.setdefault(base_key(cand), set()).add(value)
            for bk, values in buckets.items():
                if len(values) == 1 and bk not in self.base:
                    self.derived[bk] = next(iter(values))

            # 前后缀包含：`家庭影院` 命中 `CHC家庭影院`、`IPTV5` 命中 `IPTV5+`。
            # 把每个图标名的所有前后缀（长度 >= 4）登记为键，只接受唯一命中，避免过度泛化。
            affix_buckets: dict[str, set[str]] = {}
            for bk, value in self.base.items():
                if len(bk) < 4:
                    continue
                for size in range(4, len(bk) + 1):
                    for sub in (bk[:size], bk[-size:]):
                        if sub != bk:
                            affix_buckets.setdefault(sub, set()).add(value)
            for sub, values in affix_buckets.items():
                if len(values) == 1 and sub not in self.base and sub not in self.derived:
                    self.affix[sub] = next(iter(values))

    def _hit(self, key: str) -> str | None:
        if key in self.exact:
            return self.exact[key]
        low = key.lower()
        if low in self.lower:
            return self.lower[low]
        bk = base_key(key)
        if bk:
            if bk in self.base:
                return self.base[bk]
            if bk in self.derived:
                return self.derived[bk]
            if bk in self.affix:
                return self.affix[bk]
        if low:
            if low in self.derived:
                return self.derived[low]
            if low in self.affix:
                return self.affix[low]
        return None

    def lookup(self, name: str) -> str | None:
        for cand in candidates(name):
            hit = self._hit(cand)
            if hit is not None:
                return hit
        return None


# --------------------------------------------------------------------------- #
# 数据读取
# --------------------------------------------------------------------------- #

def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except FileNotFoundError:
        return ""


def parse_txt_playlist(text: str) -> list[tuple[str, str, str]]:
    """解析 txt 直播源，返回 [(频道名, 地址, 分类)]。"""
    rows: list[tuple[str, str, str]] = []
    genre = "未分类"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("#genre#"):
            head = line.split(",", 1)[0].strip()
            if head:
                genre = head
            continue
        if line.startswith("【") or line.startswith("//"):
            continue
        if "," not in line:
            continue
        name, url = line.split(",", 1)
        name = name.strip()
        url = url.strip()
        if not name or not url:
            continue
        rows.append((name, url, genre))
    return rows


def parse_m3u_playlist(text: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    pending: tuple[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            group = ""
            m = re.search(r'group-title="([^"]*)"', line)
            if m:
                group = m.group(1)
            name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            m = re.search(r'tvg-name="([^"]*)"', line)
            if m and m.group(1).strip():
                name = m.group(1).strip()
            pending = (name, group)
            continue
        if line.startswith("#"):
            continue
        if pending is not None:
            name, group = pending
            if name and line:
                rows.append((name, line, group or "未分类"))
            pending = None
    return rows


def load_mapping(path: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or "," not in line:
            continue
        old, new = line.split(",", 1)
        old, new = old.strip(), new.strip()
        if old and new:
            mapping[old] = new
            mapping.setdefault(base_key(old), new)
    return mapping


def load_aliases(path: Path) -> dict[str, str]:
    """别名文件：`原频道名,logo文件名(不含 .png)`，允许 # 注释。"""
    aliases: dict[str, str] = {}
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "," not in line:
            continue
        src, dst = line.split(",", 1)
        src, dst = src.strip(), dst.strip()
        if src and dst:
            aliases[src] = dst
    return aliases


def load_epg_index(path: Path) -> NameMatcher | None:
    """从 e.xml 提取 display-name -> channel id 的映射。"""
    text = read_text(path)
    if not text:
        return None
    items: dict[str, str] = {}
    pattern = re.compile(
        r'<channel\s+id="([^"]+)"\s*>\s*<display-name[^>]*>([^<]+)</display-name>',
        re.S,
    )
    for m in pattern.finditer(text):
        cid, name = m.group(1).strip(), m.group(2).strip()
        if name:
            items.setdefault(name, cid)
    return NameMatcher(items) if items else None


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #

def write_if_changed(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except UnicodeDecodeError:
            pass
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def m3u_escape(value: str) -> str:
    return value.replace('"', "'").replace("\r", " ").replace("\n", " ")


class Enricher:
    """给频道名补全 logo / tvg-id。"""

    def __init__(
        self,
        logo_matcher: NameMatcher | None,
        epg_matcher: NameMatcher | None,
        mapping: dict[str, str],
        aliases: dict[str, str],
        logo_base: str,
        logo_files: dict[str, str] | None = None,
    ):
        self.logo_matcher = logo_matcher
        self.epg_matcher = epg_matcher
        self.mapping = mapping
        self.aliases = aliases
        self.alias_matcher = NameMatcher({k: v for k, v in aliases.items()}) if aliases else None
        self.logo_base = logo_base.rstrip("/")
        # 图标名（不含扩展名）-> 真实文件名，供别名表把 `CETV1` 补成 `CETV1.png`
        self._by_stem: dict[str, str] = dict(logo_files or {})
        self._logo_cache: dict[str, str | None] = {}
        self._epg_cache: dict[str, str | None] = {}

    def _complete_name(self, stem: str) -> str:
        """别名表写的是不含扩展名的文件名，补成真实文件名；找不到就原样返回。"""
        if stem in self._by_stem:
            return self._by_stem[stem]
        low = stem.lower()
        for key, filename in self._by_stem.items():
            if key.lower() == low:
                return filename
        return stem

    def _mapped_names(self, name: str) -> list[str]:
        out = [name]
        mapped = self.mapping.get(name) or self.mapping.get(base_key(name))
        if mapped and mapped != name:
            out.append(mapped)
        return out

    def logo_file(self, name: str) -> str | None:
        if name in self._logo_cache:
            return self._logo_cache[name]
        found: str | None = None
        if self.alias_matcher is not None:
            found = self.alias_matcher.lookup(name)
            # 别名表里写的是"不含扩展名的文件名"，这里补回真实文件名
            if found is not None and not Path(found).suffix:
                found = self._complete_name(found)
        if found is None and self.logo_matcher is not None:
            for candidate_name in self._mapped_names(name):
                found = self.logo_matcher.lookup(candidate_name)
                if found is not None:
                    break
        self._logo_cache[name] = found
        return found

    def logo_url(self, name: str) -> str | None:
        filename = self.logo_file(name)
        if not filename:
            return None
        return f"{self.logo_base}/{quote(filename)}"

    def tvg_id(self, name: str) -> str | None:
        if name in self._epg_cache:
            return self._epg_cache[name]
        found: str | None = None
        if self.epg_matcher is not None:
            for candidate_name in self._mapped_names(name):
                found = self.epg_matcher.lookup(candidate_name)
                if found is not None:
                    break
        self._epg_cache[name] = found
        return found


def build_m3u(
    rows: list[tuple[str, str, str]],
    enricher: Enricher,
    max_urls: int = 0,
    header_extra: str = "",
) -> str:
    lines = ["#EXTM3U"]
    if header_extra:
        lines.append(header_extra)

    seen_urls: set[str] = set()
    per_channel: Counter = Counter()
    for name, url, genre in rows:
        if url in seen_urls:
            continue
        if max_urls and per_channel[name] >= max_urls:
            continue
        seen_urls.add(url)
        per_channel[name] += 1

        attrs = [f'tvg-name="{m3u_escape(name)}"', f'group-title="{m3u_escape(genre)}"']
        epg_id = enricher.tvg_id(name)
        if epg_id:
            attrs.insert(0, f'tvg-id="{m3u_escape(epg_id)}"')
        logo = enricher.logo_url(name)
        if logo:
            attrs.append(f'tvg-logo="{logo}"')
        lines.append(f"#EXTINF:-1 {' '.join(attrs)},{m3u_escape(name)}")
        lines.append(url)
    return "\n".join(lines) + "\n"


def build_txt(rows: list[tuple[str, str, str]]) -> str:
    lines: list[str] = []
    current = None
    seen_urls: set[str] = set()
    for name, url, genre in rows:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        if genre != current:
            if current is not None:
                lines.append("")
            lines.append(f"{genre},#genre#")
            current = genre
        lines.append(f"{name},{url}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="为直播源补充频道图标并生成产物")
    parser.add_argument("--root", default=".", help="仓库根目录")
    parser.add_argument("--out", default="dist", help="产物输出目录")
    parser.add_argument(
        "--logo-base",
        default="https://cdn.jsdelivr.net/gh/xisohi/CHINA-IPTV@main/logo",
        help="图标 URL 前缀（不含结尾斜杠）",
    )
    parser.add_argument("--max-urls", type=int, default=5, help="iptv.m3u 中每个频道最多保留几个地址，0 表示不限")
    parser.add_argument("--epg-icons", action="store_true", help="同时生成带 <icon> 的 dist/epg.xml")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    out = (root / args.out).resolve()

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, flush=True)

    # --- 图标库 ------------------------------------------------------------
    # 图标本体不随仓库分发（30MB 的 PNG 没必要进 git），只需要文件名清单：
    #   1) 本地存在 logo/ 目录时直接读目录（开发调试用）；
    #   2) 否则读 .github/logo-index.txt（由 sync.yml 从上游生成）。
    logo_items: dict[str, str] = {}
    logo_dir = root / "logo"
    if logo_dir.is_dir():
        for p in sorted(logo_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_EXT:
                logo_items[p.stem] = p.name
    if not logo_items:
        index_file = root / ".github" / "logo-index.txt"
        for raw in read_text(index_file).splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name = Path(line).name
            if Path(name).suffix.lower() in IMAGE_EXT:
                logo_items.setdefault(Path(name).stem, name)
    logo_matcher = NameMatcher(logo_items, derive=True) if logo_items else None
    log(f"图标库: {len(logo_items)} 个图标")

    mapping = load_mapping(root / "TV" / "channel_mapping.txt")
    log(f"频道别名表: {len(mapping)} 条")

    aliases = load_aliases(root / ".github" / "logos-aliases.txt")
    log(f"自定义图标别名: {len(aliases)} 条")

    epg_matcher = load_epg_index(root / "e.xml")
    log(f"节目单频道: {'已加载' if epg_matcher else '无'}")

    enricher = Enricher(logo_matcher, epg_matcher, mapping, aliases, args.logo_base, logo_items)

    # --- 采集源 -----------------------------------------------------------
    groups: "OrderedDict[tuple[str, str, str], list[tuple[str, str, str]]]" = OrderedDict()

    def add_file(kind: str, province: str, isp: str, path: Path) -> None:
        text = read_text(path)
        if not text:
            return
        rows = parse_m3u_playlist(text) if path.suffix.lower() == ".m3u" else parse_txt_playlist(text)
        if rows:
            groups[(kind, province, isp)] = rows

    for kind in ("unicast", "multicast"):
        base = root / kind.capitalize()
        if not base.is_dir():
            continue
        for province_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            for playlist in sorted(province_dir.glob("*.txt")):
                add_file(kind, province_dir.name, playlist.stem, playlist)

    live_txt = root / "TV" / "live.txt"
    if live_txt.is_file():
        rows = parse_txt_playlist(read_text(live_txt))
        if rows:
            groups[("merged", "tv", "live")] = rows

    all_rows: list[tuple[str, str, str]] = []
    for rows in groups.values():
        all_rows.extend(rows)
    log(f"源文件: {len(groups)} 个，频道行: {len(all_rows)}")

    if not all_rows:
        print("错误: 没有解析到任何频道，退出", file=sys.stderr)
        return 1

    # --- 生成产物 ---------------------------------------------------------
    banner = "# 由 zzzzzhy/CHINA-IPTV 自动生成：同步上游 + 补全频道图标"
    written = 0

    if write_if_changed(out / "iptv.m3u", build_m3u(all_rows, enricher, args.max_urls, banner)):
        written += 1
    if write_if_changed(out / "iptv-full.m3u", build_m3u(all_rows, enricher, 0, banner)):
        written += 1
    if write_if_changed(out / "iptv.txt", build_txt(all_rows)):
        written += 1

    for kind in ("unicast", "multicast"):
        kind_rows: list[tuple[str, str, str]] = []
        for (gkind, province, isp), rows in groups.items():
            if gkind != kind:
                continue
            kind_rows.extend(rows)
            if write_if_changed(out / kind / province / f"{isp}.m3u", build_m3u(rows, enricher, 0, banner)):
                written += 1
        if kind_rows and write_if_changed(out / f"{kind}.m3u", build_m3u(kind_rows, enricher, args.max_urls, banner)):
            written += 1

    # --- 覆盖率报告 -------------------------------------------------------
    total = len(all_rows)
    unique_names = Counter(name for name, _, _ in all_rows)
    matched_rows = sum(c for name, c in unique_names.items() if enricher.logo_file(name))
    matched_unique = sum(1 for name in unique_names if enricher.logo_file(name))
    missing = [(name, c) for name, c in unique_names.items() if not enricher.logo_file(name)]
    missing.sort(key=lambda x: (-x[1], x[0]))

    report = [
        f"总频道行数: {total}",
        f"有图标行数: {matched_rows} ({matched_rows / total:.1%})",
        f"唯一频道名: {len(unique_names)}",
        f"有图标唯一名: {matched_unique} ({matched_unique / len(unique_names):.1%})",
        f"图标库数量: {len(logo_items)}",
        f"图标 URL 前缀: {args.logo_base}",
        "",
        "未匹配频道（按出现次数排序，前 200 条；可在 .github/logos-aliases.txt 中手工补别名）:",
    ]
    report += [f"  {c:>5}  {name}" for name, c in missing[:200]]
    if write_if_changed(out / "logo-coverage.txt", "\n".join(report) + "\n"):
        written += 1
    log(f"图标覆盖: {matched_rows}/{total} 行 ({matched_rows / total:.1%})，唯一名 {matched_unique}/{len(unique_names)}")

    # --- 可选：带图标的 EPG ------------------------------------------------
    if args.epg_icons:
        epg_text = read_text(root / "e.xml")
        if epg_text:

            def inject(match: re.Match) -> str:
                block = match.group(0)
                if "<icon" in block:
                    return block
                name_match = re.search(r"<display-name[^>]*>([^<]+)</display-name>", block)
                if not name_match:
                    return block
                logo = enricher.logo_url(name_match.group(1).strip())
                if not logo:
                    return block
                return block.replace(
                    name_match.group(0),
                    f'{name_match.group(0)}\n<icon src="{logo}"/>',
                    1,
                )

            new_epg = re.sub(r"<channel\s+id=\"[^\"]+\"\s*>.*?</channel>", inject, epg_text, flags=re.S)
            if write_if_changed(out / "epg.xml", new_epg):
                written += 1
            log("已生成带图标的 dist/epg.xml")

    log(f"产物目录: {out}（本次写入 {written} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
