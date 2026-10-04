# local-GPT-live

かぶせ発話に対応するローカル音声基盤です。人格・モデル設定・LLM推論は
digital-souls-core API、Agentの実行はprivate-agentが所有します。

開発規約は[AGENTS](AGENTS.md)、ブランチ運用は[リポジトリ運用](docs/repository-policy.md)を参照してください。

CIとローカル検証の手順は[開発ガイド](CONTRIBUTING.md)にまとめています。

Python 3.12とuv 0.8.22で`uv sync --frozen`後、`uv run --no-sync voice-demo`を実行します。
GPU・マイク・共有サービスへ接続せず、7種類の合成イベントを検証できます。

[境界ADR](docs/adr/0001-voice-boundary.md)・[用語](CONTEXT.md)・
[実装と未統合範囲](docs/architecture.md)・[検証手順](docs/testing.md)・
[初回公開手順](docs/initialization.md)を確認してください。
