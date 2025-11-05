# Pixloader

Pixloader は、Pixiv のブックマーク一覧を定期的に取得し、画像ファイルをローカルへ自動保存するための軽量サービスです。Docker コンテナとして動作するように設計されており、ボリュームをマウントするだけで画像やダウンロード履歴を永続化できます。

## 主な機能
- Pixiv の App-API を利用したリフレッシュトークン認証
- ブックマーク済みイラストの一括ダウンロード（公開／非公開を選択可能）
- 複数枚イラストに対応し、スレッドプールで並列ダウンロード
- SQLite によるダウンロード済み判定（重複ダウンロードを防止）
- 任意の間隔でのリピート実行（常駐運用向け）
- オプションでダウンロード済みイラストをブラウザから確認できるビューアを提供

## 必要なもの
- Python 3.11 以上（Docker イメージに含まれます）
- Pixiv のリフレッシュトークン  
  ※ `pixivpy` 付属スクリプトなどで取得できます。例: `python -m pixivpy3 -t your_username your_password`
- Docker / Docker Compose

### リフレッシュトークンの取得手順（ブラウザ利用）
Pixloader では Selenium を使ったトークン取得を同梱していないため、公式 OAuth フローをブラウザで踏むスクリプトを利用するのが簡単です。以下は Windows PowerShell での例ですが、macOS / Linux でもほぼ同じ手順で実行できます。

```powershell
# 1. スクリプトをダウンロード
mkdir .\scripts -Force
Invoke-WebRequest `
  -Uri https://gist.githubusercontent.com/ZipFile/c9ebedb224406f4f11845ab700124362/raw/pixiv_auth.py `
  -OutFile .\scripts\pixiv_auth.py

# 2. 依存をインストール
py -m pip install --user requests

# 3. トークン取得
python .\scripts\pixiv_auth.py login
```

実行すると既定ブラウザで Pixiv のログイン画面が開きます。ログイン後、URL に `...?code=XXXXXXXX` が表示されるので `XXXXXXXX` 部分を PowerShell に貼り付けると、ターミナルに `refresh_token: ...` が表示されます。この値を `.env` の `PIXIV_REFRESH_TOKEN` に設定するか、Pixloader のセットアップページで貼り付けてください。

#### 使いやすくラップしたミニサービス例
最小限の Flask アプリを作成してブラウザ経由でトークンを取得することもできます。以下の内容を `scripts/token_helper.py` に保存し、`python scripts/token_helper.py` と実行すると `http://localhost:5000/` でフォームが使えます。

```python
from __future__ import annotations

from flask import Flask, redirect, render_template_string, request
import requests
from urllib.parse import urlencode

APP = Flask(__name__)

LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
AUTH_TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"

TEMPLATE = """
<!doctype html>
<title>Pixiv Refresh Token Helper</title>
<h1>Pixiv Refresh Token Helper</h1>
{% if token %}
  <p>Refresh token:</p>
  <pre>{{ token }}</pre>
  <p>`.env` の <code>PIXIV_REFRESH_TOKEN</code> にコピーしてください。</p>
{% elif code %}
  <p>コードを取得しました。トークン発行中...</p>
{% else %}
  <p><a href="{{ auth_url }}">Pixiv にログインしてコードを取得</a></p>
{% endif %}
"""


@APP.route("/")
def index():
    code = request.args.get("code")
    if not code:
        params = {
            "code_challenge": "",
            "code_challenge_method": "plain",
            "client": "pixiv-android",
        }
        auth_url = f"{LOGIN_URL}?{urlencode(params)}"
        return render_template_string(TEMPLATE, code=None, token=None, auth_url=auth_url)

    response = requests.post(
        AUTH_TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "code_verifier": "",
            "grant_type": "authorization_code",
            "include_policy": "true",
            "redirect_uri": REDIRECT_URI,
        },
        headers={"User-Agent": "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"},
        timeout=30,
    )
    json_data = response.json()
    token = json_data.get("refresh_token")
    return render_template_string(TEMPLATE, code=code, token=token, auth_url=None)


if __name__ == "__main__":
    APP.run(port=5000)
```

上記は ZipFile 氏のスクリプトを簡易的にラップしたものです。自分でホストする際は HTTPS 化やアクセス制限など必要に応じて調整してください。

