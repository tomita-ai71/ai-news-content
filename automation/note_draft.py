#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse, re, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import re

STORAGE = "storage_state.json"
NEW_NOTE_URLS = [
    "https://note.com/notes/new",          # 旧UI
    "https://note.com/new",                # 新UI
    "https://note.com/creation/note",      # 生成系導線
    "https://note.com/creation",           # 生成トップ
]

def read_markdown(md_path: str):
    text = Path(md_path).read_text(encoding="utf-8")
    title = None
    for line in text.splitlines():
        if line.strip().startswith("# "):
            title = re.sub(r"^#\s*", "", line.strip()); break
    return (title or "ストーリーニュース"), text
   
def sanitize_body(text: str) -> str:
    """
    よく混入するターミナル行を除去・整理。
    - 行頭が 'git ', 'echo ', 'ls ', 'cd ', '# '（コメント）など
    - シェル展開っぽい '>>', '~/note-automation', '$ ' を含む行
    - 末尾に連続する空行を圧縮
    """
    lines = []
    for raw in text.splitlines():
        s = raw.rstrip()
        drop = False
        if re.match(r'^\s*(git|echo|ls|cd|pwd|cat|chmod|python3?|pip|brew)\b', s): drop = True
        if re.search(r'(\>\>|\$\s|~/note-automation|^\s*#\s*retrigger\b)', s):   drop = True
        if s in ('d',): drop = True
        if not drop:
            lines.append(s)
    # 末尾の空行を1つに圧縮
    while lines and lines[-1] == '':
        lines.pop()
    lines.append('')  # 末尾にちょうど1つだけ空行
    return '\n'.join(lines)

def accept_banners(page):
    for sel in [
        'button:has-text("同意")','button:has-text("同意して続行")',
        'button:has-text("許可")','button:has-text("OK")',
        'button:has-text("Accept")','button:has-text("Agree")',
        '[aria-label="同意する"]','[aria-label="許可"]'
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible():
                btn.click(); page.wait_for_timeout(300)
        except Exception:
            pass

def ensure_login(context, page):
    page.goto("https://note.com", timeout=60_000)
    page.wait_for_load_state("networkidle")
    accept_banners(page)
    print("👉 初回はブラウザで手動ログインしてください（2FA可）。")
    input("   ログインできたら Enter を押す → セッション保存します… ")
    context.storage_state(path=STORAGE)
    print(f"✅ セッション保存: {STORAGE}")

def see_editor(page) -> bool:
    try:
        ed = page.locator('[contenteditable="true"], div[role="textbox"]').first
        return ed.is_visible()
    except Exception:
        return False

def click_creation_paths(page) -> bool:
    """ホームやヘッダから“新規作成/記事を書く”などを順に試す"""
    accept_banners(page)
    candidates = [
        'a:has-text("投稿")', 'a:has-text("記事")', 'a:has-text("ノート")',
        'a:has-text("書く")', 'button:has-text("投稿")', 'button:has-text("記事")',
        'a[href*="/notes/new"]', 'a[href*="/new"]',
        '[href="/creation"]', '[href="/creation/note"]',
        'a[aria-label*="作成"]', 'button[aria-label*="作成"]'
    ]
    for sel in candidates:
        try:
            el = page.locator(sel).first
            if el.is_visible():
                el.click()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(800)
                if see_editor(page): return True
        except Exception:
            continue
    return False

def go_new_note(page) -> bool:
    """URL直叩き → クリック導線 → 最後は人間アシスト"""
    # 1) 既知URLを順に試す
    for url in NEW_NOTE_URLS:
        try:
            page.goto(url, timeout=60_000)
            page.wait_for_load_state("networkidle")
            accept_banners(page)
            page.wait_for_timeout(1200)
            if see_editor(page): return True
        except Exception:
            continue

    # 2) ホームからの導線クリック
    try:
        page.goto("https://note.com", timeout=60_000)
        page.wait_for_load_state("networkidle")
        accept_banners(page)
        if click_creation_paths(page): return True
    except Exception:
        pass

    # 3) 人間アシスト：ユーザーが手で「新規記事の編集画面」まで開く
    print("🖐 新規ノート画面に自動到達できませんでした。")
    print("   ブラウザで『記事を書く/ノート作成』などから編集画面を開き、エディタが表示されたら Enter を押してください。")
    input("   準備ができたら Enter... ")
    return see_editor(page)

def robust_fill_title(page, title: str):
    sels = [
        'textarea[placeholder*="タイトル"]','input[placeholder*="タイトル"]',
        '[data-testid="title"]','[aria-label*="タイトル"]',
        'header textarea','header input','textarea[name="title"]','input[name="title"]'
    ]
    for sel in sels:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=4000)
            el.click(); el.fill(""); el.type(title, delay=10)
            return True
        except Exception:
            continue
    # 最後の手段：contenteditable の先頭へ h1 を追加
    try:
        page.wait_for_selector('[contenteditable="true"], div[role="textbox"]', timeout=6000)
        page.evaluate("""
            (t)=>{
              const ed=document.querySelector('[contenteditable="true"], div[role="textbox"]');
              if(!ed) return false;
              ed.focus();
              const h=document.createElement('h1'); h.textContent=t;
              ed.prepend(h); return true;
            }
        """, title)
        return True
    except Exception:
        return False

