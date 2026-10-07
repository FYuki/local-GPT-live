# 用語

| 用語 / 実装名 | 定義・状態 |
| --- | --- |
| Core | 人格付きLLM API。音声判断を持たない外部依存 |
| VoiceSession | 本repoの音声会話制御。Core APIのcaller |
| input generation / InputGrant | Backendが採番する入力認可。track/revisionと対応 |
| response | 1つの生成・配信単位。LiveKit接続時は送出台帳による推定終了、基底Sessionでは実再生ACKの完了条件で出力中の状態を閉じる |
| playback | 端末が実際に消費する音声。配信量を実再生量とみなさない |
| backchannel / take_turn | 相槌継続 / 発話権取得。PoCの判定を維持 |

不変条件は[ADR](docs/adr/0001-voice-boundary.md)、実装範囲は[構成](docs/architecture.md)を参照。
