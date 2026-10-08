# ADR 0006: 認証済みhostと公式SDK RPC

状態: 採用（単一Sessionのhost APIと合成検証。実ブラウザ会話受入は含まない）

## 背景

[ADR 0003](0003-livekit-adapter.md)はhostの認証・device gate・5秒入力ACKを後続境界としていた。
[ADR 0004](0004-browser-playback-bridge.md)は既存bindingとACKの受付を定める。
今回、公式SDKを使用して#14が呼べる最小制御wireと宛先指定通知を接続する。

## 決定

- `host_rpc.py`が制御schemaと固定reason、`LiveKitHost`がSDK登録・認可・期限・通知を所有する。
  hostが確認済みSession IDとparticipant identity/SID、取得済みendpoint/tokenを注入する。
  SDK caller identityからRoom participantを取得しSIDを照合する。JSONの自己申告identityは使用しない。
- 統計準備とreader開始を分け、実Backend grantのACK後だけ正式PCMを処理する。
  新trackは統計用に先にpublish/配送し、最初の要求からACKまで同じ5秒期限を使う。
  直接Python openは準備と開始を続け、既存契約を維持する。
- muteはマイク入力gate、focusはtext入力欄のfocusとして固定する。
  gate解除だけで旧trackを再開しない。論理reconnectは同じhost/Sessionで新trackを要求する。
  実RTC切断は既存単回transportを終了する。
- 受付失効は準備lockを待たず同期実施する。遅着cleanupは保存した操作とgrantを照合する。
  SDK資源の所有と遅着回収はtransportに残す。close RPCは終了受付を返し自己待ちを避ける。
- publish時点でready期限を保存し、受付と待機の双方で照合する。
  publish、metadata、通知失敗、時間推定からreadyや実再生ACKを生成しない。
- responseごとの既存ACK bindingを出力trackへ束縛する。制御bindingとACK bindingを区別する。
  STT起動を含むresponse_startedとoutput_trackでcontrol bindingを通知する。
  全通知と制御応答はSession、接続識別子、単調なstate_sequence、Backend現在revisionを共有し、
  gate・取消・再接続・stream終了後の再認可を端末内部参照なしで進める。
  宛先指定SDK通知は送信直前にSIDとscopeを再確認し、未送信の旧通知を破棄する。
  [ADR 0005](0005-sent-audio-progress.md)の推定計算・凍結・実再生未確認の意味を維持する。

操作別schema、成功・拒否・timeout・再送規則、組込順序は[host契約](../livekit-host-rpc.md)を正とする。
既存SDKと純粋browser controllerを再実装せず、広いPoC protocolを移植しない。

## 検証と制約

偽Roomと実RpcInvocationDataから実AudioInput・Backend・Session・transportへ接続するIT1で、
認可、不正型、ACK前抑止、期限、gate変更前後、旧操作・timer・統計/resetの遅着、通知を検証する。
schemaはUT、既存browser bridgeはIT2。STの実会話・実マイク・聴感、実RTP対応は別受入。
認証情報発行、共有サービス設定、UI、履歴・人格・長期記憶は変更しない。
