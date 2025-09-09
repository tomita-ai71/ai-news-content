#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ニュース収集→ストーリー化（初報/続報紐付け）→Markdown生成（jp/en）
出力: jp/input.md, en/input.md（configのemit_*で制御）
"""

from __future__ import annotations
import argparse, json, os, sys, re, datetime as dt
from pathlib import Path
from typing import List, Dict, Any, Tuple

import feedparser
import faiss
import numpy as np
from sentence_transformers import SentenceTransformer
import yaml

ROOT = Path(__file__).resolve().parent.parent  # repo root
AUTOMATION_DIR = ROOT / "automation"
JP_MD = ROOT / "jp" / "input.md"
EN_MD = ROOT / "en" / "input.md"
DEBUG_DIR = AUTOMATION_DIR / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# --------- util ---------
def load_config(cfg_path: Path) -> dict:
    with cfg_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def iso_date(dtobj: dt.datetime | None, fallback: str = "") -> str:
    if not dtobj:
        return fallback or dt.date.today().isoformat()
    return dtobj.date().isoformat()

def clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s

def short_summary(title: str, summary: str, limit: int = 140) -> str:
    base = clean_text(summary) or clean_text(title)
    return (base[:limit] + "…") if len(base) > limit else base

def now_jst_date() -> str:
    return (dt.datetime.utcnow() + dt.timedelta(hours=9)).date().isoformat()

# --------- fetching ---------
def fetch_articles(feeds: List[str], per_feed_limit: int) -> List[Dict[str, Any]]:
    arts: List[Dict[str, Any]] = []
    for url in feeds:
        try:
            d = feedparser.parse(url)
            for e in d.entries[: per_feed_limit]:
                pub = None
                for key in ("published_parsed", "updated_parsed"):
                    if getattr(e, key, None):
                        pub = dt.datetime(*getattr(e, key)[:6])
                        break
                arts.append({
                    "title": clean_text(getattr(e, "title", "")),
                    "link": getattr(e, "link", ""),
                    "summary": clean_text(getattr(e, "summary", "")),
                    "source": clean_text(getattr(d.feed, "title", "")) or clean_text(url),
                    "published": iso_date(pub, fallback=now_jst_date()),
                })
        except Exception as ex:
            print(f"[warn] feed error {url}: {ex}", file=sys.stderr)
    # 重複タイトル・リンクを緩めに除去
    seen = set(); uniq = []
    for a in arts:
        k = (a["title"], a["link"])
        if k in seen: continue
        seen.add(k); uniq.append(a)
    return uniq

# --------- clustering (story linking) ---------
def build_index(embs: np.ndarray) -> faiss.Index:
    dim = embs.shape[1]
    index = faiss.IndexFlatIP(dim)  # cosine用に後で正規化して内積
    return index

def l2norm(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=1, keepdims=True) + 1e-12
    return v / n

def link_stories(articles: List[Dict[str, Any]], thr: float, model_name="all-MiniLM-L6-v2") -> List[Dict[str, Any]]:
    if not articles:
        return []

    model = SentenceTransformer(model_name)
    texts = [a["title"] + " " + (a["summary"] or "") for a in articles]
    embs = model.encode(texts, convert_to_numpy=True)
    embs = l2norm(embs)

    index = build_index(embs)
    index.add(embs)

    stories: List[Dict[str, Any]] = []
    centroids: List[np.ndarray] = []  # 各ストーリーの代表ベクトル
    story_ids: List[int] = []

    for i, art in enumerate(articles):
        if not centroids:
            stories.append({
                "id": 0,
                "title": art["title"],
                "articles": [art],
                "first_seen_at": art["published"],
                "last_updated_at": art["published"],
            })
            centroids.append(embs[i].copy())
            story_ids.append(0)
            continue

        # 既存ストーリーとの最大類似度
        cents = np.stack(centroids)
        sims = (cents @ embs[i].reshape(-1,1)).squeeze(1)
        j = int(np.argmax(sims)); sim = float(sims[j])

        if sim >= thr:
            sid = j
            stories[sid]["articles"].append(art)
            stories[sid]["last_updated_at"] = max(stories[sid]["last_updated_at"], art["published"])
            # 代表ベクトルを更新（単純平均）
            new_centroid = (centroids[sid]* (len(stories[sid]["articles"])-1) + embs[i]) / len(stories[sid]["articles"])
            centroids[sid] = new_centroid
        else:
            sid = len(stories)
            stories.append({
                "id": sid,
                "title": art["title"],
                "articles": [art],
                "first_seen_at": art["published"],
                "last_updated_at": art["published"],
            })
            centroids.append(embs[i].copy())
        story_ids.append(sid)

    # 各ストーリー内を日付昇順に
    for s in stories:
        s["articles"].sort(key=lambda x: x["published"])
    return stories

# --------- rendering (markdown) ---------
JP_TMPL = """# {headline}（最終更新：{last_updated}）
**3行要約**  
- {sum1}  
- {sum2}  
- {sum3}

