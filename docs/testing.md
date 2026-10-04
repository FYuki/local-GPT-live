# 検証と実音声受入

## 自動検証

Python 3.12、uv 0.8.22を使い、WSL Ubuntuで実行する。

```sh
uv sync --frozen
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync mypy
uv run --no-sync python tools/check_docs.py
uv build
uv run --no-sync voice-demo
```

同梱fixtureは架空のtextとプログラム生成PCMのみ。マイク収録、配布音声、声modelは使わない。
無音は実CPU VADへ、かぶせ/割込/連続/reconnect/cancel/timeoutは合成した正式発話イベントへ投入する。
STTの認識精度、スピーカーの出音、WebRTC統計、遅延品質を測定した試験ではない。
provider integrationはHTTP mockとローカルloopback fixtureで契約と切断を検証する。

## 実音声受入（別枠）

1. 親作業者とGPU利用時間・Core版・共有STT/TTS endpointを固定する。サービス再設定はしない。
2. 既存LiveKit adapterを統合し、実マイクで最低3往復、文中休止、語頭/語尾、相槌継続を確認する。
3. 生成中・生成終了後再生中の割り込み、cancel、通信断、新track再開、TTS失敗後の回復を確認する。
4. 人が読み・声質・分割間gap・待ち時間を評価する。自動テストで代用しない。
5. 版・条件・数値結果・人の確認を分けてevidenceへ記録する。私的会話や録音を公開しない。

PoCの2026-09-19実用受入表には利用者マイク確認済みの記録があるが、今回の新構成には引き継がない。
旧p95 1968ms等の測定も条件付き過去証跡であり、このrepoの性能値ではない。
