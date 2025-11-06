## Pixloader 超詳細ガイド（人間・エージェント共用）

### 1. システム概要
- Pixloader は Pixiv アカウントのブックマーク一覧を定期的に取得し、画像とメタデータをローカルへ保存して閲覧・管理できるサービス。
- 実行形態は Python 単体または Docker コンテナ。コアプロセスはダウンロードループ、トークン入力サーバ、Flask ビューアの 3 要素。
- 詳細な機械向け仕様は `docs/pixloader_agent_spec.json` に JSON 形式で保存済み。

### 2. 実行環境と依存関係
- Python 3.11 ベース。標準起動コマンドは `python -m app.main`。
- Docker 利用時は `docker-compose.yml` の `pixloader` サービスを参照。`PIXLOADER_HOST_ROOT` をホスト側にマウントしてデータを永続化。
- 主要依存ライブラリは pixivpy3、requests、tenacity、python-dotenv、Flask。全て `requirements.txt` に固定バージョンを記載。

### 3. ディレクトリと主要モジュール
- `app/main.py`: エントリポイント。構成読み込み、トークン取得、同期ループ、ビューア起動を司る。
- `app/config.py`: 環境変数から設定を読み込み、ディレクトリ生成や型バリデーションを行う。
- `app/pixiv_service.py`: Pixiv API との通信。認証、ブックマークページング、画像ダウンロード、タスク展開を担当。
- `app/downloader.py`: `DownloadManager` が並列ダウンロードを調整し、完了後にレジストリへ記録。
- `app/storage.py`: SQLite スキーマと操作。ダウンロード履歴、タグ、評価値、メタデータ同期状態を管理。
- `app/viewer_app.py`: Flask ビューアと REST API。ギャラリー、詳細ビュー、メンテナンス操作、ログ閲覧などを提供。
- `app/maintenance.py`: ファイル検証、ブックマーク検証、最近ブクマダウンロードのバッチ処理。
- `app/token_server.py`: トークン入力用 HTTP サーバ。PKCE でログイン URL を生成し、ブラウザ入力を受け付ける。
- `scripts/pixiv_auth.py`: CLI で Pixiv ログイン・トークン更新を行う補助スクリプト。

### 4. 環境変数と設定
- `PIXIV_REFRESH_TOKEN`: 必須。直接指定しない場合はトークンファイルに保存しておく。
- `PIXLOADER_DOWNLOAD_DIR`: 既定は `./downloads`。起動時にディレクトリが作成され、ここから DB・トークンファイルも派生。
- `PIXLOADER_DB_PATH`: 既定は `<download_dir>/pixloader.db`。SQLite ファイルの保存場所。
- `PIXLOADER_TOKEN_FILE`: 既定は `<download_dir>/refresh_token.txt`。トークンサーバからの受信先。
- `PIXIV_BOOKMARK_RESTRICT`: `public` / `private` / `both`。大文字小文字を無視して評価される。
- `PIXLOADER_MAX_PAGES`: 0 で無制限。ブックマーク API の最大ページ数を制限する場合に利用。
- `PIXLOADER_INTERVAL_SECONDS`: 同期ループの待機秒。0 の場合は 1 回実行のみ。
- `PIXLOADER_CONCURRENCY`: 同時ダウンロード数 (1〜16)。
- `PIXLOADER_TOKEN_PORT`: トークン入力サーバのポート番号。デフォルト 8080。
- `PIXLOADER_ALLOW_PASSWORD_LOGIN`: 現状フロント UI では利用していないが、将来的な ID/PW 入力許可を想定したフラグ。
- `PIXLOADER_ENABLE_VIEWER`: True で Flask ビューアを起動し、ダウンロードループは別スレッドに移行。
- `PIXLOADER_VIEWER_PORT`: ビューアの公開ポート。デフォルト 8081。
- `PIXLOADER_VIEWER_HOST`: ビューアがバインドするホスト名。デフォルトは `0.0.0.0`。
- `PIXLOADER_AUTO_SYNC_ON_START`: True で起動時に即同期を開始。False の場合は手動トリガ待ち。

