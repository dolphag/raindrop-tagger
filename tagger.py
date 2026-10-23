#!/usr/bin/env python3
"""
raindrop-tagger: Raindrop.io のブックマークにウェブページ内容を元に自動タグ付けする。

使い方:
    python tagger.py              # タグなしのブックマークをすべて処理
    python tagger.py --dry-run    # タグを保存せずプレビュー
    python tagger.py --limit 10   # 最大10件だけ処理
    python tagger.py --collection 12345  # 特定コレクションのみ処理
"""

import os
import sys
import time
import argparse

import requests
import anthropic
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

RAINDROP_TOKEN = os.environ.get("RAINDROP_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# タグ生成のシステムプロンプト（全リクエストで共通 → プロンプトキャッシュで節約）
SYSTEM_PROMPT = """\
あなたはウェブページの内容を分析してタグを付けるアシスタントです。
与えられたウェブページの情報を読み、そのページを最もよく表すタグを生成してください。

ルール:
- タグは最大5個まで
- 日本語または英語（内容に合わせて選択）
- 1〜3語の短い単語・フレーズのみ
- カンマ（,）区切りで返す
- タグ以外は何も返さない（説明文や記号は不要）

例:
タイトル: Pythonでデータ分析を始めよう
出力: Python,データ分析,pandas,機械学習,チュートリアル\
"""


# ──────────────────────────────────────────────
#  Raindrop.io API
# ──────────────────────────────────────────────

def _raindrop_headers() -> dict:
    return {"Authorization": f"Bearer {RAINDROP_TOKEN}"}


def get_untagged_bookmarks(collection_id: int = 0, page: int = 0, per_page: int = 50):
    """タグなしブックマークを取得する。(collection_id=0 → 全コレクション)"""
    resp = requests.get(
        f"https://api.raindrop.io/rest/v1/raindrops/{collection_id}",
        params={"search": "notag:true", "perpage": per_page, "page": page},
        headers=_raindrop_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", []), data.get("count", 0)


def merge_tags(existing: list[str], new: list[str]) -> list[str]:
    """既存タグを保持したまま新規タグを追加する（大文字小文字を無視して重複除去）。"""
    merged = list(existing)
    seen = {t.lower() for t in existing}
    for t in new:
        if t.lower() not in seen:
            merged.append(t)
            seen.add(t.lower())
    return merged


def update_bookmark_tags(bookmark_id: int, tags: list[str]) -> bool:
    """ブックマークのタグを更新する。tags は最終的な全タグ（既存タグ込み）。"""
    resp = requests.put(
        f"https://api.raindrop.io/rest/v1/raindrop/{bookmark_id}",
        json={"tags": tags},
        headers={**_raindrop_headers(), "Content-Type": "application/json"},
        timeout=15,
    )
    return resp.ok


# ──────────────────────────────────────────────
#  ウェブページ取得
# ──────────────────────────────────────────────

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_page_content(url: str) -> tuple[str, str, str]:
    """URLからタイトル・概要・本文を取得する。失敗時は空文字を返す。"""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _BROWSER_UA},
            timeout=15,
            allow_redirects=True,
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        title = soup.title.get_text(strip=True) if soup.title else ""

        meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
            "meta", attrs={"property": "og:description"}
        )
        description = (meta.get("content", "") or "").strip() if meta else ""

        body = " ".join(soup.get_text(separator=" ", strip=True).split())
        return title, description, body[:3000]

    except Exception:
        return "", "", ""


# ──────────────────────────────────────────────
#  Claude でタグ生成
# ──────────────────────────────────────────────

def generate_tags(
    client: anthropic.Anthropic,
    url: str,
    title: str,
    description: str,
    content: str,
) -> list[str]:
    """Claude API を使ってタグを生成する。システムプロンプトをキャッシュ。"""
    parts = []
    if title:
        parts.append(f"タイトル: {title}")
    parts.append(f"URL: {url}")
    if description:
        parts.append(f"概要: {description}")
    if content:
        parts.append(f"本文（抜粋）: {content[:2000]}")

    message = client.messages.create(
        model="claude-haiku-4-5",   # バッチタグ付けはコスト効率重視
        max_tokens=128,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # 全リクエストで再利用
            }
        ],
        messages=[{"role": "user", "content": "\n".join(parts)}],
    )

    raw = message.content[0].text.strip()
    tags = [t.strip() for t in raw.split(",") if t.strip()]
    return tags[:5]


