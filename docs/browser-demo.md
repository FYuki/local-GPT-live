# 最小ブラウザ会話デモ

日本語画面から既存の認証済みLiveKit hostを操作するローカル入口。
公式browser SDKは `livekit-client@2.22.3`、lockから導入する。
対象はChromium（Chrome/Edge）。実機版は受入時に記録する。
既存の資格情報を明示注入し、認証設定や共有サービスの起動構成は変更しない。

## 起動と設定受渡し

Python 3.12、uv 0.8.22、Node.js 22を用意する。リポジトリルートから実行する。

```sh
uv sync --frozen --extra livekit
npm ci --prefix browser
npm --prefix browser start
```

1. `http://127.0.0.1:4173/` を開く。接続URL、取得済みのブラウザtoken、
   host identityを入力して「接続」を押す。Session/connection/host SIDは初回は空でよい。
   通知待受はRoom接続前に登録される。画面に表示されたparticipant identity/SIDを確認する。
2. 別端末で下のhost CLIへ、同じroom用の取得済みhost tokenと画面のparticipantを渡す。
   tokenは非表示promptで入力する。コマンド引数にtokenを含めない。
3. CLIが表示したSession ID、connection IDを画面へ入力する。
   ブラウザが参加していることを確認してCLIでEnterを押す。
4. CLIが接続後に示すhost SIDを画面へ入力し「host同期」を押す。
   SDK上の期待host identity/SIDを確認してから、専用の状態取得RPCでSession/connectionを照合し、
   現在のbinding/revisionを取得する。host参加通知が後着する場合はそのイベントを待つ。
   初期host_stateが先着し送信者participantが未解決の場合、そのpacketは拒否する。
   初期通知の欠落・拒否からも、この状態取得で回復する。通知再送や任意bindingは不要。
   「host同期済み」表示までは制御操作を許可しない。SIDはtokenのidentityとは別の値。

```sh
uv run --no-sync browser-demo-host \
  --url ws://127.0.0.1:7880 \
  --participant-identity <画面のidentity> --participant-sid <画面のSID> \
  --host-identity <host tokenのidentity> --mode diagnostics
```

診断は入力PCMの到達sample数だけを端末へ表示する。「送信」で短い440Hz合成音を要求できる。
本文の認識や会話品質を検証するモードではない。PCM・本文・transcriptを保存しない。
音声入力は通常と同じgrant/ACK、音声出力は通常と同じSession/ready/送出/推定経路を通る。
入力到達数、再生準備、人が実際に聞いた結果を別々に記録する。
診断成功を通常会話成功にしない。通常モードからの自動fallbackはない。

通常会話は `--mode conversation` に加えて
`--stt-url <既存Whisper HTTP>`、`--core-url <既存loopback Core HTTP>`、
`--tts-url <既存VOICEVOX HTTP>`、`--core-alias <登録alias>`、
`--speaker-id <既存speaker番号>` を必須指定する。
共有STT/TTSに接続できない場合は失敗として扱い、サービスを起動・再設定しない。
終了はCLIでCtrl+C。画面は「切断」で端末資源を解放する。

## 操作・状態・失効

- マイク開始: 新track取得・publish → 準備中 → 認可待ち → 保存grant照合 → input_ack成功 → 正式入力中。
  ACK前のpublishは統計準備用で、Backend正式入力ではない。
  echoCancellation/noiseSuppressionを有効化し、DTXを無効化する。
  openからACKまで一つの5秒予算を使う。
- マイク停止: 端末送信を先に抑止し、host mute gateで失効後に旧trackを解放。
  再開はgate解除だけで行わず、マイク開始で新track・新認可を取得する。
- テキスト欄のfocus: host focus gateで音声入力を失効する。
  blurはgate解除だけ。送信本文は制御JSONとして解釈せずtextフィールドへ配送する。
- 取消・再接続: pending入力と出力を失効する。再接続ボタンは存続するRoomの論理reconnect。
  実RTC切断・reconnecting・host退室では単回hostを終了するため、
  新しいparticipant SIDとhost/Session/connection設定で起動手順をやり直す。
- 出力: 現応答metadataと現在hostの購読trackを照合し、Web Audio出力接続とrunning状態からreadyを送る。
  消音media element（muted=true、volume=0）のplayを開始してWebRTC decoderを駆動する。
  可聴出力はWeb Audioだけ。無音でもplay完了や最初のPCMをready条件にしない。
  permission/autoplay拒否・準備失敗・timeoutを失敗表示し、遅いplay拒否も現応答の資源を解放する。
  RTPの出力接続だけを使い、既存PCM rendererを重ねず、実再生ACKを生成しない。
- 生成完了でも「出力中」を維持する。BEの推定終了通知で表示を解除し次入力を受け付ける。
  推定は `sdk_submitted_elapsed`、実再生確認はfalse。聴取済みと表示しない。

通知とRPC応答の共通状態はstate_sequence順で採用する。同じ現行control binding/responseの
track metadataは共通状態が古くても結合する。output_trackのbindingはACK専用であり、
ready/cancelへはcontrol_bindingを使う。旧接続ACK・ready・遅着資源を新接続へ採用しない。
host拒否には失敗を表示し、拒否応答の現在状態を次の明示操作へ使う。本文・取消を自動再送しない。
初期同期専用の `local-gpt-live.state.v1` は入力認可・gate・応答を変更しない。
取得前後にSDK相手のidentity/SIDを確認し、取得したSession/connection/stateを照合する。
相手不一致・切断後の遅着応答は採用しない。参加確認待ちとRPCはそれぞれ5秒で失敗を表示し、
失敗後は接続設定とhost参加を確認して「host同期」で明示再試行する。時間だけによる自動retryはない。

tokenはアプリURL query、localStorage、アプリログへ保存しない。入力欄は接続時に消去する。
SDKの認証通信そのものは既存LiveKit契約であり、アプリURLへの保存と区別する。
配信はloopback固定で、任意ファイルを公開しない。

## 自動検証と実機受入

```sh
npm --prefix browser run build
npm --prefix browser test
npm --prefix browser run test:integration
node browser/node_modules/playwright/cli.js install chromium
npm --prefix browser run test:browser
uv run --no-sync pytest -q tests/test_browser_demo.py
```

UTは状態・grant・期限・失効、IT1はJSから実Python host/Backendの受渡し、
IT2は実DOM/Chromium/Web Audioとfake SDKを検証する。既存ACK・bridge・renderer回帰も実行する。
ローカルRTCPeerConnection同士の試験では、RTP未送信でもadapter準備が完了すること、
その後に送信した440Hz合成toneのRTP受信数とWeb Audio側の信号を観測する。
共有LiveKitへの実接続や人の聴感の成功証拠ではない。
`browser-demo.yml` は全PRとmain/epic pushの通常CIへこれらを配線する。
SDKの根拠は[公式RPC資料](https://docs.livekit.io/transport/data/rpc/)と固定版の型・ソース。
再生接続は[Web Audio API](https://developer.mozilla.org/en-US/docs/Web/API/AudioContext/createMediaStreamSource)。

実RTC・利用者側実マイク・共有STT/TTS実会話・スピーカーの聴感は別受入であり、
合成マイクやCI成功をその証明にしない。Issue15の親担当がブラウザ版、接続条件、
権限/音声device、入力到達量、各状態、3往復・割込み・失敗回復、人の聴感を記録する。
公開配備、常駐追加、認証変更、共有サービス操作は行わない。
