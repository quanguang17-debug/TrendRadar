#!/usr/bin/env python3
# coding: utf-8
"""每日抓取彭博社 RSS 文章（标题、正文、发布时间）。

说明：
1. 通过 Bloomberg RSS 获取当天文章链接；
2. 逐篇抓取网页并提取正文；
3. 将结果保存为 JSON 文件，便于后续入库/分析。

用法示例：
    python scripts_bloomberg_daily.py \
      --feed https://feeds.bloomberg.com/technology/news.rss \
      --timezone Asia/Shanghai \
      --window-start-hour 8 \
      --out data/bloomberg
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import pytz
import requests

DEFAULT_FEED = "https://feeds.bloomberg.com/technology/news.rss"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class BloombergArticle:
    title: str
    content: str
    published_at: str
    url: str


def fetch_rss(feed_url: str, timeout: int = 20) -> ET.Element:
    resp = requests.get(
        feed_url,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    return ET.fromstring(resp.content)


def parse_rss_items(root: ET.Element) -> List[dict]:
    channel = root.find("channel")
    if channel is None:
        return []

    items: List[dict] = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        if title and link:
            items.append({"title": title, "link": link, "pub_date": pub_date})
    return items


def parse_pub_time(pub_date: str, timezone: str) -> str:
    if not pub_date:
        return ""
    dt = parsedate_to_datetime(pub_date)
    if dt.tzinfo is None:
        dt = pytz.UTC.localize(dt)
    local_dt = dt.astimezone(pytz.timezone(timezone))
    return local_dt.isoformat()


def extract_article_body(html: str) -> str:
    """优先抓 JSON-LD 中的 articleBody，失败后回退到正文段落拼接。"""
    # 方案 1：JSON-LD
    json_ld_matches = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        flags=re.S | re.I,
    )
    for raw_json in json_ld_matches:
        cleaned = raw_json.strip()
        if not cleaned:
            continue
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            continue

        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            article_body = node.get("articleBody")
            if isinstance(article_body, str) and article_body.strip():
                return normalize_whitespace(article_body)

    # 方案 2：正文容器内的 <p>
    candidates = re.findall(
        r"<p[^>]*>(.*?)</p>",
        html,
        flags=re.S | re.I,
    )
    paragraphs = []
    for p in candidates:
        text = strip_html_tags(p)
        text = normalize_whitespace(text)
        if len(text) >= 30:
            paragraphs.append(text)

    return "\n".join(paragraphs)


def strip_html_tags(raw: str) -> str:
    no_script = re.sub(r"<script[\s\S]*?</script>", "", raw, flags=re.I)
    no_style = re.sub(r"<style[\s\S]*?</style>", "", no_script, flags=re.I)
    no_tags = re.sub(r"<[^>]+>", "", no_style)
    return no_tags


def normalize_whitespace(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_article_content(url: str, timeout: int = 20) -> str:
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return extract_article_body(resp.text)


def is_same_day(local_iso: str, timezone: str, target_date: datetime) -> bool:
    if not local_iso:
        return False
    dt = datetime.fromisoformat(local_iso)
    tz = pytz.timezone(timezone)
    return dt.astimezone(tz).date() == target_date.date()


def build_window(timezone: str, start_hour: int) -> tuple[datetime, datetime]:
    """构建时间窗口：昨天 start_hour 到今天 start_hour。"""
    tz = pytz.timezone(timezone)
    now = datetime.now(tz)
    today_anchor = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if now < today_anchor:
        end_dt = today_anchor
    else:
        end_dt = today_anchor
    start_dt = end_dt - timedelta(days=1)
    return start_dt, end_dt


def is_in_window(local_iso: str, start_dt: datetime, end_dt: datetime) -> bool:
    if not local_iso:
        return False
    dt = datetime.fromisoformat(local_iso)
    return start_dt <= dt < end_dt


def crawl_bloomberg_daily(
    feed_url: str,
    timezone: str,
    only_today: bool,
    use_window: bool,
    window_start_hour: int,
    limit: Optional[int],
) -> List[BloombergArticle]:
    root = fetch_rss(feed_url)
    items = parse_rss_items(root)

    tz = pytz.timezone(timezone)
    today = datetime.now(tz)
    window_start, window_end = build_window(timezone, window_start_hour)

    results: List[BloombergArticle] = []
    for item in items:
        published_at = parse_pub_time(item["pub_date"], timezone)
        if use_window:
            if not is_in_window(published_at, window_start, window_end):
                continue
        elif only_today and not is_same_day(published_at, timezone, today):
            continue

        try:
            content = fetch_article_content(item["link"])
        except requests.RequestException as exc:
            print(f"[WARN] 抓取失败: {item['link']} ({exc})")
            content = ""

        results.append(
            BloombergArticle(
                title=item["title"],
                content=content,
                published_at=published_at,
                url=item["link"],
            )
        )

        if limit is not None and len(results) >= limit:
            break

    return results


def default_output_file(out_dir: Path, timezone: str) -> Path:
    tz = pytz.timezone(timezone)
    day = datetime.now(tz).strftime("%Y-%m-%d")
    return out_dir / f"bloomberg_{day}.json"


def validate_feed_domain(feed_url: str) -> None:
    host = urlparse(feed_url).netloc.lower()
    if "bloomberg.com" not in host:
        raise ValueError(f"feed 域名非法（需为 bloomberg.com）：{host}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="每日抓取彭博社文章")
    parser.add_argument("--feed", default=DEFAULT_FEED, help="Bloomberg RSS 链接")
    parser.add_argument("--timezone", default="Asia/Shanghai", help="时区")
    parser.add_argument("--out", default="data/bloomberg", help="输出目录")
    parser.add_argument("--limit", type=int, default=None, help="最多抓取多少篇")
    parser.add_argument(
        "--window-start-hour",
        type=int,
        default=8,
        help="时间窗口起点小时（默认8，窗口为昨天8点到今天8点）",
    )
    parser.add_argument(
        "--mode",
        choices=["window", "today", "all"],
        default="window",
        help="抓取模式：window=昨天指定小时到今天指定小时；today=仅今天；all=不过滤日期",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="兼容旧参数：等同于 --mode all",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    validate_feed_domain(args.feed)
    if not (0 <= args.window_start_hour <= 23):
        raise ValueError("--window-start-hour 必须在 0-23 之间")

    mode = args.mode
    if args.all:
        mode = "all"

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    articles = crawl_bloomberg_daily(
        feed_url=args.feed,
        timezone=args.timezone,
        only_today=(mode == "today"),
        use_window=(mode == "window"),
        window_start_hour=args.window_start_hour,
        limit=args.limit,
    )

    output_path = default_output_file(out_dir, args.timezone)
    window_start, window_end = build_window(args.timezone, args.window_start_hour)
    payload = {
        "meta": {
            "source": "Bloomberg RSS",
            "feed": args.feed,
            "timezone": args.timezone,
            "mode": mode,
            "window_start_hour": args.window_start_hour,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "generated_at": datetime.now(pytz.timezone(args.timezone)).isoformat(),
            "count": len(articles),
        },
        "articles": [asdict(article) for article in articles],
    }

    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"完成，抓取 {len(articles)} 篇，输出文件：{output_path}")


if __name__ == "__main__":
    main()
