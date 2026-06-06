# camera-eye

ONVIF + RTSP で部屋のカメラに「見る目」を持たせる CLI。Tapo C216 で確認、PTZ 対応 ONVIF カメラなら他機種でも通る想定。

## できること

- `capture` — 最新フレームを 1 枚 JPEG で取得
- `pan {left|right|up|down} [seconds]` — パン/チルト
- `watch start|stop|status` — 1 fps の常時取得 daemon。動かしておくと `capture` が file 返すだけになりレスポンスが上がる
- `status` — 設定と ONVIF 疎通の確認
- `setup` — 設定ファイル対話作成

## 前提

- Python 3.11+ (tomllib)
- ffmpeg (PATH 上)
- ONVIF を有効化したカメラ (Tapo は app の高度な設定 → カメラアカウントで ON にする)

## セットアップ

```sh
python camera_eye.py setup
export CAMERA_EYE_PASS='your-onvif-password'
python camera_eye.py status
```

設定は `~/.camera-eye/config.toml`。パスワードは設定ファイルに書かず、必ず `CAMERA_EYE_PASS` 環境変数で渡す。

## レスポンス

- `capture` 単発 = ffmpeg を毎回起動するので 2〜3 秒
- `watch start` 後の `capture` = 直近フレームを返すだけで <300ms (大半は Python interpreter 起動時間)

GUI デバッグなど高頻度に観たい用途では `watch start` してから `capture` する。

## 設計メモ

- 認証は WS-Security UsernameToken (PasswordDigest)。SOAP は raw 文字列、ライブラリ依存なし
- snapshot 系の ONVIF GetSnapshotUri は C216 では `ter:ActionNotSupported`、RTSP → ffmpeg 経由で取る
- `watch` daemon は ffmpeg を detach 起動 (`-vf fps=1 -update 1`)。pid は `~/.camera-eye/watch.pid`、log は `~/.camera-eye/watch.log`