## クイックスタート（Docker Compose）
1. 必要に応じて `.env` を作成し、取得したトークンや設定値を記入します（後述のブラウザ入力機能を使う場合、ここで `PIXIV_REFRESH_TOKEN` を書かなくても大丈夫です）。

   ```env
   # 例: 非公開ブックマークを対象にする
   PIXIV_BOOKMARK_RESTRICT=private
   # 例: 1 時間ごとに再実行
   PIXLOADER_INTERVAL_SECONDS=3600
   # 例: 保存先を D:\\Pixloader-image にする
   PIXLOADER_HOST_ROOT=D:\\Pixloader-image
   ```

2. コンテナを起動します。

   ```bash
   docker compose up -d
   ```

   既定では `./data` ディレクトリがコンテナ内 `/data` にマウントされ、以下が保存されます。

   - `downloads/` : ダウンロードした画像ファイル
   - `pixloader.db` : ダウンロード履歴(SQLite)

3. 初回起動時に `http://localhost:8080/` を開き、ブラウザのセットアップページでリフレッシュトークンを登録します。既に取得済みのトークンを貼り付けるか、`PIXLOADER_ALLOW_PASSWORD_LOGIN=true` を設定していれば Pixiv のログイン情報を入力してトークンを生成できます（取得したトークンは `data/refresh_token.txt` に保存され、次回以降は自動で読み込まれます）。アカウントで 2 段階認証を有効にしている場合は、Pixiv から届く 6 桁の認証コードを入力することでログインできます。

4. ビューア機能を利用したい場合は `.env` に `PIXLOADER_ENABLE_VIEWER=true` を追加し、`docker compose up -d` 後に `http://localhost:41412/` を開いてください（ポートは `PIXLOADER_VIEWER_PORT` で変更できます）。ダウンロード済みの作品サムネイル・ページ一覧がブラウザから確認できます。

   #### ビューアの主なアップデート

   - AI生成フラグに対応し、AI作品のみ/除外のフィルタおよびカード上のAIバッジ表示が可能になりました。
   - 表示形式を「文字のみ / 小 / 中 / 大 / 特大 / 画像のみ」から切り替えられます。
   - カード内のタグまたは作者名をクリックすると、フィルタフォームへ自動入力され素早く絞り込みできます。
   - 一覧画面で独自タグや評価をインライン編集すると、カードの表示内容も即座に同期されます。
   - モバイル表示を最適化し、スマートフォンでも閲覧・編集しやすくなりました。

5. ログを確認する場合は次のコマンドを利用してください。

   ```bash
   docker compose logs -f
   ```

## 環境変数

| 変数名 | 既定値 | 説明 |
| --- | --- | --- |
| `PIXIV_REFRESH_TOKEN` | **必須** | Pixiv のリフレッシュトークン。.env で設定するかブラウザ入力で登録します。 |
| `PIXIV_BOOKMARK_RESTRICT` | `public` | `public` または `private` を指定。 |
| `PIXLOADER_DOWNLOAD_DIR` | `/data` (Docker 時) | 画像を保存するディレクトリ。通常は変更不要です。 |
| `PIXLOADER_DB_PATH` | `<download_dir>/pixloader.db` | ダウンロード履歴を保持する SQLite ファイル。 |
| `PIXLOADER_TOKEN_FILE` | `<download_dir>/refresh_token.txt` | リフレッシュトークンを保存するパス。 |
| `PIXLOADER_MAX_PAGES` | `0` | ブックマークリストを取得するページ数上限。0 で無制限。 |
| `PIXLOADER_INTERVAL_SECONDS` | `0` | 連続実行時の待機秒数。0 で 1 回のみ。 |
| `PIXLOADER_CONCURRENCY` | `4` | 同時ダウンロード数 (1-16)。 |
| `PIXLOADER_TOKEN_PORT` | `8080` | ブラウザからトークンを入力するためのローカルポート。`docker-compose.yml` のポート公開と合わせて変更してください。 |
| `PIXLOADER_ALLOW_PASSWORD_LOGIN` | `false` | ブラウザのセットアップページで Pixiv ID/パスワードを入力してトークンを取得できるようにします。機密情報を扱うため、必要な場合のみ `true` に設定してください。 |
| `PIXLOADER_ENABLE_VIEWER` | `false` | ブラウザビューアを有効化します。`true` にした場合、ダウンロードループと並行して HTTP サーバーが起動します。 |
| `PIXLOADER_VIEWER_PORT` | `41412` | ビューアの公開ポート。 |
| `PIXLOADER_VIEWER_HOST` | `0.0.0.0` | ビューアがバインドするホスト名。LAN に公開する場合のみ変更してください。 |
| `PIXLOADER_HOST_ROOT` | `./data` | ホスト側で `/data` にマウントするディレクトリ。例: `PIXLOADER_HOST_ROOT=D:\\Pixloader-image` |