### 5. 起動から停止までの処理フロー
- 起動時に `configure_logging()` で root logger を INFO ベースに設定し、リングバッファハンドラを追加。
- `Config.load()` が `.env` と環境変数を読み込み、ディレクトリを作成、整数・ブール値の妥当性確認、トークンファイルからのフォールバックを行う。
- リフレッシュトークンが無い場合は `TokenInputServer` を立ち上げ、`http://localhost:<port>/` のフォームでコードまたは URL を貼り付けてもらう。取得できるまでブロッキング。
- 認証成功後に `PixivBookmarkService` と `SyncController` を初期化。
- ビューア有効時は `create_viewer_app()` を呼び出し、ダウンロードループを `threading.Thread` で起動して Flask サーバを `viewer_app.run()` で実行。
- ダウンロードループ `_download_loop()` は stop_event を参照しつつ、各サイクルで以下を実施。
  1. `PixivBookmarkService.authenticate()` で Pixiv API にログイン。
  2. `DownloadRegistry` をコンテキストマネージャで開き、`_backfill_metadata()` を実行。
  3. `DownloadManager.run()` がブックマークを走査して未取得画像をダウンロード。
  4. エラーがあればログに出力し、`SyncController` に記録。
  5. 次サイクルまで `SyncController.wait_for_next_cycle()` あるいは `_sleep_or_exit()` で停止。
- 停止シグナル (SIGINT/SIGTERM) を受け取ると、stop_event をセットし、トークンサーバやダウンロードスレッドを順次クリーンアップ。

### 6. Pixiv API とダウンロード処理
- `PixivBookmarkService.iter_bookmarks()` は restrict モードごとにページングし、`next_url` の `max_bookmark_id` や `offset` を解析して続行。レートリミット検知時は 30 秒スリープ。
- 取得したイラストは `_slugify()` でディレクトリ名を生成し、ページごとに `ImageTask` を構築。単ページ作品は `meta_single_page` を優先、複数ページは `meta_pages` の `original_image_url` を使用。
- `DownloadManager` は `ThreadPoolExecutor` を使用。`_max_workers` の 4 倍まで Future をキューに積み、バースト時に `wait(..., FIRST_COMPLETED)` で順次回収。
- ダウンロード成功後は `DownloadRegistry.record_download()` で DB に upsert。タグは JSON 文字列、ブクマ数などの数値は None 時に 0 として扱う。

### 7. データ永続化とスキーマ
- SQLite DB (`PIXLOADER_DB_PATH`) には `downloads` と `illustration_meta` が存在。初回起動やバージョンアップ時に不足カラムを `ALTER TABLE` で追加。
- `downloads` は (illust_id, page) が主キー。ファイルパス、タイトル、作者、ダウンロード日時、タグ JSON、ブクマ数、閲覧数、R18、AI判定、投稿日時、ブクマ日時、メタデータ同期フラグを保持。
- `illustration_meta` はカスタムタグと単一の総合評価値を保持。`DownloadRegistry.record_download()` 時に存在しなければ `INSERT OR IGNORE` で作成。
- ビューア起動時には `rating_axes` と `illustration_ratings` テーブルをチェックし、既定軸「Star」を作成。任意の軸追加・削除・更新は `/settings/rating-axes` から行える。
- 画像ファイルは `<download_root>/<illust_id>_<タイトルslug>/<illust_id>_p<ページ番号><拡張子>` で保存。ターゲットディレクトリはダウンロード前に `mkdir(parents=True, exist_ok=True)`。