# ──────────────────────────────────────────────
#  メイン処理
# ──────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Raindrop.io ブックマーク自動タグ付けツール"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="タグを保存せずプレビューのみ"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="処理するブックマークの最大数（0=無制限）"
    )
    parser.add_argument(
        "--collection", type=int, default=0,
        help="対象コレクションID（0=全コレクション）"
    )
    parser.add_argument(
        "--delay", type=float, default=0.5,
        help="リクエスト間の待機秒数（デフォルト: 0.5）"
    )
    args = parser.parse_args()

    # 環境変数チェック
    if not RAINDROP_TOKEN:
        print("Error: RAINDROP_TOKEN が設定されていません。.env ファイルを確認してください。")
        sys.exit(1)
    if not ANTHROPIC_API_KEY:
        print("Error: ANTHROPIC_API_KEY が設定されていません。.env ファイルを確認してください。")
        sys.exit(1)

    client = anthropic.Anthropic()

    if args.dry_run:
        print("[DRY RUN モード] タグは保存されません\n")

    total_done = 0
    n_success = 0
    n_failed = 0
    n_skipped = 0

    print("タグなしブックマークの自動タグ付けを開始します...\n")

    # 処理前に対象を全ページ分集める。処理中にタグが付くと検索結果が
    # 詰まってページ送りでずれるため、取得と更新を分離する。
    candidates = []
    seen_ids = set()
    page = 0
    while True:
        try:
            items, total_count = get_untagged_bookmarks(
                collection_id=args.collection, page=page
            )
        except requests.exceptions.RequestException as e:
            print(f"Raindrop.io API エラー: {e}")
            break

        if not items:
            break
        for bm in items:
            # API側の絞り込みが効かない場合に備え、タグ付き済みはここでも除外
            if bm.get("tags") or bm["_id"] in seen_ids:
                continue
            seen_ids.add(bm["_id"])
            candidates.append(bm)
        if args.limit and len(candidates) >= args.limit:
            break
        if len(items) < 50:
            break
        page += 1

    if args.limit:
        candidates = candidates[: args.limit]
    target = f"（最大 {args.limit}件）" if args.limit else "（全件）"
    print(f"タグなしブックマーク: {len(candidates)}件 {target}\n")

    # ── 各ブックマークを処理 ──
    for bm in candidates:
        bm_id = bm["_id"]
        title = bm.get("title", "")
        url = bm.get("link", "")
        domain = bm.get("domain", "")
        total_done += 1

        label = title or url
        print(f"[{total_done}] {label[:70]}{'…' if len(label) > 70 else ''}")
        if domain:
            print(f"     {domain}")

        # ウェブページ取得
        page_title, description, body = fetch_page_content(url)
        effective_title = page_title or title

        # Claude でタグ生成
        try:
            tags = generate_tags(client, url, effective_title, description, body)
        except anthropic.APIError as e:
            print(f"     ❌ Claude API エラー: {e}\n")
            n_failed += 1
            time.sleep(1)
            continue

        if not tags:
            print("     ⚠️ タグを生成できませんでした\n")
            n_skipped += 1
            continue

        print(f"     🏷️  {', '.join(tags)}")

        # Raindrop.io に保存
        if not args.dry_run:
            ok = update_bookmark_tags(
                bm_id, merge_tags(bm.get("tags") or [], tags)
            )
            if ok:
                print("     ✅ 保存完了\n")
                n_success += 1
            else:
                print("     ❌ 保存失敗\n")
                n_failed += 1
        else:
            print("     [DRY RUN] スキップ\n")
            n_success += 1

        time.sleep(args.delay)

    print("=" * 50)
    print(
        f"完了: 成功 {n_success}件 / 失敗 {n_failed}件 / スキップ {n_skipped}件"
    )


if __name__ == "__main__":
    main()
