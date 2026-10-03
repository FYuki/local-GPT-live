# 開発規約

- WindowsからWSL Ubuntuで開発する。GPU/STT/TTSはUbuntu-dogfoodの共有サービスが所有する。
- 専用worktreeを使い、main→epic/*→workの順で分岐し、workのPRはepicへ向ける。
- mainへ直接pushしない。初回main作成の例外は明示承認を必要とする。
- CIは全PRとmain/epic pushで実行。epic統合はCI green、main統合はCI greenとCodeRabbit指摘対応後。初期PRはdraft。
- CodeRabbit依頼は親作業者と調整し、差分のみレビューする。設定ファイルを接続証拠としない。
- 人向け文書・コメントは日本語、commitはConventional Commits。
- 人格、長期記憶、Agent loopを移植しない。音声本文・秘密値・私的ログをcommitしない。
- 共有サービスを停止・再設定しない。mock/fixtureと実音声受入を分ける。
- 公開・認証設定、常駐追加、権利不明な音声・声モデルの追加は行わない。
