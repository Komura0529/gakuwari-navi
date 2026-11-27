# Azure 設定・デプロイ手順書（学割ナビ）

このドキュメントは、学割ナビを Azure App Service に公開し、Azure Table Storage /
Blob Storage と連携するまでの手順をまとめたものです。Portal の操作は日本語UI前提で書いています。

> 注意：リソースの作成には Azure サブスクリプションが必要です。学校の実習用サブスクリプションを使い、
> 課金が発生する操作は必ず先生・管理者の指示に従ってください。
> 接続文字列やキーなどの秘密情報は、GitHub やチャットなどに貼らないでください。

---

## 1. リソースグループの作成

1. Azure Portal（https://portal.azure.com）にサインイン
2. 「リソース グループ」→「作成」
3. 名前：`rg-gakuwari`、リージョン：`Japan East` →「確認および作成」→「作成」

## 2. ストレージアカウントの作成（Table / Blob 用）

1. 「ストレージ アカウント」→「作成」
2. リソースグループ：`rg-gakuwari`
3. ストレージアカウント名：`stgakuwari` + 自分だけの数字など（世界で一意な小文字英数字）
4. リージョン：`Japan East`、パフォーマンス：`Standard`、冗長性：`LRS`（実習では最安で十分）
5. 「確認および作成」→「作成」

作成後、アプリ側が初回アクセス時にテーブル（Posts / Comments）と
Blob コンテナー（post-images）を自動作成します。手動作成は不要です。

### 画像の公開方針
- Blob コンテナー `post-images` は **非公開（プライベート）** のままにします
- 画像は Flask の `/media/<参照>` ルート経由でのみ配信されます（直接URLでは見られません）
- 評価項目「Blob Storage から画像を表示」は、この Flask 経由配信で満たします
  （サンプル画像を Blob に入れたい場合は、Portal の「ストレージ ブラウザー」から
  `post-images` コンテナーへアップロードし、投稿データの image を `blob:<ファイル名>` にします）

## 3. App Service の作成

1. 「App Service」→「作成」→「Web アプリ」
2. リソースグループ：`rg-gakuwari`、名前：`gakuwari-navi` + 自分だけの文字列（URLになるため一意に）
3. 公開：`コード`、ランタイムスタック：`Python 3.12`、OS：`Linux`
4. リージョン：`Japan East`
5. 価格プラン：実習用の無料 / 基本プランを選択（学校の指示に従う）
6. 「確認および作成」→「作成」

## 4. App Service の設定

### 4.1 スタートアップコマンド
App Service →「構成」→「全般設定」→ スタートアップ コマンドに以下を設定：

```
gunicorn --bind=0.0.0.0 --timeout 600 app:app
```

### 4.2 環境変数（アプリケーション設定）
App Service →「構成」→「アプリケーション設定」に追加します（値は Portal 上でのみ入力し、Gitには入れません）。

| 名前 | 値 | 備考 |
|---|---|---|
| `SECRET_KEY` | ランダムな長い文字列 | セッション/CSRF用。必ず設定 |
| `AZURE_STORAGE_CONNECTION_STRING` | ストレージの接続文字列 | 方法A（簡易）。ストレージアカウント→「アクセス キー」からコピー |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` | デプロイ時に pip install を実行させる |

#### 方法B：マネージドID（推奨・権限がある場合）
1. App Service →「ID」→ システム割り当てマネージドID を「オン」にして保存
2. ストレージアカウント →「アクセス制御 (IAM)」→ ロールの割り当て追加：
   - `ストレージ テーブル データ共同作成者`
   - `ストレージ BLOB データ共同作成者`
   を App Service のマネージドID に付与
3. 環境変数から `AZURE_STORAGE_CONNECTION_STRING` を削除し、代わりに
   `AZURE_STORAGE_ACCOUNT_NAME` = ストレージアカウント名 を設定

### 4.3 動作モードの確認
デプロイ後、`https://<アプリ名>.azurewebsites.net/health` にアクセスしてください。
このエンドポイントは Table Storage と Blob Storage へ **実際に接続して** 確認します。

- 正常時：`{"status":"ok","storage":"azure","table":"ok","blob":"ok"}`（HTTP 200）
- 接続失敗時：`{"status":"error", ...}`（HTTP 503）。この場合は正常とは表示されません。
  `table` / `blob` のどちらが `error` かを見て、環境変数やロールの設定を確認してください
- `storage` が `local` の場合は環境変数が読み込まれていません（再起動してみてください）

## 5. GitHub 連携（push で自動デプロイ）

1. このプロジェクトを GitHub の Public リポジトリに push する
   （`.env` や接続文字列が含まれていないことを必ず確認）
2. App Service →「デプロイ センター」→ ソース：`GitHub` を選択し、自分のリポジトリと `main` ブランチを連携
3. 連携すると GitHub 側にワークフローとシークレット（発行プロファイル）が自動登録されます

> **【重要】ワークフローの重複に注意**
> デプロイセンターで連携すると、Azure がワークフローを自動生成します
> （例：`.github/workflows/azure-webapps-python.yml`）。
> 本プロジェクト同梱の `.github/workflows/gakuwari-navi-deploy.yml` と両方残すと、
> push のたびにデプロイが二重に走って競合します。**必ずどちらか1つだけ**にしてください。
> - 同梱版を使う場合：自動生成された方を削除し、同梱版の `AZURE_WEBAPP_NAME` を自分のアプリ名に変更
> - 自動生成版を使う場合：同梱版（gakuwari-navi-deploy.yml）を削除

4. 以後は `main` への push のたびに自動でデプロイされます。進捗は GitHub の「Actions」タブで確認できます

## 6. 静的版の公開について

- 静的版は Flask版と同じ App Service から **`/static-demo/`** で公開されます
  （例：`https://<アプリ名>.azurewebsites.net/static-demo/`）。別の App Service は不要です
- 静的版単体で公開したい場合は、`static_site/` フォルダだけを別の App Service にデプロイしても動作します
- 静的版の投稿・コメントはブラウザの LocalStorage に保存され、サーバーには保存されません。
  評価項目 5〜7（Flask / Table / Blob）は Flask版で満たします

## 7. トラブルシューティング

| 症状 | 確認すること |
|---|---|
| 画面が真っ白 / 503 | App Service の「ログ ストリーム」で gunicorn の起動エラーを見る。スタートアップコマンドの設定漏れが多い |
| `storage` が `local` のまま | 環境変数の保存後にアプリを再起動したか。接続文字列のコピーミス（末尾の `;` など） |
| 画像が表示されない | Blob コンテナーが作成されているか。マネージドIDのロール付与漏れ（方法B） |
| 投稿が保存されない | Table Storage への接続エラーがログに出ていないか。`/health` の結果を確認 |
| デプロイが失敗する | GitHub Actions のログを確認。`AZURE_WEBAPP_NAME` とシークレット名の不一致が多い |
