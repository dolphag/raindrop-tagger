#!/usr/bin/env python3
"""
raindrop-tagger: Raindrop.io のブックマークにウェブページ内容を元に自動タグ付けする。

使い方:
    python tagger.py              # タグなしのブックマークをすべて処理
    python tagger.py --dry-run    # タグを保存せずプレビュー
    python tagger.py --limit 10   # 最大10件だけ処理
    python tagger.py --collection 12345  # 特定コレクションのみ処理
    python tagger.py --dry-run --include-tagged --limit 20 --model claude-haiku-5-5
                                  # タグ付き済みも対象に、既存タグと新タグを比較（保存しない）
"""

import json
import os
import re
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

DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_VOCAB_SIZE = 100

# タグ生成のシステムプロンプト（全リクエストで共通 → プロンプトキャッシュで節約）
SYSTEM_PROMPT = """\
あなたはウェブページの内容を分析してタグを付けるアシスタントです。
与えられたウェブページの情報を読み、そのページを最もよく表すタグを生成してください。

ルール:
- タグは最大5個まで
- 日本語または英語（内容に合わせて選択）
- 1〜3語の短い単語・フレーズのみ
- サイト名・媒体名（例: Speaker Deck, note）はタグにしない
- 括弧・中黒などの記号を含むタグは作らない
- カンマ（,）区切りの1行だけで返す。JSON・コードブロック・箇条書き・説明文は禁止
- 本文が読み取れない場合も、タイトルとURLから推測して同じ形式で返す

例:
タイトル: Pythonでデータ分析を始めよう
出力: Python,データ分析,pandas,機械学習,チュートリアル\
"""

VOCAB_PROMPT = """

既存タグ一覧（使用頻度の高い順）:
{vocab}

既存タグの使い方:
- 内容に合う既存タグがあれば、新しい表記を作らず、一覧の表記そのままで使う
  （例: 一覧に Gartner があるなら ガートナー とは書かない）
- 一覧に無い製品名・組織名・技術名などの固有名詞は、新しいタグとして追加してよい
- 一覧にある汎用タグだけで済ませず、内容を特徴づける具体的なタグも含める\
"""


def build_system_prompt(vocabulary: list[str] | None) -> str:
    """既存タグ一覧があればシステムプロンプトに組み込む。"""
    if not vocabulary:
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT + VOCAB_PROMPT.format(vocab=", ".join(vocabulary))


# ──────────────────────────────────────────────
#  Raindrop.io API
# ──────────────────────────────────────────────

def _raindrop_headers() -> dict:
    return {"Authorization": f"Bearer {RAINDROP_TOKEN}"}