## 全体像（なぜ重要か）
{overview}

## タイムライン
{timeline}

## いま時点の理解
- 技術/製品: {tech}
- 競合/エコシステム: {eco}
- リスク/不確実性: {risk}

## 次の注目ポイント
- {next1}
- {next2}

—  
出典まとめ：
{refs}
"""

EN_TMPL = """# {headline} (Last updated: {last_updated})
**In 3 bullets**  
- {sum1}  
- {sum2}  
- {sum3}

## Why it matters
{overview}

## Timeline
{timeline}

## Where we are now
- Tech/Product: {tech}
- Ecosystem/Competition: {eco}
- Risks/Unknowns: {risk}

## Watch next
- {next1}
- {next2}

—  
Sources:
{refs}
"""

def render_story_md(story: Dict[str, Any], lang: str = "jp") -> str:
    arts = story["articles"]
    headline = clean_text(story["title"])
    last_updated = story["last_updated_at"]
    # 簡易サマリ（3点）
    s1 = short_summary(arts[0]["title"], arts[0].get("summary",""))
    s2 = short_summary(arts[min(1, len(arts)-1)]["title"], arts[min(1, len(arts)-1)].get("summary","")) if len(arts) >= 2 else s1
    s3 = short_summary(arts[min(2, len(arts)-1)]["title"], arts[min(2, len(arts)-1)].get("summary","")) if len(arts) >= 3 else s2

    # タイムライン
    lines = []
    for a in arts:
        lines.append(f"- {a['published']}: {a['title']}（出典: {a['source']}）\n  - {a['link']}")
    timeline = "\n".join(lines)

    # 出典
    refs = "\n".join([f"- {a['source']}: {a['link']}" for a in arts])

    ctx = dict(
        headline=headline,
        last_updated=last_updated,
        sum1=s1, sum2=s2, sum3=s3,
        overview="このトピックの重要性を一言で。（編集で追記）" if lang=="jp" else "One-liner of significance. (edit later)",
        tech="（編集で追記）" if lang=="jp" else "(edit later)",
        eco="（編集で追記）" if lang=="jp" else "(edit later)",
        risk="（編集で追記）" if lang=="jp" else "(edit later)",
        next1="公式発表/イベント/ベータ配布などの予定" if lang=="jp" else "Upcoming official events/betas",
        next2="規制/競合動向/指標の更新" if lang=="jp" else "Regulatory/competition signals",
        timeline=timeline,
        refs=refs,
    )
    tmpl = JP_TMPL if lang == "jp" else EN_TMPL
    return tmpl.format(**ctx)

def write_markdowns(stories: List[Dict[str, Any]], emit_jp: bool, emit_en: bool) -> None:
    # とりあえず “最新更新日が最も新しい1ストーリー” を出力
    if not stories:
        if emit_jp: JP_MD.write_text("# 本日のAIニュース（下書き）\n\n（該当なし）", encoding="utf-8")
        if emit_en: EN_MD.write_text("# Today’s AI News (Draft)\n\n(None)", encoding="utf-8")
        return

    stories.sort(key=lambda s: s["last_updated_at"], reverse=True)
    top = stories[0]
    if emit_jp:
        JP_MD.parent.mkdir(parents=True, exist_ok=True)
        JP_MD.write_text(render_story_md(top, lang="jp"), encoding="utf-8")
    if emit_en:
        EN_MD.parent.mkdir(parents=True, exist_ok=True)
        EN_MD.write_text(render_story_md(top, lang="en"), encoding="utf-8")

# --------- main ---------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(AUTOMATION_DIR / "config.yml"))
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    feeds: List[str] = cfg.get("feeds", [])
    thr: float = float(cfg.get("similarity_threshold", 0.72))
    per_feed_limit: int = int(cfg.get("per_feed_limit", 10))
    emit_jp: bool = bool(cfg.get("emit_jp", True))
    emit_en: bool = bool(cfg.get("emit_en", False))

    arts = fetch_articles(feeds, per_feed_limit=per_feed_limit)
    DEBUG_DIR.joinpath("raw_articles.json").write_text(json.dumps(arts, ensure_ascii=False, indent=2), encoding="utf-8")

    stories = link_stories(arts, thr=thr)
    DEBUG_DIR.joinpath("stories.json").write_text(json.dumps(stories, ensure_ascii=False, indent=2), encoding="utf-8")

    write_markdowns(stories, emit_jp=emit_jp, emit_en=emit_en)
    print(f"✅ generated: jp={JP_MD.exists()} en={EN_MD.exists()}")

if __name__ == "__main__":
    main()
