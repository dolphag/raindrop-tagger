# raindrop-tagger

Raindrop.io のブックマークを取得し、ページ内容（タイトル・meta description・本文抜粋）を Claude（Haiku）に読ませて自動でタグを生成し、Raindrop.io に書き戻す CLI ツールです。標準ではタグなしのブックマークだけを対象にし、タグ付き済みを含めた全件の再タグ付け（既存タグへのマージ、または置き換え）にも対応しています。

## できること

- Raindrop.io からタグ未設定のブックマークを収集（全コレクション、または指定コレクションのみ）
- 各ブックマークの URL にアクセスしてページタイトル・description・本文を抽出
- Claude API でページ内容に合ったタグを最大5個生成（日本語/英語は内容に応じて自動選択）
- Raindrop.io の既存タグ（使用頻度の高い順）を語彙として渡し、表記ゆれ（例: Gartner / ガートナー）を抑える
- 生成したタグを Raindrop.io のブックマークに保存
- `--dry-run` でタグを保存せずにプレビューだけ確認可能
- `--include-tagged` でタグ付き済みも対象にして、既存タグと新タグを並べて比較（モデルやプロンプトの比較用）
- `--retag-all` でタグ付き済みも含めて全件を再タグ付け（既存タグにマージ）
- `--retag-all --replace` で既存タグを消して付け直し。更新前のタグは自動でバックアップされ、`--restore` で戻せる

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

# タグ付き済みも含めて全件を再タグ付けし、既存タグにマージして保存（実行前に確認あり）
python tagger.py --retag-all

# 件数を絞って試す / 確認を省略 / 中断後に途中から再開（--start は先頭からのスキップ件数）
python tagger.py --retag-all --limit 20
python tagger.py --retag-all --yes
python tagger.py --retag-all --start 300

# 既存タグを消して付け直す（更新前のタグは tag-backup-*.json に自動保存。既存タグは語彙に使わない）
python tagger.py --retag-all --replace --dry-run --limit 20   # まず保存せずに確認
python tagger.py --retag-all --replace

# バックアップのタグに戻す（--dry-run で内容確認のみ）
python tagger.py --restore tag-backup-20261023-091931.json

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
| `--model ID` | タグ生成に使うモデルID | `claude-haiku-5-5` |
| `--vocab-size N` | プロンプトに渡す既存タグの件数（使用頻度順、0=渡さない） | 300 |
| `--include-tagged` | タグ付き済みも対象にして、既存タグと新タグを並べて表示（`--dry-run` と `--limit` が必須） | 無効 |
| `--retag-all` | タグ付き済みも含めて全件を再タグ付けし、既存タグにマージして保存（実行前に確認あり） | 無効 |
| `--replace` | `--retag-all` と併用。既存タグを消して、生成したタグに置き換える | 無効 |
| `--start N` | 先頭から N 件をスキップ（`--retag-all` の中断後の再開用） | 0 |
| `--yes` | `--retag-all` の実行前確認を省略 | 無効 |
| `--restore FILE` | バックアップ JSON のタグに戻す（`--dry-run` で内容確認のみ） | - |

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
- ✅ 変更なし … 保存するタグが既存と同じだったため、更新しなかった（成功件数にカウント）
- ❌ 保存失敗 / Claude API エラー … API呼び出しが失敗（失敗件数にカウント）
- ⚠️ スキップ: （理由） … ページを取得できず、保存済みの概要もないため、タグを生成せずスキップした（スキップ件数にカウント。タグは変更されません）
- ℹ️ （理由）のため、保存済みの概要を使います … ページが使えず、Raindrop の概要でタグを生成した
- ⚠️ タグを生成できませんでした … Claudeがタグを返さなかった、または出力の形式が崩れていて再試行しても直らなかった（スキップ件数にカウント。元のタグは変更されません）

`--retag-all` では、各ブックマークの `既存:` の行に更新前のタグが表示されます。

## 注意事項

- `.env` ファイルは `.gitignore` 済みですが、APIキーを含むため取り扱いに注意してください。
- タグ生成には `claude-haiku-5-5` を使用します（`--model` で変更可）。
- Raindrop.io の既存タグを使用頻度の高い順に取得（デフォルト300件、`--vocab-size` で変更、0で無効）してプロンプトに渡し、表記ゆれ（例: Gartner / ガートナー）や、一般名詞を組み合わせた細かい複合タグ（例: AI＋名詞）の乱造を抑えます。製品名・組織名などの固有名詞は、1件しか使われなくても新しいタグとして付けます。システムプロンプトはプロンプトキャッシュの対象です。
- `--retag-all` は、全件を処理するため時間と API 利用料がかかります。先に `--dry-run --limit 20` などで品質を確認し、件数を `--limit` で絞って試してから全件を実行してください。途中で Ctrl-C で止めた場合は、表示された値を `--start` に指定すると途中から再開できます。
- `--replace` は、対象ブックマークの既存タグをすべて削除します。実行前に `tag-backup-日時.json`（更新前のタグ）が実行したフォルダに作られ、`--restore` で戻せます。ファイルにはブックマークのタイトルと URL が含まれるため、コミットしないでください（`.gitignore` 済み）。復元はバックアップ全体が単位で、個別のタグだけを戻すことはできません。
- `--replace` では、既存タグは作り直す対象なので語彙として使いません。代わりに、実行中に生成したタグから語彙を作ります（25件ごとに更新）。そのため序盤は表記ゆれが出やすく、中断して `--start` で再開した場合は語彙が空から作り直されます。
- ページを取得できない、またはエラー画面だった場合（HTTP エラー・タイムアウト・HTTP 200 で返る「ページが見つかりません」・ボット対策画面・トップページへの転送など）は、Raindrop が保存している概要（excerpt）を代わりに使います。概要も無く情報が足りないブックマークは、**推測でタグを作らずスキップし、既存タグは変更しません**。スキップした URL は、実行の最後に `skipped-日時.txt` へ書き出されます（ブックマークの内容を含むため `.gitignore` 済み）。エラー画面の判定は簡易的なもので、誤ってスキップされることはありますが、その場合もタグは変更されません。
