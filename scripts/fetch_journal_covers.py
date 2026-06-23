#!/usr/bin/env python3
"""
fetch_journal_covers.py — 给 publications.json 里每篇 paper 抓 journal cover image

用法:
    python3 scripts/fetch_journal_covers.py              # 全量抓
    python3 scripts/fetch_journal_covers.py --limit 5   # 只抓前 5 个（debug）
    python3 scripts/fetch_journal_covers.py --venue "NeuroImage"  # 只抓指定 venue

策略:
    1. 读 _data/publications.json
    2. 按 (venue, year) 分组
    3. 对每个 venue: 用 OpenAlex source 对象查 PII / ISSN / publisher
    4. 构造 issue URL（按 publisher 不同）
    5. 用 StealthyFetcher 抓 issue page，找 cover image URL
    6. 缓存到 _data/journal_covers.yml（venue + year → cover URL）

publisher 路由:
    Elsevier (10.1016):  /journal/{slug}/vol/{vol}/suppl/C → 找 ars.els-cdn.com cov200h.gif
    Wiley (10.1002):     /toc/{issn}/{vol}/{iss} → onlinelibrary.wiley.com cover
    Springer (10.1007):  /journal/{id}/volumes-and-issues/{vol}-{iss} → link.springer.com
    PLOS (10.1371):      /plosone/issue?id=...  → journals.plos.org
    Frontiers (10.3389): /journals/{name}/volumes-and-issues/{vol} → frontiersin.org
    OUP (10.1093):       /toc/{slug}/{vol}/{iss} → academic.oup.com
    MIT Press:           SKIP (CF-blocked)
"""
import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBS_JSON = REPO_ROOT / "_data" / "publications.json"
COVERS_YML = REPO_ROOT / "_data" / "journal_covers.yml"
OPENALEX_API = "https://api.openalex.org"
UA = "LABLabUM-cover-bot ([email protected])"


# =============== 工具 ===============

def load_pubs():
    with open(PUBS_JSON) as f:
        return json.load(f)


def load_covers():
    if COVERS_YML.exists():
        with open(COVERS_YML) as f:
            return yaml.safe_load(f) or {}
    return {}


def save_covers(covers):
    COVERS_YML.parent.mkdir(parents=True, exist_ok=True)
    with open(COVERS_YML, "w") as f:
        yaml.safe_dump(covers, f, allow_unicode=True, sort_keys=True)


def cache_key(venue, year):
    """统一 cache key"""
    return f"{venue}|{year}"


# =============== OpenAlex 辅助 ===============

_openalex_cache = {}