def robust_fill_body(page, body_md: str):
    ed_sel='[contenteditable="true"], div[role="textbox"]'
    ed=None
    try:
        page.wait_for_selector(ed_sel, timeout=8000)
        ed=page.locator(ed_sel).first
    except Exception:
        # iframe 対応
        try:
            for fr in page.frames:
                try:
                    fr.wait_for_selector(ed_sel, timeout=2500)
                    ed=fr.locator(ed_sel).first; break
                except Exception: continue
        except Exception: pass
    if ed is None: return False

    # ① 直接 append
    try:
        page.evaluate("""
            (txt)=>{
              const pick=()=>document.querySelector('[contenteditable="true"], div[role="textbox"]')
                           || (document.querySelector('iframe')?.contentDocument?.querySelector('[contenteditable="true"], div[role="textbox"]'));
              const ed=pick(); if(!ed) return false;
              ed.focus();
              while(ed.firstChild) ed.removeChild(ed.firstChild);
              for(const line of txt.split("\\n")){
                const div=document.createElement('div'); div.textContent=line;
                ed.appendChild(div);
              }
              return true;
            }
        """, body_md)
        return True
    except Exception:
        pass
    # ② execCommand
    try:
        page.evaluate("""
            (txt)=>{
              const pick=()=>document.querySelector('[contenteditable="true"], div[role="textbox"]')
                           || (document.querySelector('iframe')?.contentDocument?.querySelector('[contenteditable="true"], div[role="textbox"]'));
              const ed=pick(); if(!ed) return false;
              ed.focus(); document.execCommand('selectAll', false, null);
              document.execCommand('insertText', false, txt); return true;
            }
        """, body_md)
        return True
    except Exception:
        pass
    # ③ タイプ
    try:
        ed.click()
        page.keyboard.type(body_md[:4000], delay=1)
        return True
    except Exception:
        return False

def dump_debug(page, tag="debug"):
    try:
        Path("debug").mkdir(exist_ok=True)
        Path(f"debug/page_url_{tag}.txt").write_text(page.url, encoding="utf-8")
        Path(f"debug/page_html_{tag}.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=f"debug/screen_{tag}.png", full_page=True)
        print(f"🧪 debug saved under ./debug (tag={tag})")
    except Exception as e:
        print("debug dump failed:", e)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--headless", default="false", choices=["true","false"])
    args = ap.parse_args()

    title, body = read_markdown(args.md)
    sed -i '' '/title, body = read_markdown(args.md)/a\
    body = sanitize_body(body)
' automation/note_draft.py
    if args.title.strip(): title = args.title.strip()
    headless = (args.headless == "true")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            storage_state=STORAGE if Path(STORAGE).exists() else None,
            viewport={"width":1280,"height":900}
        )
        page = context.new_page()

        # ログインが切れていたら最初に取り直す
        page.goto("https://note.com", timeout=60_000)
        page.wait_for_load_state("networkidle")
        accept_banners(page)

        if not go_new_note(page):
            dump_debug(page, "cant_open_editor")
            print("❌ 編集画面に到達できませんでした。画面の導線名が変わっている可能性があります。")
            context.storage_state(path=STORAGE); browser.close(); sys.exit(1)

        ok1 = robust_fill_title(page, title)
        ok2 = robust_fill_body(page, body)
        if not (ok1 and ok2):
            dump_debug(page, "input_fail")
            print("❌ 入力に失敗。debug配下の screen/input_fail.png と page_html を確認してください。")
            context.storage_state(path=STORAGE); browser.close(); sys.exit(1)

        # 明示保存（自動保存でも可）
        for sel in ['text=下書き保存','role=button[name=/下書き|保存/]','button:has-text("下書き")','button:has-text("保存")']:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(): btn.click(); page.wait_for_timeout(600)
                break
            except Exception: pass

        page.screenshot(path="note_draft.png", full_page=True)
        context.storage_state(path=STORAGE)
        browser.close()
        print("✅ 下書き投入完了（スクショ: note_draft.png）")
        print("💡 最終公開は手動でご確認ください。")

chmod +x note_draft.py

cat >> automation/note_draft.py <<'PY'
# --- sanitize stray shell-like lines from markdown body ---
import re

def sanitize_body(text: str) -> str:
    """よく混入するターミナル行/操作ログを除去して本文をクリーンにする。"""
    lines = []
    for raw in text.splitlines():
        s = raw.rstrip()
        drop = False
        # 先頭がシェルコマンドっぽい
        if re.match(r'^\s*(git|echo|ls|cd|pwd|cat|chmod|python3?|pip|brew)\b', s):
            drop = True
        # リダイレクト/パス/コメント合図など
        if re.search(r'(\>\>|\$\s|~/note-automation|^\s*#\s*retrigger\b)', s):
            drop = True
        # 迷子の単独 'd'
        if s == 'd':
            drop = True
        if not drop:
            lines.append(s)
    # 末尾の空行を1つに
    while lines and lines[-1] == '':
        lines.pop()
    lines.append('')
    return '\n'.join(lines)
PY

if __name__ == "__main__":
    main()
