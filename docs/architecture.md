# 現在の構成と統合境界

`AudioInput` → `BackendVoiceInput` → CPU VAD/区間検出 → 上限付きPCM capture →
`VoiceSession` → Whisper HTTP → 相槌/発話権判定 → Core SSE → VOICEVOX HTTP → `Playback`。

実装済みの純粋ロジックはPoCから移植し、HTTP adapterと外部Coreを呼ぶ会話制御を分離した。
Coreへはaliasと確定textを送る。人格prompt、tool実行、長期記憶、会話DBは持たない。
現在のCore adapterは各要求に最新user入力だけを渡す。履歴統合は別タスクのAPI確定後に接続し、
未再生部分をassistant履歴へ保存する実装を先回りして追加しない。

## 移植対応表

元repoは[固定コミット](https://github.com/FYuki/digital-souls/tree/fce7382884d981c42be7fbd3ddaffe7469e27588)。

| PoCの実装 | 本repo | 変更 |
| --- | --- | --- |
| backend/app/voice_input/* | src/local_gpt_live/voice_input/* | import namespaceとNumPy型注釈。CPU Silero/libfvad、hash、閾値、世代制御を維持 |
| conversation_core/turn_decision.py | turn_decision.py | 同じ分類器 |
| livekit_transport/stt_audio.py | stt_audio.py | 同じ前処理・範囲検査 |
| remote_whisper_client.py | providers.Whisper | 同じraw PCM endpoint、async HTTP adapter |
| voicevox_client.py | providers.Voicevox | 同じquery/synthesis endpoint、Session期限とasync取消へ接続 |
| conversation_coreの人格/永続化/LLM経路 | providers.CoreChat | コピーせずCore Chat Completions API |
| LiveKit RTC・FE再生観測 | 最小RTC adapter・FE未統合 | [接続API](livekit-adapter.md)で既存境界へ接続。ブラウザwire/実再生ACKと実接続受入は後続 |
| Irodori HTTP/固定voice設定 | irodori.Irodori | 既存APIで登録済みvoiceを参照。声model/audioの同梱なし。engine自動fallbackなし |

## イベント・取消契約

- 入力PCMは16kHz mono little-endian PCM16。grantはBackendが採番し、別trackで再認可する。
- 入力sample時計とwall clockを混同しない。無音preroll最大64,000 bytes、発話最大960,000 bytes、STT待機最大3件。
- 正式speech_started時のresponseをoverlap対象として保持する。candidateだけで回答停止しない。
- 既存と同じ有効信号800msごと・最大3回のSTT previewでtake-turnを先行判定する。
  相槌で始まる長い入力は再評価する。最終STTを優先し、preview失敗は最終STTへ進む。
- 相槌/曖昧反応は旧回答を継続する。take_turn、明示cancel、text優先、reconnectで旧出力を失効させる。
- 停止順序は出力失効・queue消去→playback_stopped→生成task取消→response_cancelled。
  遅いprovider完了はactive responseを再照合する。出力packetにもresponse_id/sequenceを付ける。
- LiveKit adapterは[送出台帳と経過時間](sent-audio-progress.md)をSessionへ接続し、生成完了・
  全ブロックのSDK投入・推定時刻の通過で`output_estimated_completed`としてactiveを終了する。
  発話開始時にもこの条件を確認してoverlapを捕捉し、捕捉後は書き換えない。入力世代は維持する。
  取消・割込み時は固定した完全ブロックprefixと部分範囲を1件保持する。文字数や履歴を推測で補わない。
- 推定取得先を接続しない基底Sessionでは、generation_completed後も未確認の回答は取消可能。
  配信済み区間を0始まりの連続番号で
  acknowledge_playbackへACKし、全区間を確認してからplayback_completedで終端へ進む。
  consumeやqueueの空だけでは再生完了にしない。[Backend受付契約](adr/0002-playback-ack.md)を参照。
- `Playback.consume`は端末への引渡しであり、実出音の証拠ではない。実adapterはstop時にデバイスqueueも消す。
- provider taskは最大4件、出力queueは4MB、回答textは16,000文字。容量超過・timeoutは失敗通知し後続会話を受ける。
- Session.closeは取消後1秒までdrainを待つ。取消を無視する外部providerはshutdown_pendingを通知し、成功扱いしない。
- 生本文・PCM・native例外はイベントへ出さず、種類・response ID・固定reasonだけを返す。

Core取消はHTTP stream close。GPUジョブそのものの停止保証とは異なり、ローカル出力失効を先に保証する。
既存mainではOllama streamingは未対応。llamacpp PR #15の未マージ状態をmainの機能として扱わない。

## 実transport接続時の必須条件

最小RTC adapterはホストが指定したparticipant identity/SIDと新trackの照合、受信統計とPCM欠落の検査を持つ。
ホスト側の参加者認証、5秒以内のネットワーク入力ACK、
mute/focus/text/reconnectのdevice gate、echoCancellation/noiseSuppression、実再生範囲ACKは
まだこの最小ライブラリの外側にある。`BackendVoiceInput.open`だけをネットワーク公開してはならない。
残る境界を接続して実接続受入するまで、ブラウザ会話の完成版とはしない。
300ms静音時のSTT準備先行、実再生prefixと履歴保存の連携、キャラクター固有の読み辞書も未統合。
Irodoriはdev/testの既存登録voiceを参照するadapterのみ。voiceの選定・登録・配布は実施しない。
