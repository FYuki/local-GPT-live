# 再生ACK境界の検証（2026-10-04 UTC）

関連: [Issue #1](https://github.com/FYuki/local-GPT-live/issues/1)、
[提案契約](../adr/0002-playback-ack.md)。
base: `01a680aa9825bcff0da7a1d8a83b9463cc231f1b`。
検証対象コード: `48c875f9603a47561aa9f7f5a4af7df0f0b3b959`。後続コミットはこの証跡の文書追加のみ。

## 環境と結果

Windows x64、CPython 3.12.14、uv 0.8.22。専用worktreeを使用し、既存uv.lockの
`uv sync --frozen --offline`に成功。依存version・lockは変更していない。
ONNX Runtimeが必要とするVC++ DLLはMicrosoft公式VCLibsから検証用領域へ展開し、
全25 DLLのAuthenticode署名がValidであることを確認。process内のDLL検索先だけを追加した。
OSインストール、共有GPUサービス、既存mainの作業ファイルは変更していない。

| コマンド | 結果 |
| --- | --- |
| pytest -q -p no:cacheprovider | 88 passed（新規16件、skipなし） |
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

## 実接続の状態と制限

ユーザー指定はUbuntu-dev。正規の権限経路で`wsl.exe --list --verbose`を確認したが、
存在するのはUbuntu（停止中）とUbuntu-dogfood（実行中）のみ。Ubuntu-devの実在・user・
サービスを確認できないため、実接続はNOT RUN。他のディストリビューションを起動して代替していない。
sudo、別user、WSLアクセス拒否の迂回、実マイク・カメラ、私的会話取得は行っていない。

LiveKit本体、ブラウザの出力時計照合、実再生prefixの履歴連携、人の音声品質受入は後続。
この証跡はBackendの自動検証だけであり、Ubuntu-devの実接続受入や人格評価の完了を意味しない。
GitHub CIとCodeRabbitはPRで実際の最終head・結果を確認する。