### 8. ビューアアプリの機能
- ルート `/` はギャラリー表示。クエリパラメータでページング、カード表示形式、サイズ、タグ・作者・タイトルフィルタ、R18/AI フラグ、評価条件を指定可能。
- 詳細ページ `/illust/<int:illust_id>` は作品ヘッダ、各ページのファイルパス、評価軸スコアを表示。
- ファイル配信 `/files/<path>` はディレクトリトラバーサルを防ぐため `resolve()` と `relative_to()` を利用。
- API `/api/illust/<id>/meta` は JSON で `rating`, `custom_tags`, `axes` を受け取り、既定軸の上限を超えないようにクリップ。結果として正規化したタグと評価値を返却。
- `/api/logs` はリングバッファから指定件数 (1〜500) のログを返す。各レコードは ISO8601 UTC の timestamp, level, logger name, message を含む。
- `/api/sync/status` は `SyncController` の状態（進行中か、直近サイクル番号、開始/終了時刻、エラー、未同期メタ件数）を返却。
- `/api/sync/start` は手動同期をトリガー。SyncController が存在しない場合は 503。
- `/api/maintenance/*` 系は進行状況を返すステータスと、ファイル検証・ブクマ検証を開始する POST エンドポイントを提供。既に処理中なら HTTP 409。
- `/api/recent/*` は最新ブクマの取得バッチを操作。進行中フラグや最後に取得した作品の概要を保持。

### 9. メンテナンスタスク
- `verify_files`: DB レコードと実ファイルを突き合わせ、欠損時に Pixiv から再ダウンロード。結果は確認件数、欠損数、修復数、失敗数。
- `verify_bookmarks`: Pixiv のブックマーク一覧を再取得し、DB に存在しない illust をダウンロードまたは欠損として記録。
- `fetch_recent_batch`: 最新のブクマから limit 件を取得し、存在しないファイルのみダウンロード。結果に処理件数、ダウンロード数、スキップ数、次回カーソル、最新 illust の概要が含まれる。
- これらの処理はビューアからのリクエストで別スレッドが起動し、共有辞書をロックして進捗を更新。

### 10. スレッド・同期・ロギング
- `DownloadManager` のスレッドプールと `DownloadRegistry` の SQLite 接続は `threading.Lock` で守られ、`check_same_thread=False` を指定している。
- `SyncController` は手動トリガ用の `threading.Event` を持ち、状態アクセスは専用ロックでガード。
- ビューア内のメンテナンス状態辞書は `threading.Lock` または `threading.RLock` で保護。
- ログは Python logging のハンドラでリングバッファに格納され、INFO 以上が標準でコンソール出力される。

### 11. 異常系・エッジケース
- Pixiv API がレートリミットを返した場合は 30 秒待機してリトライし、繰り返し失敗したらエラーログを出してモードを終了。
- トークンサーバ起動時にポートが占有されていると `OSError` が発生し、ログに出力して None を返す。結果として `main` が終了するため、ポート変更が必要。
- メタデータ補完中に作品が削除されている場合は `mark_metadata_synced()` を呼び、再試行対象から外す。
- 既にダウンロード済みでファイルが存在する場合はスキップ、DB のみ存在するときは警告ログを残して再ダウンロード。
- ビューア API 実行中に同期を走らせると 409 または 503 を返して衝突を防ぐ。

### 12. 運用のヒント
- `.env` に最低限 `PIXIV_REFRESH_TOKEN` と `PIXLOADER_HOST_ROOT` を記載すると手動トークン入力を省略可能。
- 大量ダウンロード時は `PIXLOADER_CONCURRENCY` を調整し、ネットワーク帯域と Pixiv API の負荷を考慮。
- メンテナンスを定期運用したい場合は `/api/maintenance/verify-files` をスケジューラから叩くと欠損検知がしやすい。
- ビューアを外部公開する場合は `PIXLOADER_VIEWER_HOST` と Docker port の設定を見直し、ファイアウォール許可を行う。
- 仕様変更や拡張時は `docs/pixloader_agent_spec.json` と Serena メモリ `implementation_specs` の更新を忘れずに行う。

### 13. 参考ドキュメント
- 機械可読仕様: `docs/pixloader_agent_spec.json`
- Serena メモリ: `implementation_specs` (仕様ファイル), `shared_docs` (本ドキュメント)
- README: `README.md`
