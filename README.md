# raindrop-tagger

Raindrop.io のタグなしブックマークを取得し、ページ内容（タイトル・meta description・本文抜粋）を Claude（Haiku）に読ませて自動でタグを生成し、Raindrop.io に書き戻す CLI ツールです。

## できること

- Raindrop.io からタグ未設定のブックマークを収集（全コレクション、または指定コレクションのみ）
- 各ブックマークの URL にアクセスしてページタイトル・description・本文を抽出
- Claude API でページ内容に合ったタグを最大5個生成（日本語/英語は内容に応じて自動選択）
- 生成したタグを Raindrop.io のブックマークに保存
- `--dry-run` でタグを保存せずにプレビューだけ確認可能

## セットアップ

### 1. 依存パッケージのインストール

```bash
# pip install -r requirements.txt
python -m pip install -r requirements.txt
```

### 2. API キーの設定

`.env.example` を `.env` にコピーし、値を設定します。

```bash
cp .env.example .env
```

`.env` の中身:

```env
# Raindrop.io のテストトークン
# 取得方法: https://app.raindrop.io/settings/integrations → "Create test token"
RAINDROP_TOKEN=your_raindrop_test_token_here

# Anthropic API キー
# 取得方法: https://console.anthropic.com/settings/keys
ANTHROPIC_API_KEY=your_anthropic_api_key_here
```

- `RAINDROP_TOKEN`: Raindrop.io の [Settings → Integrations](https://app.raindrop.io/settings/integrations) で「Create test token」から取得します。
- `ANTHROPIC_API_KEY`: [Anthropic Console](https://console.anthropic.com/settings/keys) から取得します。

## 使い方

```bash
# タグなしのブックマークをすべて処理
python tagger.py

# タグを保存せずプレビューだけ（動作確認用）
python tagger.py --dry-run

# 最大10件だけ処理
python tagger.py --limit 10

# タグ付き済みも対象に、既存タグと新タグを並べて比較（保存しない。--dry-run と --limit が必須）
python tagger.py --dry-run --include-tagged --limit 20

# タグ生成モデルを指定（デフォルト: claude-haiku-5-5）。モデル比較に便利
python tagger.py --dry-run --include-tagged --limit 20 --model claude-haiku-5-5

# 特定コレクションのみ処理（コレクションIDを指定）
python tagger.py --collection 12345

# リクエスト間の待機秒数を変更（デフォルト: 0.5秒）
python tagger.py --delay 1.0
```

### オプション一覧

| オプション | 説明 | デフォルト |
|---|---|---|
| `--dry-run` | タグを保存せずプレビューのみ表示 | 無効 |
| `--limit N` | 処理するブックマークの最大数（0=無制限） | 0 |
| `--collection ID` | 対象コレクションID（0=全コレクション） | 0 |
| `--delay N` | リクエスト間の待機秒数 | 0.5 |

初めて使う場合は、まず `--dry-run --limit 5` くらいで動作とタグの質を確認してから本番実行するのがおすすめです。

```bash
python tagger.py --dry-run --limit 5
```

## 実行結果の見方

各ブックマークについて、タイトル・ドメイン・生成されたタグ・保存結果が表示されます。

```
[1] Pythonでデータ分析を始めよう
     example.com
     🏷️  Python, データ分析, pandas, 機械学習, チュートリアル
     ✅ 保存完了
```

最後に成功/失敗/スキップ件数のサマリーが出力されます。

- ✅ 保存完了 … タグをRaindrop.ioに保存
- ❌ 保存失敗 / Claude API エラー … API呼び出しが失敗（失敗件数にカウント）
- ⚠️ タグを生成できませんでした … Claudeがタグを返さなかった（スキップ件数にカウント）

## 注意事項

- `.env` ファイルは `.gitignore` 済みですが、APIキーを含むため取り扱いに注意してください。
- タグ生成には `claude-haiku-5-5` を使用します（`--model` で変更可）。
- Raindrop.io の既存タグを使用頻度の高い順に取得（デフォルト100件、`--vocab-size` で変更、0で無効）してプロンプトに渡し、表記ゆれ（例: Gartner / ガートナー）を抑えます。システムプロンプトはプロンプトキャッシュの対象です。
- ページ取得に失敗したブックマーク（アクセス不可・タイムアウトなど）でも、URLとタイトルのみからタグ生成を試みます。
