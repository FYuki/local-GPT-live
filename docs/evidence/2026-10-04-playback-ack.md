# 再生ACK境界の検証（2026-10-04 UTC）

関連: [Issue #1](https://github.com/FYuki/local-GPT-live/issues/1)、
[提案契約](../adr/0002-playback-ack.md)。
base: `01a680aa9825bcff0da7a1d8a83b9463cc231f1b`。
初回検証コード: `48c875f9603a47561aa9f7f5a4af7df0f0b3b959`（88テスト成功）。
CodeRabbit対応後の最終コード: `48fef7b79df2426cf5088bb93ba9648344fb0098`。
後続コミットはこの証跡の文書更新のみ。

## 環境と結果

Windows x64、CPython 3.12.14、uv 0.8.22。専用worktreeを使用し、既存uv.lockの
`uv sync --frozen --offline`に成功。依存version・lockは変更していない。
ONNX Runtimeが必要とするVC++ DLLはMicrosoft公式VCLibsから検証用領域へ展開し、
全25 DLLのAuthenticode署名がValidであることを確認。process内のDLL検索先だけを追加した。
OSインストール、共有GPUサービス、既存mainの作業ファイルは変更していない。

| コマンド | 結果 |
| --- | --- |
| pytest -q -p no:cacheprovider | 90 passed（新規18件、skipなし） |
| ruff check . --no-cache | 成功 |
| mypy --cache-dir 検証用ディレクトリ | 14 source files、成功 |
| python tools/check_docs.py | 日本語文書入口・相対リンク、成功 |
| python tools/check_repository.py | 追跡文書の相対リンク、成功 |
| uv build --offline --python 検証用Python --out-dir 検証用ディレクトリ | sdist・wheel、成功 |
| voice-demo | 合成イベント7/7、成功 |
| git diff --check | 成功 |

新規試験は未配信・不正型・飛び越しACK、冪等再送、旧responseの失効、生成中ACK、
配信済み未再生の完了拒否、cancel/reconnect/close/新responseを通す。
既存のCore loopback socket取消試験も実行した。Core/Whisper/TTS実サービスの合格ではない。
実行stdoutとコマンド一覧は作業領域の`playback-pr-validation.json`へ保存した。

## CodeRabbitの差分レビュー

最初のhead `40166df5d18e85927c79c5c5c65ebc6c0c31e52d`について、
[実レビュー](https://github.com/FYuki/local-GPT-live/pull/2#pullrequestreview-5404371499)
はbase `01a680a`からの8ファイルだけを選択した。設定はja-JP/assertive、auto review無効のまま。
[指摘1件](https://github.com/FYuki/local-GPT-live/pull/2#discussion_r4176182066)は、
CLIのACK呼出しがassert式内にあり`python -O`で消えること。コードを確認して修正し、
最適化モードでの7シナリオ通過と、ACK拒否時の明示例外を追加検証した。
全90テストと上記lint/type/docs/build/CLIを再実行して成功。最終headのCIと差分再レビューはPR本文に記録する。

## 実接続の状態と制限

以下は環境指定が明確になる前の初回時点の記録。後にユーザーがdevをWSLの`Ubuntu`と明示し、
[Ubuntuでの合成実接続検証](2026-10-04-ubuntu-connections.md)を実施した。初回のNOT RUNは履歴として保持する。

ユーザー指定はUbuntu-dev。正規の権限経路で`wsl.exe --list --verbose`を確認したが、
存在するのはUbuntu（停止中）とUbuntu-dogfood（実行中）のみ。Ubuntu-devの実在・user・
サービスを確認できないため、実接続はNOT RUN。他のディストリビューションを起動して代替していない。
sudo、別user、WSLアクセス拒否の迂回、実マイク・カメラ、私的会話取得は行っていない。

LiveKit本体、ブラウザの出力時計照合、実再生prefixの履歴連携、人の音声品質受入は後続。
この証跡はBackendの自動検証だけであり、Ubuntu-devの実接続受入や人格評価の完了を意味しない。
GitHub CIとCodeRabbitはPRで実際の最終head・結果を確認する。
