# 認証済みLiveKit host/RPC

単一のhost確認済みparticipant identity/SIDとSessionを公式SDKへ接続する。
設計は[ADR 0006](adr/0006-livekit-host-rpc.md)、音声側は
[adapter API](livekit-adapter.md)と[既存ACK境界](browser-playback-bridge.md)を参照する。
Python SDKはlockの`livekit==1.1.16`。RPC登録・呼出しは
[公式RPC資料](https://docs.livekit.io/transport/data/rpc/)と固定SDKのAPIを使用する。
資格情報の発行や環境変数の自動読込みはしない。

## 最小の組込み

provider、既存endpoint、取得済みtoken、認証済みidentity/SIDは呼出側が用意する。
Session IDも呼出側が確認済みの対応から渡す。

```python
from local_gpt_live.input import AudioInput
from local_gpt_live.livekit_host import LiveKitHost
from local_gpt_live.livekit_transport import LiveKitConfig, LiveKitPlayback, LiveKitTransport
from local_gpt_live.session import VoiceSession
from local_gpt_live.voice_input.pipeline import VoiceInputPipeline

session = VoiceSession(stt, core, tts, LiveKitPlayback(), emit=observe_session)
audio = AudioInput(session, VoiceInputPipeline())
transport = LiveKitTransport(audio, LiveKitConfig(
    url=existing_url, token=issued_token,
    participant_identity=authorized_identity, participant_sid=authorized_sid,
))
host = LiveKitHost(transport, session_id=authorized_session_id)
try:
    # 認証済みアプリの既存接続設定として、host identity・session_id・host.connection_idを端末へ渡す。
    # 端末の通知購読を先に登録し、host_stateから初期binding/revisionを取得する。
    await prepare_authenticated_application(host)
    await host.connect()
    await serve_authenticated_application(host)
finally:
    await host.aclose()
```

`serve_authenticated_application`等は利用アプリが所有する処理であり、このpackageのAPIではない。
hostは接続前に作り、`connect()`でRoom接続後にlocal participantへRPCを登録する。
既存`on_track`/`on_segment`/Session observerも呼ぶ。host付きtransportに、別の入力認可や
独立したACK receiverを重ねない。直接Python利用の`transport.open_input`は従来どおり維持する。

ブラウザは公式SDKの`localParticipant.performRpc`を使用する。
認証済みアプリの接続設定からSession IDとconnection_idを取得し、通知購読をhost接続前に登録する。
最初の`host_state`または下記の専用状態取得RPCから初期binding/revisionを取得する。
binding自体は認証資格情報ではない。SDKのDataReceivedがParticipantConnectedより先に届き、
送信者participantを解決できない場合、その通知を採用しない。

## 初期状態の取得

`local-gpt-live.state.v1` はbindingをまだ持たない端末の初期同期専用RPC。
payloadはexact field集合 `v:1, session_id, connection_id` の単一JSON object。
制御と同じ8192 bytes上限・識別子制約・重複key/未知field拒否を適用する。
hostはSDK caller identityと現在Roomの認可対象SIDを照合し、Session/connectionの一致後に
`ok:true, v:1, type:"host_state"` と共通状態を返す。状態順序の採番以外にbinding、grant、
入力revision、gate、Session応答を変更せず、制御RPCのbinding検証も保持する。
未認証・不正schema・別Session/connectionには固定reasonの拒否だけを返し、状態を開示しない。

端末は期待host identity/SIDをSDKのremoteParticipantsで確認してからこのRPCを呼び、
応答後にも同じparticipant/SIDの存続を確認する。Session/connection/state_sequenceを検証してから
制御を有効化する。participantが後着する場合はParticipantConnectedで取得へ進む。
初期通知が欠落してもこの経路で同期できる。sender不明packetの採用、ダミーbindingを使った
副作用RPCの拒否応答への依存、待ち時間だけのretryは行わない。

## ブラウザからの制御例

```js
const invoke = async (message, responseTimeout = 5000) => JSON.parse(
  await room.localParticipant.performRpc({
    destinationIdentity: hostIdentity,
    method: 'local-gpt-live.control.v1',
    payload: JSON.stringify(message),
    responseTimeout,
  }),
);
let stateSequence = -1;
const acceptState = (message) => {
  if (message.session_id !== sessionId || message.connection_id !== connectionId ||
      message.state_sequence < stateSequence) return false;
  stateSequence = message.state_sequence;
  controlBinding = message.control_binding;
  inputRevision = message.input_revision;
  return true;
};
// 認証済みhostから指定topicのDataReceivedだけを渡す。旧接続の通知は採用しない。
const onHostEvent = (message) => {
  if (message.session_id !== sessionId || message.connection_id !== connectionId) return;
  acceptState(message);
  if (message.control_binding !== controlBinding) return;
  // state_sequenceが古くても、同じ現行scopeのsegment metadataは別途処理する。
  // 遅着input_invalidatedはinput_revisionが現在値未満なら採用しない。
  // response_started/output_track: 新応答へ切替。output_track.bindingはACK専用。
  // input_invalidated: 端末の旧trackを停止。新trackをpublishして明示再認可する。
  // response_failed/input_failed/input_rejected/transport_failed: 失敗を表示する。
  // active_response_idとイベントのresponse_idを照合して応答表示を更新する。
};
const base = () => ({v: 1, session_id: sessionId, binding: controlBinding,
                     request_id: crypto.randomUUID()});
// host_stateを受領しacceptStateで初期状態を採用してから制御を開始する。
const requestStarted = performance.now();
// 新trackのpublishと統計用配送を先に開始する。正式PCM処理はACKまでhostが止める。
const prepared = await invoke({...base(), type: 'open_input',
                               track_sid: microphoneTrackSid, expected_revision: inputRevision});
acceptState(prepared);
if (!prepared.ok || performance.now() - requestStarted >= 5000) throw new Error('input unavailable');
const remaining = Math.min(prepared.remaining_ms, 5000 - (performance.now() - requestStarted));
if (remaining <= 0) throw new Error('input timeout');
const started = await invoke({...base(), type: 'input_ack', grant: prepared.grant}, remaining);
acceptState(started);
if (!started.ok || performance.now() - requestStarted >= 5000) throw new Error('input timeout');
```

RPC失敗・期限超過時には端末の正式入力を採用せず、trackを停止する。
host側の同一期限timerも部分grantを失効させる。再認可は新track・現在revisionで行う。
RTP統計をgrant取得後まで止めると開始前検査との相互待ちになるため、その順序は禁止する。

## 制御schema

methodは`local-gpt-live.control.v1`。UTF-8で8192 bytes以内の単一JSON objectだけを受け付ける。
共通必須fieldは`v:1`（整数）、`type`、`session_id`、`binding`、`request_id`。
識別子は非空・制御文字なし・UTF-8で256 bytes以内。未知field、重複key、配列、コメント、
フェンス、NaN、bool/floatによる整数偽装を拒否する。text内のJSONはtextとしてだけ扱う。

| type | 共通field以外の必須field | adapterへの対応・成功 |
| --- | --- | --- |
| `open_input` | `track_sid`, `expected_revision`（0以上のsafe integer） | `prepare_input`で統計とgrantを準備。`ok:true, grant, binding, remaining_ms`。readerは作らない |
| `input_ack` | `grant` | 保存済み実体へ`start_input`。`ok:true` |
| `confirm_output_ready` | `response_id`, `track_sid` | 同じ現行response/SID・期限内だけ`confirm_output_ready`。`ok:true` |
| `text` | 非空`text` | reader/pending失効後`Session.submit_text`。`ok:true, response_id, binding` |
| `mute` | `enabled`（bool） | マイク入力gate。trueで入力失効。`ok:true` |
| `focus` | `enabled`（bool） | text入力欄のfocus gate。trueで入力失効。`ok:true` |
| `cancel` | `response_id` | 現行応答照合後、受付失効→Session取消・queue消去。`ok:true, binding` |
| `reconnect` | なし | 論理再接続。入力・出力失効後`Session.reconnect`。`ok:true, binding` |
| `close` | なし | 受付・入力・旧出力を同期失効し終了受付。`ok:true, binding`。資源cleanupは非同期 |

grantはexact field集合`track_sid, request_id, input_generation, input_revision`。
後二者は正のsafe integer。JSONから`InputGrant`を復元せず、保存したgrantの全値・型と照合する。
client revisionは期待値の比較だけに使い、次revisionはhostがBackend現在値から決める。
Roomの送信者identityとSID、host所有Session、bindingを副作用前に照合する。
同identityの再参加を旧SIDとして認可しない。SID交代を検出したactive hostは失効する。

## 期限、拒否、再送、失効

入力は最初のopen受付からACKまで5秒、SDK callerのresponse budgetが短ければその時間まで。
統計待機・終了streamの回収待機・Backend reset・ACK待機が同一期限を消費する。
再送は同じopen内容だけを同じgrant・残期限で返す。異なる内容は`input_conflict`で拒否。
ACK欠落時はtimerから失効し、期限後のACKもtimer実行前に直接拒否する。
期限内の同じACKはactive grantに限り冪等。現行grantへの不正ACKや開始失敗は部分認可を失効する。
旧revisionのACKは新grantを失効させず拒否する。

出力readyはpublish後から`output_ready_timeout`まで（既定3秒）。
SID・response・control bindingが一致し、受信側が購読と再生準備を確認してから送る。
publishやSegmentSentからreadyを作らず、ready前はcaptureしない。
期限後はreadyの再送も拒否するが、期限内ready済みの同じ出力は後続segmentも送信できる。

muteとfocusは独立して保持し、いずれかtrueならopenを拒否する。同値gate再送は冪等。
falseへ戻すだけでは旧trackを再開しない。text・cancel・reconnect・closeはpendingと入力を失効し、
旧ACK受付を閉じてからSessionと旧出力を停止する。control bindingも回転する。
旧bindingのtext/停止再送は明示拒否。cancelは旧responseも拒否する。
SDKの実`reconnecting`/`disconnected`は単回transportの終了であり、論理reconnectと異なる。
実切断・SID交代の後は認証をやり直して新transport/hostを作る。
旧準備、旧timer、native capture/publishの遅着は新grant/応答を変更しない。
close応答はcleanup完了ではなく終了受付。`shutdown_pending`は資源解放成功ではない。

拒否応答は`ok:false, reason`。公開reasonは`invalid_control`、`unauthorized`、`stale_binding`、
`stale_revision`、`stale_grant`、`stale_response`、`input_conflict`、`input_suppressed`、
`input_timeout`、`input_start_failed`、`operation_invalidated`、`output_not_ready`、
`playback_ack_rejected`、`operation_failed`。内部例外の本文は返さない。
SDKの配送失敗やRPC自体のtimeoutもあり得るため、失敗を成功へ変換しない。
入力timeout/失効後は現在revision・新trackを取得して明示再認可する。
認証済みで対象Sessionに一致する制御応答は、成功・拒否とも後述の現在状態を付ける。応答の`binding`は`control_binding`と同じ値。
端末は全操作（gate解除、取消、論理再接続を含む）の応答を`acceptState`へ渡し、
次openにはこのBackend現在revisionを使う。自己申告revisionでBackendを書き換えない。
`stale_binding`/`stale_revision`にも現在状態があるため、状態を更新し、操作結果を拒否として扱う。
新しいrequest_idで利用者が現在状態に対して選んだ操作を送る。textや取消を盲目的に再送しない。
不正JSON、別Session、未認証への応答には現在状態を付けない。

## 通知と既存実再生ACK

topicは`local-gpt-live.events.v1`。`publish_data(reliable=True, destination_identities=[authorized_identity])`
で宛先を限定し、送信直前にRoomのSIDとcontrol bindingを再照合する。
未送信通知はscope失効時に破棄する。既にSDKへ渡したpacketの取消は保証せず、
端末もconnection_id・state_sequence・responseを照合して旧通知を捨てる。通知queueは512件で上限超過を固定reasonで観測する。
通知失敗はreadyやACKを生成しない。本文、PCM、token、生例外は通知・応答・公開ログへ含めない。

| type | fieldと意味 |
| --- | --- |
| `host_state` | 接続時の初期状態。通知購読はhost接続前に登録する |
| `response_started` | `response_id`。textとSTT起動の双方で新control bindingを伝える |
| `input_invalidated` | `reason="input_invalidated"`。ACK期限切れ、gate、stream終了・読取失敗などによるgrant失効後の状態 |
| `response_failed`, `transport_failed`, `input_failed`, `input_rejected` | `response_id`（入力のみの失敗はnull）、`reason`はイベント種別の固定値。native例外や本文は含めない |
| `output_track` | `v, response_id, track_sid, binding, control_binding`。bindingはACK用、control_bindingはreadyを含む制御用 |
| `segment_sent` | `v, response_id, sequence, track_sid, sample_rate, sample_start, sample_end, binding`。SDKへ渡せた半開区間 |
| `generation_completed` | `v, response_id, final_audio_sequence`。生成完了。送出・再生完了ではない |
| `output_estimated_completed`, `output_estimated_stopped` | `v, response_id`と既存snapshotのgeneration/track/rate、BE時刻、凍結時刻、送出・推定sample末尾、sequence、complete、basis、real_playback_confirmed。全block台帳は送らず、送出区間は個別metadataで通知 |
| `playback_stopped`, `response_cancelled` | `v, response_id`。旧端末scopeの停止 |

制御応答（認証済み・対象Session一致）と全通知は、共通して`session_id, connection_id,
state_sequence, control_binding, input_revision, input_active, muted, focused,
active_response_id, closed`を持つ。connection_idはhostの寿命に固定された接続識別子であり、
新hostでは必ず変わる。論理reconnectは同じconnection_idを保つ。state_sequenceは同じhost内で
状態を直列化するたびに増える。RPC応答と通知で共有し、到着順が逆転しても小さい値の状態は採用しない。
イベント本体の処理は分け、同じ現行control binding/responseのmetadataは状態が古くても処理する。
入力失効は古いinput_revisionで現在のgrantを停止させない。
`input_active`は準備済みを含むBackend grantの存在であり、正式PCM開始の証拠はinput_ack成功。

STT→submit_audio→response_startedは新control_bindingを通知し、output_trackも同じ制御状態を
含む。端末はこの状態を採用して、そのresponse_id/track_sidでreadyを送り、cancelにも
control_bindingを使う。output_trackとsegment_sentの`binding`だけは既存ACK専用であり、
制御へ流用しない。音声起動にtext RPCの戻り値は不要。

失敗後は通知のactive_response_idで応答表示、input_activeで入力認可表示を更新する。
入力失効では新revision・新trackで再認可し、response_failedでも次textまたは音声入力へ進める。
transport_failedは処理失敗の通知で、RTC切断を意味するとは限らない。Roomが存続する場合は
次RPCで状態を再確認できる。通知配送自体の失敗は同じ配送経路へ再通知しない。
端末はSDKエラー・RPC timeoutを失敗表示し、成功を推測しない。生きている接続では既知のbindingで
制御を送ると、stale_binding拒否を含め現在状態が返る。実RTC切断では旧connection_idを失効させ、
新hostの認証済み接続設定とhost_stateを受け直す。切断済み経路から停止通知が届く保証はない。

推定は必ず`basis="sdk_submitted_elapsed", real_playback_confirmed=false`。
音声なしの終了にはsnapshotがないため`response_id`とイベント種別だけを返す。
metadataのsequenceはbridgeへ渡す際に`audioSequence`、sample fieldは対応するcamelCaseへ変換する。
generation通知がsegmentより先に来る場合もあり、最終metadataを全て受領してからbridge.finishを呼ぶ。

ACK methodは`local-gpt-live.playback-ack.v1`。既存v1の`playback_ack`/`playback_complete`
wireをそのままpayloadへ渡し、認証後`PlaybackAckTransport.receive`の結果を`ok`で返す。
bridgeのsendはこの`ok`をbooleanとして返す。SDK送出成功を受理結果にしない。
SegmentSentだけからACKを作らず、RTP受信PCM量も実再生証拠にしない。
RTPと識別済みPCMの対応測定は別Issue。推定終了に実再生ACKを必須化しない。

## #14への引継ぎと未受入範囲

#14は認証済みアプリによるSession/connection_idの受渡しとhost_stateの初期状態同期、公式SDK RPC呼出し、
新マイクtrackのpublish、入力ACK、device gate、通知購読、出力readyを実装する。
output_trackからcontrolBindingを更新し、停止・応答切替時にローカル旧scopeを閉じる。
マイクのechoCancellation/noiseSuppression、AudioContextのユーザー操作による準備もUI側の責務。
このhostは公式SDK登録と通知まで実装しているが、公式ブラウザSDKと実LiveKitを通る会話は未受入。
合成UT/IT1、既存browser IT2と、実マイク・実会話・聴感のSTを区別する。
実サービス接続、GPU/STT/TTS起動、資格情報変更、配備、常駐追加は本作業で行わない。
