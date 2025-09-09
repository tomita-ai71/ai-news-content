#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
X→（続報が付いたら）長文化：JP=Note, EN=Reddit/Medium
- 収集: RSS
- クラスタ: MiniLM 重心 cosine
- 状態: automation/state.json に永続化
- 出力:
  jp/x_first.txt, en/x_first.txt
  jp/input.md (Note下書き), en/longform.md (Reddit/Medium下書き)
"""
from __future__ import annotations
# 先頭あたり
import argparse, datetime as dt, json, textwrap
from pathlib import Path
from typing import Any, Dict, List
import feedparser
import numpy as np
from sentence_transformers import SentenceTransformer
import re   # ← 追加
from transformers import pipeline

ROOT = Path(__file__).resolve().parents[1]
CFG  = ROOT / "automation" / "config.yml"
STATE= ROOT / "automation" / "state.json"

JP_MD = ROOT / "jp" / "input.md"
EN_MD = ROOT / "en" / "longform.md"
JP_X  = ROOT / "jp" / "x_first.txt"
EN_X  = ROOT / "en" / "x_first.txt"

# --- 翻訳：英語→日本語（短文向け） ---
_translator = None
def ja_translate(text: str) -> str:
    """必要な時だけ初期化して英→日翻訳。失敗時は原文返し。"""
    global _translator
    if not text:
        return text
    try:
        if _translator is None:
            from transformers import pipeline  # 念のため局所importでもOK
            _translator = pipeline("translation", model="Helsinki-NLP/opus-mt-en-jap")
        t = text.strip()
        if len(t) > 220:  # 極端に長いタイトルは切り詰め
            t = t[:200] + "..."
        return _translator(t, max_length=256)[0]["translation_text"]
    except Exception:
        return text

def looks_english(s: str) -> bool:
    """英文字比率でざっくり英語判定（URL/記号は除外）。"""
    import re
    core = re.sub(r'https?://\S+|[^A-Za-z ]', '', s or "")
    letters = sum(c.isalpha() for c in core)
    return letters > 0 and (letters / max(1, len(core))) > 0.5

def load_cfg() -> Dict[str,Any]:
    import yaml
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def iso_date(entry) -> str:
    for k in ("published_parsed","updated_parsed"):
        t = getattr(entry, k, None)
        if t: return dt.date(t.tm_year, t.tm_mon, t.tm_mday).isoformat()
    return dt.date.today().isoformat()

def collect(feeds: List[str], max_items: int) -> List[Dict[str,Any]]:
    rows=[]
    for url in feeds:
        d = feedparser.parse(url)
        for e in d.entries[:max_items]:
            rows.append({
                "title": getattr(e, "title", "").strip(),
                "link": getattr(e, "link", ""),
                "date": iso_date(e),
                "source": d.feed.get("title", url),
            })
    # ざっくり重複排除（title+date）
    uniq={}
    for r in rows:
        k=(r["title"], r["date"])
        if k not in uniq: uniq[k]=r
    return list(uniq.values())

def load_state() -> Dict[str,Any]:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"stories": []}  # stories: [{id, centroid, items:[{title,link,date,source}], locale}]

def save_state(s: Dict[str,Any]):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

def cluster_incremental(locale:str, items:List[Dict[str,Any]], thr:float, state:Dict[str,Any]) -> Dict[str,Any]:
    """既存stateに増分吸着。centroidは正規化済みベクトルの平均。"""
    model = SentenceTransformer("all-MiniLM-L6-v2")
    vecs = model.encode([x["title"] for x in items], normalize_embeddings=True)

    # 該当ロケールの既存
    stories = [s for s in state["stories"] if s.get("locale")==locale]
    def cos(a,b): return float(np.dot(np.array(a), np.array(b)))

    # index無しのシンプル探索（件数少なので十分）
    used=[]
    for i, it in enumerate(items):
        v = vecs[i].tolist()
        # 既存候補
        sid=None; best=-1.0
        for st in stories:
            c = cos(v, st["centroid"])
            if c>best: sid=st["id"]; best=c
        if best>=thr and sid is not None:
            # 既存に追加（同リンクはスキップ）
            st=[s for s in state["stories"] if s["id"]==sid][0]
            if all(it["link"]!=x["link"] for x in st["items"]):
                st["items"].append(it)
                # centroid再計算（単純平均）
                arr=np.array(st["centroid"])* (len(st["items"])-1)
                arr=(arr+np.array(v))/len(st["items"])
                st["centroid"]= (arr/np.linalg.norm(arr)).tolist()
        else:
            # 新規ストーリー
            new_id = (max([s["id"] for s in state["stories"]] or [0])+1)
            state["stories"].append({
                "id": new_id, "locale": locale,
                "centroid": v, "items": [it]
            })
    return state

def pick_first_reports(state:Dict[str,Any], locale:str) -> List[Dict[str,Any]]:
    """itemsが1件だけ＝初報（未長文化）を抽出（今日追加分に限定しない簡易版）"""
    out=[]
    for st in state["stories"]:
        if st["locale"]!=locale: continue
        if len(st["items"])==1: out.append(st)
    # 新しい順
    out.sort(key=lambda s: s["items"][0]["date"], reverse=True)
    return out

def pick_longform_targets(state:Dict[str,Any], locale:str, limit:int) -> List[Dict[str,Any]]:
    """itemsが2件以上＝続報つき → 長文化対象"""
    cand=[s for s in state["stories"] if s["locale"]==locale and len(s["items"])>=2]
    # 最新更新順
    def last_date(s): return max(x["date"] for x in s["items"])
    cand.sort(key=lambda s: last_date(s), reverse=True)
    return cand[:limit]

def render_longform_md(story:Dict[str,Any], locale:str) -> str:
    items=sorted(story["items"], key=lambda x: x["date"])
    today=dt.date.today().isoformat()
    head=items[-1]
    if locale=="jp":
        lines=[f"# AIニュースストーリー（最終更新: {today}）","",
               "**3行要約**",
               f"- {head['title']}（{head['source']}）",
               "", "## 全体像（なぜ重要か）",
               "初報と続報をひとつの物語として整理しました。", "",
               "## タイムライン"]
        for a in items:
            lines.append(f"- {a['date']}: {a['title']}（出典: {a['source']}）[{a['link']}]")
        lines += ["", "## いま時点の理解", "- 事実と公式発表を中心に速報整理。", "",
                  "## 次の注目ポイント", "- 製品公開・規制・公式イベントの続報。", "",
                  "—", "出典まとめ：各タイムラインのリンク参照"]
    else:
        lines=[f"# AI News Story (Last updated: {today})","",
               "**In 3 bullets**",
               f"- {head['title']} ({head['source']})",
               "", "## Overview",
               "We merged the first report and follow-ups into one storyline.", "",
               "## Timeline"]
        for a in items:
            lines.append(f"- {a['date']}: {a['title']} (source: {a['source']}) [{a['link']}]")
        lines += ["", "## Current understanding", "- Factual summary from official sources.", "",
                  "## What to watch next", "- Releases, regulations, and official events.", "",
                  "—", "Sources: see links above."]
    return "\n".join(lines)

# 置換：write_file 関数
def write_file(path: Path, text: str):
    """
    生成物をファイルへ書き出す前に、制御用コメントやノイズを根治的に除去。
    - <!-- retrigger --> を完全除去
    - 3行以上の連続空行を2行に圧縮
    - 末尾の空白/改行を整形（最後は改行1つ）
    """
    clean = re.sub(r'<!--\s*retrigger\s*-->\s*', '', text)
    clean = re.sub(r'(?:\n[ \t]*){3,}', '\n\n', clean)  # 空行3+ → 2
    clean = clean.rstrip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(clean, encoding="utf-8")

def main():
    import yaml
    cfg=load_cfg()
    ap=argparse.ArgumentParser()
    ap.add_argument("--only", choices=["jp","en","both"], default="both")
    args=ap.parse_args()

    state=load_state()

    # JP処理
    if args.only in ("jp","both"):
        jp_items = collect(cfg["jp"]["feeds"], cfg["jp"]["max_items_per_feed"])
        state = cluster_incremental("jp", jp_items, float(cfg["jp"]["min_similarity"]), state)
        # 初報→X
        first = pick_first_reports(state, "jp")
        if first:
            # 最新1件のみ提案（多いとXが雑音になるため）
            st = first[0]
            a = st["items"][0]
            jp_x = f"【速報】{a['title']}｜出典: {a['source']}｜{a['link']}"
            write_file(JP_X, jp_x)
        # 続報→Note
        targets = pick_longform_targets(state, "jp", cfg["jp"]["max_longform_per_run"])
        if targets:
            md = render_longform_md(targets[0], "jp")
            write_file(JP_MD, md)

    # EN処理
    if args.only in ("en","both"):
        en_items = collect(cfg["en"]["feeds"], cfg["en"]["max_items_per_feed"])
        state = cluster_incremental("en", en_items, float(cfg["en"]["min_similarity"]), state)
        # 初報→X
        first = pick_first_reports(state, "en")
        if first:
            st = first[0]
            a = st["items"][0]
            en_x = f"[Breaking] {a['title']} | source: {a['source']} | {a['link']}"
            write_file(EN_X, en_x)
        # 続報→Reddit/Medium
        targets = pick_longform_targets(state, "en", cfg["en"]["max_longform_per_run"])
        if targets:
            md = render_longform_md(targets[0], "en")
            write_file(EN_MD, md)

    save_state(state)
    print("✅ build_story_md: files generated.")

if __name__ == "__main__":
    main()
