# Ubuntu 既存 LiveKit での合成 PCM 実接続検証（2026-10-05 JST）

## 対象と境界

PR #3 統合後の epic `9a38af6ac1d2ea197fc96131c9c6c0ef98f43565` に含まれる
LiveKit アダプターを、WSL `Ubuntu` の既存 dev LiveKit に接続した。
サーバーは `livekit/livekit-server:v1.9.7`、接続先は `ws://127.0.0.1:7880`。
RTC SDK は lock 固定の `livekit==1.1.16`、既存の token 発行 SDK は `livekit-api==1.2.0`。
試験前後のローカル HTTP 応答は 200。サーバーの起動・再起動・設定変更は行っていない。

ユーザーが承認した専用検証 Room のみを対象とした。
各試行で高エントロピーの専用 Room 名と異なる参加者 identity を割り当て、
有効期間 **90 秒** の JWT を 2 件だけメモリ内で発行した。
権限は対象 Room への join、音声 microphone track の publish、subscribe に限定し、
data publish・管理権限・永続 credential は追加していない。
JWT、署名鍵、Room/identity の実値、PCM、会話本文は証跡や Git に保存しない。

## 手順

[合成スモーク](../../tools/livekit_smoke.py)を使い、次を確認した。

1. 合成参加者を接続し、その SDK participant SID に Backend 入力認可を束縛する。
2. 16 kHz mono PCM16、10 ms 単位、DTX 無効の無音を送信して受信統計を準備する。
3. 入力 grant 後に低振幅の合成 sine を送り、sample 時計・統計検査を通過した入力を観測する。
4. 偽 Core/TTS の 2 区間 WAV を配信し、受信側 AudioStream 設置後の準備確認、区間番号・sample 範囲、受信 PCM を照合する。
5. 長い区間の送信中に取消し、同期的な出力失効・queue 消去・mute、provider 取消、旧区間の送信完了通知が増えないことを検査する。
6. 次の応答を新しい track で生成し、**その track の受信 PCM** まで観測する。
7. 合成参加者を切断し、入力 grant の失効、Room 切断、pump/reader/source/stream の解放と二重 close を確認する。

ProbePipeline は PCM の位置と数量だけを調べ、VAD/STT/TTS/LLM 推論を起動しない。
マイク、音声出力装置、実ユーザー音声、GPU、Ubuntu-dogfood は使用していない。

## 結果

初回も pass したが、検証器の回復後受信確認と終了済み reader 例外検査を補強して再実行した。
アダプター本体の修正は不要だった。

| 観測 | 初回 | 補強後の再実行 |
| --- | --- | --- |
| 終了 code / status | 0 / pass | 0 / pass |
| 入力 open | 63 ms | 69 ms |
| 検査を通過した入力 PCM | 4,480 samples | 4,000 samples |
| RTC 受信 PCM 総数 | 22,240 samples | 22,880 samples |
| 出力 track / 完了区間 | 3 / 4 | 3 / 4 |
| 送信途中の取消・provider 取消 | pass | pass |
| 次応答・受信復帰 | 送信の確認 | **送信と受信を確認、pass** |
| 切断・資源解放 | pass | pass |
| 実再生 ACK の生成 | なし | なし |

再実行は 2026-10-05 01:14 UTC 頃（10:14 JST）。
受信 sample 数は RTC の受信観測量であり、ブラウザの出力時計・実出音・再生済み prefix ではない。
取消後にネットワーク内の PCM が届く可能性を許容し、停止遅延や実出音の停止を推定しない。

## 再現と残条件

`uv sync --frozen --extra livekit` 後、接続を行わない事前確認は
`uv run --no-sync python tools/livekit_smoke.py`。`--help` に必要な環境変数と制約を記載した。
実接続は別途認可済みの隔離 Room と短期 JWT をホストから供給し、
`--run --room <専用Room名>` を指定した場合だけ行う。
検証コード自身には token の発行・署名・サーバー操作機能を含めない。

[オフライン試験](../../tests/test_livekit_smoke.py)は接続既定無効、設定と権限の拒否、
PCM/Probe、偽 provider と既存 Session の取消、reader 失敗検出を確認する。
実 RTC は通常 CI に接続せず、この手動検証と区別する。

ブラウザ制御 wire、ネットワーク入力 ACK、device gate、ブラウザ実出力時計と再生 ACK、
実 STT/TTS/Core、実マイク・聴感品質は引き続き未受入。
この結果をブラウザ音声会話全体の完成とはしない。
