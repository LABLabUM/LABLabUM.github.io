#!/usr/bin/env python3
"""
fetch_publications.py — 从 OpenAlex 拉 PI 的所有论文，写 _data/publications.json

用法:
    python3 scripts/fetch_publications.py             # 跑一次
    python3 scripts/fetch_publications.py --dry-run   # 只打印 diff，不写文件
    python3 scripts/fetch_publications.py --no-merge  # 不合并 manual yml

依赖:
    pip install requests

原理:
    1. 调 OpenAlex Works API: filter=authorships.author.id:<PI_OPENALEX_ID>
    2. 对每篇 paper: 抽 title/authors/year/venue/doi/volume/issue/pages
    3. 过滤同名误归（venue 关键词 + coauthor 启发式）
    4. APA 7 风格 normalize 作者: "J Zhong" → "Zhong, J."
    5. 按 year desc 排序
    6. 跟 _data/publications_manual.yml 合并（manual 永远在最后）
    7. 写 _data/publications.json

数据流:
    OpenAlex API → this script → _data/publications.json → Jekyll → /publications/ 页面
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    print("❌ 需要 requests。装一下: pip install requests")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("❌ 需要 PyYAML。装一下: pip install pyyaml")
    sys.exit(1)


# ============ 配置 ============

# PI 在 OpenAlex 的 Author ID
PI_OPENALEX_ID = "A5047800217"

# OpenAlex API endpoint
OPENALEX_API = "https://api.openalex.org"

# 输出文件（相对仓库根）
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_JSON = REPO_ROOT / "_data" / "publications.json"
MANUAL_YML = REPO_ROOT / "_data" / "publications_manual.yml"

# 联系邮箱（OpenAlex 礼貌要求 User-Agent 里带）
CONTACT_EMAIL = "[email protected]"

# === 同名误归过滤 ===

# 必须出现在 venue 名称里的关键词（**当前未启用**，保留以便后续需要时启用）
# 同名误归是 OpenAlex 的责任，我们不当裁判 —— 老板的 ID 下的 paper 全收
VENUE_KEYWORDS_REQUIRED = []  # disabled

# 实验室常出现的合作者（**当前未启用**，同上原因）
TRUSTED_COAUTHORS = []  # disabled

# === APA 7 normalize ===
# OpenAlex 给 "J Zhong" 这种。我们 normalize 到 "Zhong, J."
# 已知映射表：first-initial + lastname → APA-style full name
# （从 _pages/team/_posts/*.md 自动生成；下面是 fallback）
APA_NAME_MAP = {
    # First-initial + lastname (OpenAlex 短形式) → APA 7
    "H Zhang": "Zhang, H.",
    "K Kang": "Kang, K.",
    "J Zhong": "Zhong, J.",
    "K Lei": "Lei, K.",
    "H Yu": "Yu, H.",
    "S Zhang": "Zhang, S.",
    "J Li": "Li, J.",
    "J Chen": "Chen, J.",
    "Y Cai": "Cai, Y.",
    "Z Liu": "Liu, Z.",
    "X Wang": "Wang, X.",
    "Y Xiao": "Xiao, Y.",
    "H Chen": "Chen, H.",
    "D Li": "Li, D.",
    "CT Ip": "Ip, C.-T.",
    "T Guo": "Guo, T.",
    "TW Guo": "Guo, T.",
    "C Pliatsikas": "Pliatsikas, C.",
    "M Nakamura": "Nakamura, M.",
    "M Diaz": "Diaz, M.",
    "W Huang": "Huang, W.",
    "Z Zhao": "Zhao, Z.",
    "R Li": "Li, R.",
    "Y Li": "Li, Y.",
    "Y Wu": "Wu, Y.",
    "F Ma": "Ma, F.",
    "C Kang": "Kang, C.",
    "Y Zhou": "Zhou, Y.",
    "Y Wang": "Wang, Y.",
    "H Wang": "Wang, H.",
    "JS Chen": "Chen, J.",
    "WL Chan": "Chan, W.-L.",
    "S Olbrich": "Olbrich, S.",
    "X Jiang": "Jiang, X.",
    "M Brunovsky": "Brunovsky, M.",
    "DB Teplow": "Teplow, D. B.",
    "B Teplow": "Teplow, D. B.",
    # Full names (有时候 OpenAlex 给完整名字)
    "Haoyun Zhang": "Zhang, H.",
    "Hanxiang Yu": "Yu, H.",
    "Keikei Lei": "Lei, K.",
    "Keyi Kang": "Kang, K.",
    "Jing Zhong": "Zhong, J.",
    "Sifan Zhang": "Zhang, S.",
    "Jiaze Li": "Li, J.",
    "Jielu Chen": "Chen, J.",
    "Yimin Cai": "Cai, Y.",
    "Zhengyuan Liu": "Liu, Z.",
    "Xiaomeng Wang": "Wang, X.",
    "Yumeng Xiao": "Xiao, Y.",
    "Huitian Chen": "Chen, H.",
    "Weike Huang": "Huang, W.",
    "Ziyi Zhao": "Zhao, Z.",
    "Rihui Li": "Li, R.",
    "Yuhang Li": "Li, Y.",
    "Hanyu Wang": "Wang, H.",
    "Cheng-Teng Ip": "Ip, C.-T.",
    "Weng-Lam Chan": "Chan, W.-L.",
    "Michèle T. Diaz": "Diaz, M. T.",
    "Michèle Diaz": "Diaz, M.",
    "Megan Nakamura": "Nakamura, M.",
    "Christos Pliatsikas": "Pliatsikas, C.",
    "David B. Teplow": "Teplow, D. B.",
    "Tao Guo": "Guo, T.",
    "T.W. Guo": "Guo, T.",
    "Dezhi Li": "Li, D.",
    "Yushen Zhou": "Zhou, Y.",
    "Yimin Wu": "Wu, Y.",
    "Fengyang Ma": "Ma, F.",
}


# ============ 工具函数 ============

def fetch_openalex_works(author_id: str, per_page: int = 200) -> list[dict]:
    """拉 PI 的所有 works（OpenAlex 一次最多 200/页，自动翻页）。"""
    all_works = []
    cursor = "*"
    page = 0
    while cursor:
        page += 1
        url = f"{OPENALEX_API}/works"
        params = {
            "filter": f"authorships.author.id:{author_id}",
            "per_page": per_page,
            "cursor": cursor,
            "sort": "publication_date:desc",
        }
        headers = {"User-Agent": f"LABLabUM-publications-bot ({CONTACT_EMAIL})"}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            print(f"  ❌ Network error on page {page}: {e}")
            break

        results = data.get("results", [])
        all_works.extend(results)
        print(f"  📥 page {page}: {len(results)} works (total so far: {len(all_works)})")

        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor or len(results) == 0:
            break

        # OpenAlex 礼貌：每秒 ≤10 请求
        time.sleep(0.15)

    return all_works


# === PI 真实机构白名单 ===
# PI (Haoyun Zhang) 的实际工作机构（从 _pages/team/_posts/2023-06-07-haoyunzhang-staff.md 提取）：
#   - PhD @ Pennsylvania State University
#   - MS @ Beijing Normal University
#   - BS @ Shaanxi Normal University
#   - 当前 (2022–Present) Assistant Professor @ University of Macau (CCBS 子机构)
#   - 2019–2021 Assistant Research Professor + SLEIC Imaging Director @ Penn State
#   - 2018 访问学者 @ McGovern Institute (MIT) + Columbia
#
# OpenAlex 给的 PI institution 通常是父机构（Penn State / UM），
# 子机构（SLEIC、Imaging Center、CCBS）也匹配。
PI_INSTITUTIONS = {
    # 当前 + 父机构
    "University of Macau",
    "Pennsylvania State University",
    "Beijing Normal University",
    "Shaanxi Normal University",
    # 子机构 / 实验室
    "McGovern Institute for Brain Research",   # MIT
    "Social, Life, & Engineering Sciences Imaging Center",  # Penn State SLEIC
    "Imaging Center",                          # SLEIC 的简化名
    "Centre for Cognitive and Brain Sciences",  # UM CCBS
    "State Key Laboratory of Cognitive Neuroscience and Learning",  # BNU 关联
    "Columbia University",
    "University of California, Santa Barbara",
}


def inst_match(inst_name: str) -> bool:
    """机构名是否在 PI 真实机构白名单（子串匹配）。"""
    if not inst_name:
        return False
    n = inst_name.lower()
    return any(k.lower() in n for k in PI_INSTITUTIONS)


def is_likely_lab_paper(work: dict) -> bool:
    """判断是不是 PI 实验室的 paper。

    双重过滤：
      1. PI 必须在作者列表里（OpenAlex ID 匹配）
      2. PI 至少有一个 institution 在 PI 真实履历白名单里

    任意一条不满足 → 视为同名误归，丢弃。
    """
    has_pi = False
    pi_insts_match = False
    for a in work.get("authorships", []):
        au = a.get("author") or {}
        if (au.get("id") or "").endswith(f"/{PI_OPENALEX_ID}"):
            has_pi = True
            # 检查 PI 的所有 institutions
            for inst in (a.get("institutions") or []):
                if inst_match(inst.get("display_name", "")):
                    pi_insts_match = True
                    break
            break
    return has_pi and pi_insts_match


def normalize_author(openalex_name: str) -> str:
    """OpenAlex 给 "J Zhong" → APA 7 风格 "Zhong, J."
    如果不在映射表，用启发式：拆单词，假设最后一词是姓。
    """
    if not openalex_name:
        return ""
    name = openalex_name.strip()

    # 已知映射优先
    # 也匹配 "J. Zhong" 带句点的情况
    key1 = name  # "J Zhong"
    key2 = re.sub(r"\s+", " ", name)  # normalize space
    # 也试试 "J. Zhong" 形式
    m = re.match(r"^([A-Z])\.?\s+(.+)$", name)
    if m:
        key3 = f"{m.group(1)} {m.group(2)}"
    else:
        key3 = None

    for k in [key1, key2, key3]:
        if k and k in APA_NAME_MAP:
            return APA_NAME_MAP[k]

    # Fallback：启发式 — 假设 "X Lastname" 或 "X. Lastname" 或 "Firstname Lastname"
    parts = name.split()
    if len(parts) == 2:
        first, last = parts
        # 如果 first 是单字母或带点 → 视为 "X Lastname"
        if len(first) <= 2 or first.endswith("."):
            initials = first.replace(".", "")
            return f"{last}, {initials}."
        # 否则视为 "Firstname Lastname" → 已经在 APA 友好格式（不需要换）
        else:
            return name
    elif len(parts) == 3:
        # "David B. Teplow" → "Teplow, D. B."
        if len(parts[1]) <= 3 and (parts[1].endswith(".") or len(parts[1]) == 1):
            initials = parts[0][0] + ". " + parts[1].rstrip(".") + "."
            return f"{parts[2]}, {initials}"
        # "Mary Anne Smith" → "Smith, M. A."
        else:
            initials = ". ".join(p[0] for p in parts[:-1]) + "."
            return f"{parts[-1]}, {initials}"
    # 单单词 / 多单词：原样返回
    return name


def normalize_authors_list(authorships: list) -> list[str]:
    """把 OpenAlex 作者列表转 APA 7 风格。"""
    out = []
    for a in authorships:
        au = a.get("author") or {}
        name = au.get("display_name") or ""
        if name:
            out.append(normalize_author(name))
    return out


def format_authors_apa(authors: list[str]) -> str:
    """APA 7 列表格式: "Smith, J., Doe, A. B., & Wong, C."
    注意：& 前要逗号 + 空格。
    """
    if not authors:
        return ""
    if len(authors) == 1:
        return authors[0]
    if len(authors) == 2:
        return f"{authors[0]}, & {authors[1]}"
    return ", ".join(authors[:-1]) + f", & {authors[-1]}"


def work_to_publication(work: dict) -> dict:
    """OpenAlex work → publications.json 的格式。"""
    venue = (work.get("primary_location") or {}).get("source") or {}
    venue_name = venue.get("display_name", "")
    # 优先用 OpenAlex 的 biblio（volume / issue / first_page / last_page）
    biblio = work.get("biblio") or {}

    # authors
    authors_apa = normalize_authors_list(work.get("authorships", []))
    authors_str = format_authors_apa(authors_apa)

    # venue with vol(issue), pages
    vol = biblio.get("volume")
    issue = biblio.get("issue")
    fp = biblio.get("first_page")
    lp = biblio.get("last_page")

    venue_with_details = venue_name
    if vol:
        if issue:
            venue_with_details += f", {vol}({issue})"
        else:
            venue_with_details += f", {vol}"
    if fp:
        if lp and lp != fp:
            venue_with_details += f", {fp}–{lp}"
        else:
            venue_with_details += f", {fp}"

    # DOI link
    doi = work.get("doi") or ""
    doi_url = doi if doi.startswith("http") else (f"https://doi.org/{doi}" if doi else "")

    # publication_year + publication_date
    year = work.get("publication_year")
    pub_date = work.get("publication_date") or ""

    # type
    work_type = work.get("type", "").replace("-", " ").title()  # "journal-article" → "Journal Article"

    return {
        "title": work.get("title", "").strip(),
        "link": doi_url or (work.get("id") or ""),  # fallback to OpenAlex URL
        "authors": authors_str,
        "authors_apa": authors_apa,  # 备用：list 形式，方便模板用
        "journal": venue_with_details,
        "venue_name": venue_name,
        "venue_type": work_type,
        "year": str(year) if year else "",
        "doi": doi,
        "publication_date": pub_date,  # YYYY-MM-DD 用于排序
        "cited_by_count": work.get("cited_by_count", 0),
        "is_oa": bool(work.get("open_access", {}).get("is_oa")),
        "oa_url": (work.get("open_access") or {}).get("oa_url") or "",
        "openalex_id": work.get("id", ""),
        # 出版社 ID（OpenAlex P-prefix），用于查 logo
        # host_organization 是 URL 形式："https://openalex.org/P4310320990"
        "publisher_id": (venue.get("host_organization") or "").rsplit("/", 1)[-1],
        "publisher_name": venue.get("host_organization_name") or "",
    }


def load_manual_yml() -> list[dict]:
    """读 _data/publications_manual.yml —— 不在 OpenAlex 里的手动补的论文。"""
    if not MANUAL_YML.exists():
        return []
    with open(MANUAL_YML) as f:
        data = yaml.safe_load(f) or []
    if not isinstance(data, list):
        print(f"  ⚠️  {MANUAL_YML.name} 应该是 list 格式（- title: ...）")
        return []
    # 给 manual 加 source 标记
    for item in data:
        item["source"] = "manual"
    return data


# ============ Publisher logo 缓存 ============

PUBLISHER_LOGOS_YML = REPO_ROOT / "_data" / "publisher_logos.yml"


def load_publisher_logos() -> dict:
    """读 _data/publisher_logos.yml（id → {name, logo_url}）。"""
    if not PUBLISHER_LOGOS_YML.exists():
        return {}
    with open(PUBLISHER_LOGOS_YML) as f:
        return yaml.safe_load(f) or {}


def save_publisher_logos(logos: dict) -> None:
    """写 _data/publisher_logos.yml（让用户可手动改）。"""
    PUBLISHER_LOGOS_YML.parent.mkdir(parents=True, exist_ok=True)
    with open(PUBLISHER_LOGOS_YML, "w", encoding="utf-8") as f:
        yaml.safe_dump(logos, f, allow_unicode=True, sort_keys=True, default_flow_style=False)


def fetch_publisher_logo(publisher_id: str) -> dict | None:
    """OpenAlex 查单个 publisher 的 logo。返回 {name, logo_url} 或 None。"""
    if not publisher_id:
        return None
    try:
        r = requests.get(
            f"{OPENALEX_API}/publishers/{publisher_id}",
            headers={"User-Agent": f"LABLabUM-publications-bot ({CONTACT_EMAIL})"},
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        logo_url = d.get("image_url")
        name = d.get("display_name") or ""
        if logo_url:
            return {"name": name, "logo_url": logo_url}
    except requests.RequestException as e:
        print(f"  ⚠️  publisher {publisher_id}: {e}")
    return None


def publisher_for_venue(venue_name: str) -> str:
    """从 venue 名推断 publisher 类型（用于选 issue URL pattern）。
    不是强制匹配 — 真正 publisher logo 用 OpenAlex API 查。"""
    n = venue_name.lower()
    if "neurobiology of language" in n:
        return "mitpress"
    if "neuroreport" in n:
        return "wolterskluwer"
    if "scientific data" in n:
        return "nature"
    if "scientific reports" in n:
        return "nature"
    if "brain sciences" in n or "brainsci" in n:
        return "mdpi"
    if "plos one" in n:
        return "plos"
    return "unknown"


# Venue → publisher_id 强制映射（OpenAlex 数据缺失时手动补）
VENUE_PUBLISHER_FALLBACK = {
    "Neurobiology of Language": ("P4310315718", "The MIT Press"),
}


def enrich_publisher_logos(pubs, cache):
    """给每篇 pub 加 publisher_logo 字段（从 cache 或新查）。返回 (pubs, new_cache)。"""
    # 收集所有 publisher_id
    needed = set()
    for p in pubs:
        pid = p.get("publisher_id")
        if not pid:
            # 尝试 VENUE_PUBLISHER_FALLBACK
            venue = p.get("venue_name", "")
            if venue in VENUE_PUBLISHER_FALLBACK:
                pid, pname = VENUE_PUBLISHER_FALLBACK[venue]
                p["publisher_id"] = pid
                p["publisher_name"] = pname
        if pid and pid not in cache:
            needed.add(pid)

    if needed:
        print(f"\n🎨 Fetching publisher logos for {len(needed)} new publisher(s)...")
        for i, pid in enumerate(sorted(needed), 1):
            print(f"  [{i}/{len(needed)}] {pid}...", end=" ", flush=True)
            info = fetch_publisher_logo(pid)
            if info:
                cache[pid] = info
                print(f"✅ {info['name']}")
            else:
                # 标记为查过但无 logo（避免下次重复查）
                cache[pid] = {"name": "", "logo_url": ""}
                print("⏭️  no logo")
            time.sleep(0.2)

    # 把 logo_url 加到每篇 pub（来自 cache 或空）
    for p in pubs:
        pid = p.get("publisher_id")
        if pid:
            info = cache.get(pid, {})
            # 优先用 local_path（本地存的 logo），fallback 到 logo_url（外链）
            local_logo = info.get("local_path", "") or ""
            remote_logo = info.get("logo_url", "") or ""
            chosen = local_logo or remote_logo
            # ⚠️ 重要：空字符串会渲染成 <img src=""> 出现 broken image，必须用 None
            p["publisher_logo"] = chosen if chosen else None
            if not p.get("publisher_name"):
                p["publisher_name"] = info.get("name", "")
    return pubs, cache


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印 diff，不写文件")
    ap.add_argument("--no-merge", action="store_true", help="不合并 manual yml")
    args = ap.parse_args()

    print("=" * 70)
    print("  LAB Lab — Publications fetcher")
    print(f"  PI OpenAlex ID: {PI_OPENALEX_ID}")
    print(f"  Output: {OUTPUT_JSON.relative_to(REPO_ROOT)}")
    print(f"  Manual yml: {MANUAL_YML.relative_to(REPO_ROOT)}")
    print("=" * 70)

    # 1. Fetch
    print("\n📡 Fetching from OpenAlex...")
    works = fetch_openalex_works(PI_OPENALEX_ID)
    print(f"  Total works fetched: {len(works)}")

    # 2. Filter + dedupe
    print("\n🔍 Keeping PI's works + de-duplicating...")
    seen_dois = set()
    seen_titles = set()
    kept = []
    for w in works:
        if not is_likely_lab_paper(w):
            continue
        # Dedupe by DOI first, then by title
        doi = (w.get("doi") or "").lower().strip()
        title = (w.get("title") or "").lower().strip()
        if doi:
            if doi in seen_dois:
                continue
            seen_dois.add(doi)
        else:
            if title in seen_titles:
                continue
            seen_titles.add(title)
        kept.append(w)
    dropped = len(works) - len(kept)
    print(f"  ✅ Kept (after dedup): {len(kept)}")
    print(f"  ❌ Dropped: {dropped}")

    # 3. Transform
    print("\n✨ Transforming to APA 7 format...")
    pubs = [work_to_publication(w) for w in kept]

    # 3.5 Enrich with publisher logos
    logo_cache = load_publisher_logos()
    pubs, logo_cache = enrich_publisher_logos(pubs, logo_cache)
    save_publisher_logos(logo_cache)
    with_logo = sum(1 for p in pubs if p.get("publisher_logo"))
    print(f"  ✅ Publications with publisher logo: {with_logo}/{len(pubs)}")

    # 4. Merge manual FIRST (so sort puts them in correct position)
    if not args.no_merge:
        manual = load_manual_yml()
        if manual:
            print(f"\n📝 Merging {len(manual)} manual entries from publications_manual.yml...")
            # Manual 永远保留，加到列表
            # 不重复：按 title 匹配
            existing_titles = {p["title"].lower().strip() for p in pubs}
            added = 0
            for m in manual:
                if m.get("title", "").lower().strip() not in existing_titles:
                    pubs.append(m)
                    added += 1
                else:
                    print(f"  ⏭️  Manual duplicate (skipping): {m.get('title','')[:50]}")
            print(f"  ✅ Added {added} unique manual entries")

    # 5. Sort by year desc, then publication_date desc, then title
    def sort_key(p):
        y = int(p["year"]) if p["year"] else 0
        d = p.get("publication_date") or ""
        return (-y, -1 if d else 0, "-" if not d else "", d, p["title"])
    pubs.sort(key=sort_key)

    # 6. Dry run?
    if args.dry_run:
        print("\n" + "=" * 70)
        print(f"  DRY RUN — would write {len(pubs)} publications to {OUTPUT_JSON.name}")
        print("=" * 70)
        for i, p in enumerate(pubs[:10], 1):
            print(f"\n  #{i} [{p['year']}] {p['title'][:70]}")
            print(f"      {p['authors'][:80]}")
            print(f"      {p['journal'][:80]}")
            print(f"      DOI: {p['doi'] or '(none)'}")
        if len(pubs) > 10:
            print(f"\n  ... and {len(pubs) - 10} more")
        return

    # 7. Write
    print(f"\n💾 Writing {len(pubs)} publications to {OUTPUT_JSON.relative_to(REPO_ROOT)}...")
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(pubs, f, ensure_ascii=False, indent=2)

    # 8. Summary
    print("\n" + "=" * 70)
    print(f"  ✅ DONE")
    print(f"  Total publications written: {len(pubs)}")
    if pubs:
        years = [int(p["year"]) for p in pubs if p["year"]]
        if years:
            print(f"  Year range: {min(years)} – {max(years)}")
        with_doi = sum(1 for p in pubs if p["doi"])
        print(f"  With DOI: {with_doi}/{len(pubs)}")
    print("=" * 70)
    print(f"\n💡 下一步:")
    print(f"   1. 看 _data/publications.json 确认正确")
    print(f"   2. bundle exec jekyll serve 看 /publications/")
    print(f"   3. git add _data/publications.json && git commit && git push")


if __name__ == "__main__":
    main()