> **Windows の場合:** Docker Desktop の Settings → Resources → File sharing で対象ドライブ (例: D:) を共有しておく必要があります。

### トークン入力について
- `PIXIV_REFRESH_TOKEN` を `.env` に設定済みであればブラウザ入力は不要です。
- `.env` に設定しない場合、コンテナ起動後に `http://localhost:8080/` へアクセスし、表示されるフォームからトークンを保存してください。保存後はページを閉じて構いません。
- トークンは `PIXLOADER_TOKEN_FILE`（既定 `downloads/refresh_token.txt`）に平文保存されます。共有マシンではアクセス権限に注意してください。
- Pixiv のログイン情報をブラウザから入力してトークンを生成する場合は、環境変数 `PIXLOADER_ALLOW_PASSWORD_LOGIN=true` を設定した状態でコンテナを再起動してください。入力した認証情報は保存されません。
- アカウントで 2 段階認証を有効にしている場合、ログインフォームでメールに届く認証コードを入力してください。誤ったコードや期限切れの場合は再取得のうえ再入力が必要です。

### 公式 API の仕様変更について
- Pixiv 公式 API は 2022 年以降、パスワードを直接送信するログイン方式を廃止しています（[pixivpy issue #158](https://github.com/upbit/pixivpy/issues/158) 参照）。そのため、Pixloader のブラウザログイン機能もリフレッシュトークンを取得できないのが現状です。
- リフレッシュトークンは、公式の OAuth フローを経由する外部ツール（例: [ZipFile 氏の Pixiv OAuth Flow ガイド](https://gist.github.com/ZipFile/c9ebedb224406f4f11845ab700124362) や [eggplants/gppt](https://github.com/eggplants/get-pixivpy-token)）を利用して取得し、セットアップページで貼り付ける運用を推奨します。
- Pixiv の仕様が再度変更された場合は、プロジェクト側でも追従できるよう継続的に情報収集する必要があります。

## ビューアについて
- `PIXLOADER_ENABLE_VIEWER=true` を設定すると、ダウンローダとは別に Flask ベースの軽量ビューアが起動します。
- ビューアはダウンロードディレクトリと SQLite の履歴を参照し、作品ごとのサムネイルやページ一覧を表示します。10 / 25 / 50 / 100 / 150 / 200 / 300 / 500 件の表示件数を切り替えられ、ブックマーク数・閲覧数・評価などでソートできます。タグ／作者／タイトル／R-18 フラグによるフィルタにも対応しています。
- 各作品の詳細画面から独自タグ（カンマ区切り）と 5 段階評価を編集でき、一覧のソート／フィルタにも即反映されます。
- 一覧や詳細画面から公式 Pixiv ページへのリンクも利用できます。HTTP 経由で画像ファイルが配信されるため、LAN 内で共有する際はアクセス権限に注意してください。
- 一覧ヘッダにバックエンド収集中のステータス表示と「画像一覧を更新」ボタンを追加。ワンクリックで最新の状態に更新できます。
- ビューアを停止したい場合は環境変数を `false` に戻すか、必要なときだけ `docker compose up` で立ち上げてください。
- 再起動時には不足している作品メタデータ（タグやブックマーク数など）を自動的に補完します。初回のみ数分かかる場合があります。

## ローカル実行
ビルドしたくない場合は直接 Python で動作させることもできます。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PIXIV_REFRESH_TOKEN=...
python -m app.main
```

## 注意事項
- Pixiv の利用規約・API 利用規約に従ってお使いください。
- 初回は大量のブックマークを取得するため時間が掛かる可能性があります。`PIXLOADER_MAX_PAGES` で件数を制限できます。
- 画像ファイル名は `イラストID_pXX.ext` 形式で保存されます。タイトルや作者名はダウンロード履歴（SQLite）に保持しています。
- リフレッシュトークンは機密情報です。`.env` やシークレットマネージャー等で安全に管理してください。

## ライセンス
MIT ライセンスを想定しています。必要に応じて `LICENSE` を作成してください。
