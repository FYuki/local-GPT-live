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

実再生ACKのBackend境界は`tests/test_playback_ack.py`で検証する。未配信・飛び越し・
不正型・旧responseを拒否し、再送の冪等性、生成中ACK、取消後の失効を確認する。
合成CLIの端末ACKはfixture上の事実であり、LiveKitやブラウザの実出力確認ではない。
ユーザーが指定するdev環境はWSLディストリビューション`Ubuntu`。実接続テストもUbuntuから行う。
共有推論endpointがUbuntu-dogfoodにあっても、devの実行主体を置き換えない。
合成入力による部分的な実接続結果は[Ubuntu検証証跡](evidence/2026-10-04-ubuntu-connections.md)を参照する。

## RTP対応のオフライン観測

`uv sync --frozen --extra livekit`で固定SDKを準備し、
`uv run --no-sync python tools/rtp_observation.py`で接続しない合成ハーネスを実行する。
`tests/test_rtp_observation.py`は通常のpytest対象に含まれる。
実SDKのframe・統計schemaと合成値を分け、同数の補完、取消後遅着、rateの相違でも
元PCM区間やブラウザ出力を確認済みにしない。実際の取消・codec・RTP配送はこのハーネスでは実行しない。
次段階の観測点と不足は[RTP測定計画](rtp-observation-plan.md)を参照する。

## 送出台帳と進捗推定の合成検証

[送出済み音声ブロックと出力終了の推定](sent-audio-progress.md)は、SDK書込み成功の
範囲・時刻、経過時間による推定、Sessionの出力中判定を検証する。実再生ACKの証明ではない。

```sh
uv sync --frozen --extra livekit
uv run --no-sync pytest -q tests/test_sent_audio.py tests/test_livekit_transport.py tests/test_estimated_output.py tests/test_session.py tests/test_input.py
```

偽SDK、合成PCM、制御した単調時計を使い、書込み成功前、部分送出、連続ブロックprefix、
frame音声時間による制限、供給空白、取消・割込み後の固定、遅着capture、旧応答の分離、
保持上限を確認する。Session側は生成と送出の順序が逆転する場合、未登録の後続ブロック、
空応答、timerと発話開始の同時評価、取消prefixの保持、入力generationの維持を検証する。
取得だけでは状態が変わらず、推定による出力終了が実再生ACKや`playback_completed`を
生成しないこと、終了後の旧ACKを拒否することも確認する。
下り遅延300msは初期仮定であり、この合成試験で実測・較正された値ではない。
新しいtoken、LiveKit実接続、ブラウザ起動、マイク権限、共有サービス変更は不要。

## 実音声受入（別枠）

1. 親作業者とGPU利用時間・Core版・共有STT/TTS endpointを固定する。サービス再設定はしない。
2. 既存LiveKit adapterを統合し、実マイクで最低3往復、文中休止、語頭/語尾、相槌継続を確認する。
3. 生成中・生成終了後再生中の割り込み、cancel、通信断、新track再開、TTS失敗後の回復を確認する。
4. 人が読み・声質・分割間gap・待ち時間を評価する。自動テストで代用しない。
5. 版・条件・数値結果・人の確認を分けてevidenceへ記録する。私的会話や録音を公開しない。

PoCの2026-09-19実用受入表には利用者マイク確認済みの記録があるが、今回の新構成には引き継がない。
旧p95 1968ms等の測定も条件付き過去証跡であり、このrepoの性能値ではない。