def get_source_for_venue(venue_name):
    """OpenAlex 查 source（journal），返回 {id, slug, issn_l, host_org_id, host_org_name}"""
    if venue_name in _openalex_cache:
        return _openalex_cache[venue_name]
    try:
        r = requests.get(
            f"{OPENALEX_API}/sources",
            params={"search": venue_name, "per_page": 3},
            headers={"User-Agent": UA},
            timeout=15,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        # 找一个最匹配的（同名）
        for s in results:
            if s.get("display_name", "").lower() == venue_name.lower():
                _openalex_cache[venue_name] = s
                return s
        if results:
            _openalex_cache[venue_name] = results[0]
            return results[0]
    except requests.RequestException as e:
        print(f"  ⚠️  OpenAlex {venue_name}: {e}")
    _openalex_cache[venue_name] = None
    return None


def publisher_for_venue(venue_name):
    """从 OpenAlex source 推断 publisher。"""
    s = get_source_for_venue(venue_name)
    if not s:
        return "unknown"
    host = (s.get("host_organization_name") or "").lower()
    issn = s.get("issn_l") or ""
    homepage = (s.get("homepage_url") or "").lower()

    # 精确匹配
    if "elsevier" in host or "sciencedirect" in homepage:
        return "elsevier"
    if "wiley" in host or "wiley" in homepage:
        return "wiley"
    if "springer" in host or "springer" in homepage:
        return "springer"
    if "mit press" in host or "direct.mit.edu" in homepage:
        return "mitpress"
    if "oxford" in host or "oup.com" in homepage or "academic.oup" in homepage:
        return "oup"
    if "plos" in host or "plos.org" in homepage or "public library of science" in host:
        return "plos"
    if "frontiers" in host or "frontiersin" in homepage:
        return "frontiers"
    if "cambridge" in host or "cambridge.org" in homepage:
        return "cambridge"
    if "sage" in host:
        return "sage"
    if "taylor" in host or "tandfonline" in homepage or "taylor & francis" in host:
        return "tandf"
    # 按 venue 名 fallback（针对识别不出来 publisher 的）
    n = venue_name.lower()
    if "neuroreport" in n:
        return "wolterskluwer"  # Lippincott
    if "scientific data" in n:
        return "nature"
    if "scientific reports" in n:
        return "nature"
    if "plos one" in n:
        return "plos"
    if "brain sciences" in n or "brainsci" in n:
        return "mdpi"
    return "unknown"


def venue_slug(venue_name):
    """把 venue name 转成 SD URL slug。
    NeuroImage → neuroimage
    Brain and Language → brain-and-language
    """
    s = venue_name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s


def issue_url_for_paper(venue, year, vol, issue, publisher):
    """构造 issue URL（按 publisher）。"""
    slug = venue_slug(venue)
    src = get_source_for_venue(venue)
    issn_l = (src or {}).get("issn_l", "")
    homepage = (src or {}).get("homepage_url", "")

    if publisher == "elsevier":
        if vol:
            iss = f"/issue/{issue}" if issue else "/suppl/C"
            return f"https://www.sciencedirect.com/journal/{slug}/vol/{vol}{iss}"

    if publisher == "wiley":
        # Wiley: onlinelibrary.wiley.com/toc/{issn}/{year}/{vol}/{iss}
        # 但更可靠: 用 OpenAlex source homepage_url 推
        if issn_l and vol:
            if issue:
                return f"https://onlinelibrary.wiley.com/toc/{issn_l}/{year}/{vol}/{issue}"
            return f"https://onlinelibrary.wiley.com/toc/{issn_l}/{year}/{vol}"
        if vol and issue:
            return f"https://onlinelibrary.wiley.com/toc/{slug}/{year}/{vol}/{issue}"

    if publisher == "springer":
        # Springer: link.springer.com/journal/{id}/volumes-and-issues/{vol}-{iss}
        # 也试: /journal/{slug}/volumes-and-issues
        if vol and issue:
            return f"https://link.springer.com/journal/{slug}/volumes-and-issues/{vol}-{issue}"
        if vol:
            return f"https://link.springer.com/journal/{slug}/volumes-and-issues/{vol}"

    if publisher == "mitpress":
        # MIT Press: direct.mit.edu/{slug}/issue/{vol}/{issue}
        # CF 墙了，跳过
        return None

    if publisher == "oup":
        # OUP: academic.oup.com/{slug}/issue/{vol}/{issue}
        if vol and issue:
            return f"https://academic.oup.com/{slug}/issue/{vol}/{issue}"
        if vol:
            return f"https://academic.oup.com/{slug}/issue/{vol}"

    if publisher == "plos":
        # PLOS: journals.plos.org/plosone/issue?id=...（复杂 id）
        # 简单方式：journals.plos.org/{slug} → 找 current issue cover
        if slug:
            return f"https://journals.plos.org/{slug}/"

    if publisher == "frontiers":
        # Frontiers: frontiersin.org/journals/{slug}/volumes-and-issues/{vol}
        if vol:
            return f"https://www.frontiersin.org/journals/{slug}/volumes-and-issues/{vol}"

    if publisher == "cambridge":
        # Cambridge: cambridge.org/core/journals/{slug}/issue/{vol}-{issue}
        if vol and issue:
            return f"https://www.cambridge.org/core/journals/{slug}/issue/{vol}-{issue}"
        if vol:
            return f"https://www.cambridge.org/core/journals/{slug}/issue/{vol}"

    if publisher == "tandf":
        # Taylor & Francis: tandfonline.com/toc/{issn}/{year}/{vol}/{iss}
        if issn_l and vol:
            if issue:
                return f"https://www.tandfonline.com/toc/{issn_l}/{year}/{vol}/{issue}"
            return f"https://www.tandfonline.com/toc/{issn_l}/{year}/{vol}"

    if publisher == "nature":
        # Nature Portfolio: nature.com/{slug}/volumes/{vol}
        if vol:
            return f"https://www.nature.com/{slug}/volumes/{vol}"
        if slug:
            return f"https://www.nature.com/{slug}/"

    if publisher == "mdpi":
        # MDPI: mdpi.com/{issn}/issues
        if issn_l:
            return f"https://www.mdpi.com/{issn_l}/issues"
        if slug:
            return f"https://www.mdpi.com/journal/{slug}/issues"

    if publisher == "wolterskluwer":
        # Wolters Kluwer / Lippincott: journals.lww.com/{slug}/toc...
        if slug:
            return f"https://journals.lww.com/{slug}/toc/default.aspx"

    return None


# =============== 抓 cover ===============

def fetch_with_stealthy(url, timeout=30000):
    """用 StealthyFetcher 抓页面（绕 CF）。"""
    from scrapling.fetchers import StealthyFetcher
    try:
        page = StealthyFetcher.fetch(url, headless=True, network_idle=False, timeout=timeout)
        html = page.html_content
        if isinstance(html, bytes):
            html = html.decode("utf-8", errors="ignore")
        return html
    except Exception as e:
        return None


def extract_cover_url(html, publisher):
    """从 issue page HTML 抓 cover URL。"""
    if not html:
        return None
    # Elsevier: ars.els-cdn.com cov200h/cov500h gif
    if publisher == "elsevier":
        m = re.search(r'(https?://ars\.els-cdn\.com/content/image/[^"\s<>]+cov200h\.[a-z]+)', html)
        if m:
            return m.group(1)
        m = re.search(r'(https?://ars\.els-cdn\.com/content/image/[^"\s<>]+cov150h\.[a-z]+)', html)
        if m:
            return m.group(1)
    # Wiley: onlinelibrary cover image
    if publisher == "wiley":
        m = re.search(r'(https?://[^"\s<>]+onlinelibrary\.wiley\.com/[^"\s<>]+(?:cover|issue)[^"\s<>]*\.(?:jpg|png|gif))', html, re.I)
        if m:
            return m.group(1)
        # Open Graph image often works
        og = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
        if og:
            return og.group(1)
    # Springer: link.springer.com cover image
    if publisher == "springer":
        m = re.search(r'(https?://[^"\s<>]+link\.springer\.com/[^"\s<>]+cover[^"\s<>]*\.(?:jpg|png))', html, re.I)
        if m:
            return m.group(1)
        og = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
        if og:
            return og.group(1)
    # OUP
    if publisher == "oup":
        og = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
        if og:
            return og.group(1)
    # PLOS
    if publisher == "plos":
        og = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
        if og:
            return og.group(1)
    # Generic OG image fallback
    og = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
    if og:
        return og.group(1)
    return None


# =============== 主流程 ===============

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="限制抓的 venue 数（debug 用）")
    ap.add_argument("--venue", default="", help="只抓指定 venue")
    ap.add_argument("--delay", type=float, default=2.0, help="每次抓之间的延迟（秒）")
    args = ap.parse_args()

    pubs = load_pubs()
    covers = load_covers()

    # 按 (venue, year) 分组
    grouped = defaultdict(list)
    for p in pubs:
        key = cache_key(p.get("venue_name", ""), p.get("year", ""))
        if key == "|":
            continue
        if key in covers and covers[key].get("cover_url"):
            continue  # 已 cache
        grouped[key].append(p)

    print("=" * 70)
    print("  LAB Lab — Journal cover fetcher")
    print("=" * 70)
    print(f"  Pubs: {len(pubs)}")
    print(f"  Already cached: {sum(1 for v in covers.values() if v.get('cover_url'))}")
    print(f"  To fetch: {len(grouped)}")
    print()

    count = 0
    success = 0
    fail = 0
    for key, items in sorted(grouped.items()):
        if args.venue and args.venue not in key:
            continue
        if args.limit and count >= args.limit:
            break
        count += 1

        venue, year = key.split("|", 1)
        publisher = publisher_for_venue(venue)
        if publisher == "unknown":
            print(f"  [{count}] {venue} ({year}) → publisher unknown, skip")
            continue

        # 收集所有可能的 vol/issue（每个 paper 自己的）
        # 然后逐个尝试
        tried_urls = set()
        any_success = False

        for p0 in items:
            vol = ""
            issue = ""
            journal = p0.get("journal", "") or ""
            m = re.search(r',\s*(\d+)(?:\((\S+?)\))?', journal)
            if m:
                vol = m.group(1)
                issue = m.group(2) or ""
            if not vol:
                continue

            url = issue_url_for_paper(venue, year, vol, issue, publisher)
            if not url or url in tried_urls:
                continue
            tried_urls.add(url)

            print(f"  [{count}] {venue} ({year}, {publisher}) → {url[:90]}...", flush=True)
            html = fetch_with_stealthy(url)
            if not html:
                print(f"      ⚠️  fetch failed")
                continue

            cover = extract_cover_url(html, publisher)
            if cover:
                covers[key] = {"venue": venue, "year": year, "cover_url": cover, "publisher": publisher}
                print(f"      ✅ {cover[:120]}")
                success += 1
                any_success = True
                break  # 找到一个就够

        if not any_success:
            covers[key] = {"venue": venue, "year": year, "cover_url": "", "publisher": publisher, "note": "no cover found in any vol"}
            print(f"  [{count}] {venue} ({year}) → no cover found across {len(tried_urls)} vol(s)")
            fail += 1

        time.sleep(args.delay)

    save_covers(covers)
    print()
    print("=" * 70)
    print(f"  ✅ DONE")
    print(f"  Fetched: {count}")
    print(f"  Success: {success}")
    print(f"  Fail: {fail}")
    print(f"  Cached at: {COVERS_YML.relative_to(REPO_ROOT)}")
    print("=" * 70)


if __name__ == "__main__":
    main()
