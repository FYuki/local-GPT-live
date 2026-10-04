# 開発と検証

WindowsからWSL Ubuntuを使用し、専用worktreeで作業する。
共有Ubuntu-dogfoodのGPUサービス、Docker、公開設定、認証情報を変更しない。

## ブランチとレビュー

`main → epic/* → 作業ブランチ`で分岐する。作業PRはepic向け、epic PRはmain向けとする。
初期PRはdraft。epic統合前に対象headのCI成功を確認する。
main統合前にはCI成功とCodeRabbit指摘対応が必要で、mainへの直接pushは禁止する。
空repoの最小初期化だけは明示承認を得てから行う。

CodeRabbitの自動レビューは無効。親作業者が1時間枠を管理・調整して差分レビューを依頼する。
`.coderabbit.yaml`があってもGitHub App接続済みとは断定しない。

## CIの段階

全PRとmain/epic pushで、追跡対象ファイルの空白・docs相対リンクを確認する。
音声package追加後はPython 3.12・uv 0.8.22で固定依存、pytest、ruff、mypy、docs、build、demoを実行する。
初期化段階ではpyproject.tomlがないため音声jobを明示的にskipする。
これは設定の検証のみであり、音声実装のテスト成功ではない。

設定単独のローカル検証:

```sh
python3 tools/check_repository.py
git diff --check
```

音声package追加後の必須検証:

```sh
uv sync --frozen
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync mypy
uv run --no-sync python tools/check_docs.py
uv build
uv run --no-sync voice-demo
```

実音声・人の品質評価は自動CIと別に記録する。音声・会話本文・秘密値をCIログへ出さない。