def get_untagged_bookmarks(
    collection_id: int = 0,
    page: int = 0,
    per_page: int = 50,
    include_tagged: bool = False,
):
    """タグなしブックマークを取得する。(collection_id=0 → 全コレクション)

    include_tagged=True の場合は絞り込まず、タグ付き済みも含めて取得する。
    """
    params = {"perpage": per_page, "page": page}
    if not include_tagged:
        params["search"] = "notag:true"
    resp = requests.get(
        f"https://api.raindrop.io/rest/v1/raindrops/{collection_id}",
        params=params,
        headers=_raindrop_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", []), data.get("count", 0)


def get_tag_vocabulary(collection_id: int = 0, top_n: int = DEFAULT_VOCAB_SIZE) -> list[str]:
    """既存タグを使用頻度の高い順に top_n 件取得する。(collection_id=0 → 全コレクション)"""
    resp = requests.get(
        f"https://api.raindrop.io/rest/v1/tags/{collection_id}",
        headers=_raindrop_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    items.sort(key=lambda x: x.get("count", 0), reverse=True)
    return [it["_id"] for it in items[:top_n]]


_BAD_TAG_CHARS = set('{}[]"`')
_MAX_TAG_LEN = 40


def parse_tags(raw: str) -> list[str] | None:
    """モデルの出力をタグのリストにする。形式が崩れていれば None を返す。"""
    text = raw.strip()
    fence = re.fullmatch(r"```[a-zA-Z]*\n?(.*?)\n?```", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()

    if text[:1] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if isinstance(data, dict):
            data = data.get("tags")
        if isinstance(data, str):
            data = data.split(",")
        if not isinstance(data, list):
            return None
        items = [str(t) for t in data]
    else:
        # 正常な出力は短い1行。改行や長文は説明文が混じっているとみなす
        if "\n" in text or len(text) > 150:
            return None
        items = text.split(",")

    tags = []
    for t in (i.strip() for i in items):
        if t and len(t) <= _MAX_TAG_LEN and not (_BAD_TAG_CHARS & set(t)):
            tags.append(t)
    return tags


def drop_site_name_tags(tags: list[str], domain: str, vocabulary: list[str] | None) -> list[str]:
    """ドメイン名と一致するタグ（サイト名・媒体名）を除く。既存タグにあるものは残す。"""
    dom = domain.casefold().replace("-", "")
    known = set(vocabulary or [])
    out = []
    for t in tags:
        key = t.casefold().replace(" ", "")
        if len(key) >= 3 and key in dom and t not in known:
            continue
        out.append(t)
    return out


def canonicalize_tags(tags: list[str], vocabulary: list[str] | None) -> list[str]:
    """既存タグと大文字小文字だけ違うものを既存の表記に揃え、重複を除く。"""
    canon = {v.casefold(): v for v in (vocabulary or [])}
    out, seen = [], set()
    for t in tags:
        t = canon.get(t.casefold(), t)
        if t.casefold() not in seen:
            seen.add(t.casefold())
            out.append(t)
    return out


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

        soup = BeautifulSoup(resp.content, "html.parser")
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
    model: str = DEFAULT_MODEL,
    vocabulary: list[str] | None = None,
    domain: str = "",
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

    # Haiku 5.x は思考がデフォルトで有効（max_tokens を消費する）ため、
    # タグ付けでは無効化する。4.x はこれらのパラメータの扱いが異なるので付けない。
    extra = {}
    if model.startswith("claude-haiku-5"):
        extra = {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}

    # 出力形式が崩れた場合は1回だけ再試行し、それでも崩れていれば空を返す
    # （壊れた文字列をタグとして保存しないため）
    for _ in range(2):
        message = client.messages.create(
            model=model,   # バッチタグ付けはコスト効率重視
            max_tokens=128,
            **extra,
            system=[
                {
                    "type": "text",
                    "text": build_system_prompt(vocabulary),
                    "cache_control": {"type": "ephemeral"},  # 全リクエストで再利用
                }
            ],
            messages=[{"role": "user", "content": "\n".join(parts)}],
        )
        raw = next((b.text for b in message.content if b.type == "text"), "")
        tags = parse_tags(raw)
        if tags is not None:
            tags = drop_site_name_tags(tags, domain, vocabulary)
            return canonicalize_tags(tags, vocabulary)[:5]
    return []


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
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"タグ生成に使うモデルID（デフォルト: {DEFAULT_MODEL}）"
    )
    parser.add_argument(
        "--include-tagged", action="store_true",
        help="タグ付き済みも対象にする（比較用。--dry-run と --limit が必須）"
    )
    parser.add_argument(
        "--vocab-size", type=int, default=DEFAULT_VOCAB_SIZE,
        help=f"プロンプトに渡す既存タグの件数（使用頻度順。0=渡さない、デフォルト: {DEFAULT_VOCAB_SIZE}）"
    )
    args = parser.parse_args()

    if args.include_tagged and not (args.dry_run and args.limit):
        parser.error("--include-tagged は --dry-run と --limit を併用してください")

    # 環境変数チェック
    if not RAINDROP_TOKEN:
        print("Error: RAINDROP_TOKEN が設定されていません。.env ファイルを確認してください。")
        sys.exit(1)
    if not ANTHROPIC_API_KEY:
        print("Error: ANTHROPIC_API_KEY が設定されていません。.env ファイルを確認してください。")
        sys.exit(1)

    client = anthropic.Anthropic()

    vocabulary = []
    if args.vocab_size > 0:
        try:
            vocabulary = get_tag_vocabulary(args.collection, args.vocab_size)
            print(f"既存タグ {len(vocabulary)}件をプロンプトに渡します\n")
        except requests.exceptions.RequestException as e:
            print(f"既存タグの取得に失敗しました（渡さずに続行します）: {e}\n")

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
                collection_id=args.collection,
                page=page,
                include_tagged=args.include_tagged,
            )
        except requests.exceptions.RequestException as e:
            print(f"Raindrop.io API エラー: {e}")
            break

        if not items:
            break
        for bm in items:
            # API側の絞り込みが効かない場合に備え、タグ付き済みはここでも除外
            if (bm.get("tags") and not args.include_tagged) or bm["_id"] in seen_ids:
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
    kind = "ブックマーク（タグ付き含む）" if args.include_tagged else "タグなしブックマーク"
    print(f"{kind}: {len(candidates)}件 {target}\n")

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
            tags = generate_tags(
                client, url, effective_title, description, body,
                model=args.model, vocabulary=vocabulary, domain=domain,
            )
        except anthropic.APIError as e:
            print(f"     ❌ Claude API エラー: {e}\n")
            n_failed += 1
            time.sleep(1)
            continue

        if not tags:
            print("     ⚠️ タグを生成できませんでした\n")
            n_skipped += 1
            continue

        if args.include_tagged:
            print(f"     既存: {', '.join(bm.get('tags') or []) or '(なし)'}")
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
