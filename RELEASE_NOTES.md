# Release Notes / リリースノート

📖 日本語は[下記](#日本語)、English is [below](#english)。

---

## 日本語

### v1.0.1 (2026-08-11)

**新機能**
- **管理画面の日英対応 (i18n)**: ブラウザの言語設定を自動検出し、日本語/英語を自動で切り替えます。サイドバー下部の切り替えボタンでいつでも手動切替も可能です（選択はCookieに保存されます）。全画面が翻訳対象です。
- **Configページに「エクスポート」セクションを追加**: これまでダッシュボードのApp一覧の横にあった「全体をエクスポート」を移動し、ドロップダウンで「全体」または個別のApp/Site/Proxy/Cron Job/Containerを選んでダウンロードできるようにしました。

**セキュリティ修正**
- 軽微なセキュリティ修正を含みます。

**細かい修正**
- インストール/更新スクリプト (`install.sh`/`update.sh`) が新規追加した`i18n.py`をコピーしていなかった不具合を修正（未更新のノードでは`sudo ./scripts/update.sh`の再実行が必要です）。
- サイドバーのレイアウトを調整（不要になった`ns:`表示を削除し、言語切り替えとバージョン表示を1行に整理、ユーザー名をログアウトの上の行に右揃えで表示）。

**アップグレード方法**: `git pull` の後、`sudo ./scripts/update.sh` を実行してください。

---

## English

### v1.0.1 (2026-08-11)

**New**
- **Bilingual admin console (i18n)**: the console now auto-detects your browser's language and switches between Japanese and English automatically, with a manual toggle at the bottom of the sidebar (saved via a cookie). Every screen is translated.
- **New "Export" section on the Config page**: replaces the "Export all" link that used to sit next to the dashboard's App list. Pick "All" or any individual App/Site/Proxy/Cron Job/Container from a dropdown, then download.

**Security fixes**
- Includes minor security fixes.

**Minor fixes**
- Fixed `install.sh`/`update.sh` not copying the newly-added `i18n.py` module (nodes updated before this release need to re-run `sudo ./scripts/update.sh`).
- Sidebar layout cleanup (dropped the now-redundant `ns:` tag, put the language toggle and version number on one line, right-aligned the username above the logout link).

**To upgrade**: `git pull`, then run `sudo ./scripts/update.sh`.
