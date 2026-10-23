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
from urllib.parse import urlparse
from collections import Counter
from datetime import datetime

import requests
import anthropic
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

RAINDROP_TOKEN = os.environ.get("RAINDROP_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_VOCAB_SIZE = 300
ERROR_TAG = "[err]"   # ページを取得できなかったブックマークに付けるタグ

# タグ生成のシステムプロンプト（全リクエストで共通 → プロンプトキャッシュで節約）
SYSTEM_PROMPT = """\
あなたはウェブページの内容を分析してタグを付けるアシスタントです。
与えられたウェブページの情報を読み、そのページを最もよく表すタグを生成してください。

ルール:
- タグは最大5個まで
- 日本語または英語（内容に合わせて選択）
- 1〜3語の短い単語・フレーズのみ（1つのタグは25文字以内。タイトルや文をそのままタグにしない）
- サイト名・媒体名（例: Speaker Deck, note）はタグにしない
- 括弧・中黒などの記号を含むタグは作らない
- カンマ（,）区切りの1行だけで返す。JSON・コードブロック・箇条書き・説明文は禁止
- 情報が乏しいときは、確実に言えるタグだけを返す（推測で固有名詞を作らない）

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
- 一覧に無い製品名・組織名・技術名などの固有名詞は、1件しか使われなくても新しいタグとして追加してよい
- 一般名詞を組み合わせた新しい複合語（例: 「AI」＋名詞）は作らない。
  既存タグか、より一般的な語を使い、細かい違いは固有名詞で表す
- 一覧にある汎用タグだけで済ませず、内容を特徴づけるタグも含める\
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
_MAX_TAG_LEN = 25


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


def write_backup(bookmarks: list[dict]) -> str:
    """更新前のタグを JSON に退避し、ファイルパスを返す。(--restore で復元できる)"""
    path = f"tag-backup-{datetime.now():%Y%m%d-%H%M%S}.json"
    items = [
        {"_id": b["_id"], "title": b.get("title", ""), "link": b.get("link", ""),
         "tags": b.get("tags") or []}
        for b in bookmarks
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"created": datetime.now().isoformat(), "items": items},
                  f, ensure_ascii=False, indent=1)
    return path


def restore_from_backup(path: str, delay: float, dry_run: bool) -> None:
    """バックアップ JSON のタグでブックマークのタグを置き換えて復元する。"""
    with open(path, encoding="utf-8") as f:
        items = json.load(f)["items"]
    print(f"{path}: {len(items)}件を復元します{'（DRY RUN）' if dry_run else ''}\n")
    n_ok = n_failed = 0
    for i, it in enumerate(items, 1):
        label = it.get("title") or it.get("link", "")
        print(f"[{i}] {label[:70]}{'…' if len(label) > 70 else ''}")
        print(f"     → {', '.join(it['tags']) or '(タグなし)'}")
        if dry_run:
            n_ok += 1
            continue
        if update_bookmark_tags(it["_id"], it["tags"]):
            n_ok += 1
        else:
            print("     ❌ 失敗")
            n_failed += 1
        time.sleep(delay)
    print(f"\n完了: 成功 {n_ok}件 / 失敗 {n_failed}件")


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


# HTTP 200 で返ってくるエラーページ・ボット対策画面の判定用
_ERROR_TITLE_RE = re.compile(
    r"^\s*(404|403|410|500|503)\b|not found|forbidden|access denied|just a moment"
    r"|attention required|ページが見つかりません|ページは(存在|見つかり)|お探しのページ"
    r"|アクセスが拒否|アクセスできません",
    re.IGNORECASE,
)
_ERROR_BODY_RE = re.compile(
    r"\b404\b|not found|access denied|enable javascript|are you a robot|captcha"
    r"|ページが見つかりません|お探しのページ|見つかりませんでした|JavaScript を?有効"
    r"|ロボットではありません",
    re.IGNORECASE,
)


def detect_page_problem(url: str, final_url: str, title: str, body: str) -> str:
    """取得したページがエラー画面・転送先のトップページ等なら、その理由を返す。正常なら空文字。"""
    if _ERROR_TITLE_RE.search(title):
        return f"エラーページの可能性（タイトル: {title[:30]}）"
    # エラーページは本文が短い。長い記事の本文中の「404」などは誤検知しない
    if len(body) < 800 and _ERROR_BODY_RE.search(body[:300]):
        return "エラーページ・アクセス制限の可能性"
    if urlparse(url).path.strip("/") and not urlparse(final_url).path.strip("/"):
        return "トップページへ転送された（記事が削除された可能性）"
    return ""


def fetch_page_content(url: str) -> tuple[str, str, str, str]:
    """URLからタイトル・概要・本文・問題の理由を取得する。

    問題がなければ4番目は空文字。問題がある場合（取得失敗・エラーページ等）は
    タイトル・概要・本文を空にして、理由を返す。
    """
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

        body = " ".join(soup.get_text(separator=" ", strip=True).split())[:3000]

        problem = detect_page_problem(url, resp.url, title, body)
        if problem:
            return "", "", "", problem
        return title, description, body, ""

    except requests.exceptions.HTTPError as e:
        return "", "", "", f"取得失敗（HTTP {e.response.status_code}）"
    except Exception as e:
        return "", "", "", f"取得失敗（{type(e).__name__}）"


_MIN_BODY_LEN = 100
_MIN_DESC_LEN = 40


def is_informative(description: str, body: str) -> bool:
    """タグ生成に足る情報（本文または概要）があるか。"""
    return len(body) >= _MIN_BODY_LEN or len(description) >= _MIN_DESC_LEN


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
    parser.add_argument(
        "--retag-all", action="store_true",
        help="タグ付き済みも含めて全件を再タグ付けし、既存タグにマージして保存する"
    )
    parser.add_argument(
        "--start", type=int, default=0,
        help="先頭からスキップする件数（--retag-all の中断後の再開用。デフォルト: 0）"
    )
    parser.add_argument(
        "--yes", action="store_true", help="--retag-all の実行前確認を省略する"
    )
    parser.add_argument(
        "--replace", action="store_true",
        help="--retag-all と併用: 既存タグを消して、生成したタグに置き換える"
             "（更新前のタグは tag-backup-*.json に退避）"
    )
    parser.add_argument(
        "--restore", metavar="FILE",
        help="バックアップ JSON のタグに戻す（--dry-run で内容確認のみ）"
    )
    parser.add_argument(
        "--error-tag", default=ERROR_TAG, metavar="TAG",
        help=f"ページを取得できなかったブックマークに付けるタグ（空文字で付けない、デフォルト: {ERROR_TAG}）"
    )
    parser.add_argument(
        "--retry-errors", action="store_true",
        help="エラータグが付いているブックマークだけを再処理する（成功したらエラータグを外す）"
    )
    args = parser.parse_args()

    if args.retry_errors and (args.retag_all or args.include_tagged or args.replace or args.start):
        parser.error("--retry-errors は --retag-all / --include-tagged / --replace / --start と同時に指定できません")
    if args.retry_errors and not args.error_tag:
        parser.error("--retry-errors には --error-tag の指定（空以外）が必要です")
    if args.retag_all and args.include_tagged:
        parser.error("--retag-all と --include-tagged は同時に指定できません")
    if args.include_tagged and not (args.dry_run and args.limit):
        parser.error("--include-tagged は --dry-run と --limit を併用してください")
    if args.start and not args.retag_all:
        parser.error("--start は --retag-all と併用してください")
    if args.replace and not args.retag_all:
        parser.error("--replace は --retag-all と併用してください")
    # 以降、タグ付き済みも取得する動作は include_tagged に集約する
    args.include_tagged = args.include_tagged or args.retag_all or args.retry_errors

    # 環境変数チェック
    if not RAINDROP_TOKEN:
        print("Error: RAINDROP_TOKEN が設定されていません。.env ファイルを確認してください。")
        sys.exit(1)
    if args.restore:
        restore_from_backup(args.restore, args.delay, args.dry_run)
        return
    if not ANTHROPIC_API_KEY:
        print("Error: ANTHROPIC_API_KEY が設定されていません。.env ファイルを確認してください。")
        sys.exit(1)

    client = anthropic.Anthropic()

    vocabulary = []
    run_vocab = Counter()   # --replace 時: 今回の実行で生成したタグの出現数
    if args.replace:
        # 既存タグは作り直す対象なので語彙として使わない。実行中に生成したタグから
        # 語彙を育てて、表記ゆれを抑える（25件ごとに更新）
        if args.vocab_size > 0:
            print("--replace: 既存タグは語彙に使わず、今回生成したタグから語彙を作ります\n")
    elif args.vocab_size > 0:
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
    skipped_pages = []   # (url, 理由, タイトル): 情報不足でスキップしたもの
    n_err_tagged = 0     # エラータグを付けた件数

    print("対象となるブックマークの件数を算出しています...")

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
            if args.retry_errors and args.error_tag not in (bm.get("tags") or []):
                continue
            seen_ids.add(bm["_id"])
            candidates.append(bm)
        if args.limit and len(candidates) >= args.start + args.limit:
            break
        if len(items) < 50:
            break
        page += 1
        print(f"\r  取得中: {len(candidates)}件", end="", flush=True)
        time.sleep(0.5)   # Raindrop API のレート制限（毎分120リクエスト）対策
    if page:
        print()

    candidates = candidates[args.start:]
    if args.limit:
        candidates = candidates[: args.limit]
    target = f"（最大 {args.limit}件）" if args.limit else "（全件）"
    if args.retry_errors:
        kind = f"エラータグ（{args.error_tag}）付きブックマーク"
    elif args.include_tagged:
        kind = "ブックマーク（タグ付き含む）"
    else:
        kind = "タグなしブックマーク"
    print(f"{kind}: {len(candidates)}件 {target}\n")

    if not candidates:
        print("対象のブックマークがありません。")
        return

    if args.retag_all and not args.dry_run and not args.yes and candidates:
        if args.replace:
            print(f"{len(candidates)}件の既存タグを削除し、生成したタグに置き換えます。")
            print("（更新前のタグは tag-backup-*.json に保存します）")
        else:
            print(f"{len(candidates)}件のタグ付けを行い、既存タグにマージして保存します。")
        if input("続行しますか？ (y/N): ").strip().lower() != "y":
            print("中止しました。")
            return
        print()

    if args.replace and not args.dry_run:
        print(f"バックアップを保存しました: {write_backup(candidates)}\n")

    if args.retag_all:
        print("タグ付き済みを含む全件の再タグ付けを開始します...\n")
    elif args.retry_errors:
        print("エラータグ付きブックマークの再処理を開始します...\n")
    elif args.include_tagged:
        print("タグ付き済みを含めて、比較用のタグ生成を開始します...\n")
    else:
        print("タグなしブックマークの自動タグ付けを開始します...\n")

    # ── 各ブックマークを処理 ──
    try:
        for bm in candidates:
            bm_id = bm["_id"]
            title = bm.get("title", "")
            url = bm.get("link", "")
            domain = bm.get("domain", "")
            total_done += 1

            label = title or url
            print(f"[{args.start + total_done}] {label[:70]}{'…' if len(label) > 70 else ''}")
            if domain:
                print(f"     {domain}")

            # ウェブページ取得
            page_title, description, body, problem = fetch_page_content(url)
            effective_title = page_title or title

            # ページが使えない／概要が無いときは、Raindrop が保存している概要で補う
            excerpt = (bm.get("excerpt") or "").strip()
            if problem or not description:
                description = excerpt

            # 情報が足りなければ推測でタグを作らず、スキップ（既存タグは変更しない）
            if not is_informative(description, body):
                reason = problem or "ページの内容が少なく、保存済みの概要もない"
                skipped_pages.append((url, reason, title))
                n_skipped += 1
                existing = bm.get("tags") or []
                # ページの問題（取得失敗・エラー画面等）には、エラータグだけを付ける。
                # 既存タグは変更しない。内容が少ないだけの場合は何も変更しない
                if problem and args.error_tag and args.error_tag not in existing:
                    if args.dry_run:
                        print(f"     ⚠️ スキップ: {reason}（{args.error_tag} を付ける予定）\n")
                    elif update_bookmark_tags(bm_id, merge_tags(existing, [args.error_tag])):
                        print(f"     ⚠️ スキップ: {reason}（{args.error_tag} を付けました）\n")
                        n_err_tagged += 1
                    else:
                        print(f"     ⚠️ スキップ: {reason}（{args.error_tag} の付与に失敗）\n")
                        n_failed += 1
                else:
                    print(f"     ⚠️ スキップ: {reason}（タグは変更しません）\n")
                time.sleep(args.delay)
                continue
            if problem:
                print(f"     ℹ️ {problem}のため、保存済みの概要を使います")

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

            if args.replace and args.vocab_size > 0:
                run_vocab.update(tags)
                if total_done % 25 == 0:
                    vocabulary = [t for t, _ in run_vocab.most_common(args.vocab_size)]

            # ページに問題があった場合はエラータグも付ける。問題がなければ、
            # 以前のエラーで付いたままのエラータグは外す
            save_tags = tags
            if problem and args.error_tag:
                save_tags = tags + [args.error_tag]
                print(f"     ⚠️ {args.error_tag} も付けます")

            # Raindrop.io に保存
            if not args.dry_run:
                existing = bm.get("tags") or []
                base = existing if problem else [t for t in existing if t != args.error_tag]
                if args.replace:
                    merged = save_tags
                    unchanged = {t.casefold() for t in merged} == {t.casefold() for t in existing}
                else:
                    merged = merge_tags(base, save_tags)
                    unchanged = merged == existing
                if unchanged:
                    print("     ✅ 変更なし\n")
                    n_success += 1
                    time.sleep(args.delay)
                    continue
                ok = update_bookmark_tags(bm_id, merged)
                if ok:
                    print("     ✅ 保存完了\n")
                    n_success += 1
                    if problem and args.error_tag:
                        n_err_tagged += 1
                else:
                    print("     ❌ 保存失敗\n")
                    n_failed += 1
            else:
                print("     [DRY RUN] スキップ\n")
                n_success += 1

            time.sleep(args.delay)
    except KeyboardInterrupt:
        print(f"\n中断しました。再開するには --start {args.start + total_done - 1} を指定してください（他のオプションは同じものを指定）。")

    print("=" * 50)
    print(
        f"完了: 成功 {n_success}件 / 失敗 {n_failed}件 / スキップ {n_skipped}件"
    )
    if n_err_tagged:
        print(f"エラータグ {args.error_tag} を付けた件数: {n_err_tagged}件")
    if skipped_pages:
        path = f"skipped-{datetime.now():%Y%m%d-%H%M%S}.txt"
        with open(path, "w", encoding="utf-8") as f:
            for u, reason, t in skipped_pages:
                f.write(f"{u}\t{reason}\t{t}\n")
        print(f"情報不足でスキップした{len(skipped_pages)}件の一覧: {path}")


if __name__ == "__main__":
    main()
