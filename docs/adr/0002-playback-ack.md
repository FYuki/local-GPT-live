# ADR 0002: 実再生ACKのBackend受付境界

状態: Proposed。今回のPRはBackend契約の実装であり、LiveKit・ブラウザ実接続の受入ではない。

## 背景

`Playback.consume`は配信先への引き渡しだけを意味する。しかし従来の
`VoiceSession.playback_completed`は生成終了と配信queueの空だけで完了できた。
ブラウザが未再生のままでも、相槌や割り込みの対象responseが閉じる可能性があった。

[固定PoCの再生prefix処理](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/frontend/src/livekit/playback.ts)
は区間のsample数とrendered sample数を照合する。
[移設契約](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/voice-backend-migration-contract.md)
も生成・配信量で端末の再生事実を補完しない。
送出時刻からの推定を持つPoC内部adapterを、そのまま実再生証拠として移植しない。

## 今回の契約

- `AudioPacket.sequence`はresponseごとの0始まり。変更しない。
- `Playback.consume`は配信した区間番号を記録するが、再生済みにしない。
- `VoiceSession.acknowledge_playback(response_id, sequence)`は区間全体の実再生ACKを受け取る。
  呼び出すtransportは、認証済みparticipantと現在のsessionを照合してから渡す。
  このPython APIは認証APIや外部ネットワークの入口ではない。
- 最初のACKは0、以後は連続する番号だけを受け付ける。未配信区間、飛び越し、不正型、
  負数、現在と異なるresponseを拒否する。同じ有効responseの受理済みACKの再送は冪等。
  transportの再送queueは、この順序を保持し、複数区間を飛び越す累積ACKへ変換しない。
- 生成完了前でも配信済み区間のACKを受け付ける。ACK単独ではresponseを完了しない。
- 生成完了、queueが空、全配信区間の連続ACKがそろった場合だけ、既存の
  `playback_completed(response_id)`で終端へ進む。音声が0区間なら区間ACKは不要。
- cancel、失敗、timeout、reconnect、新response、closeは旧ACK受付を失効させる。
  ACK待ちは取消の条件にせず、端末停止の通知とprovider取消の順序を維持する。
- 状態は現在responseの配信番号・確認番号だけ。過去ACKの無制限保存や本文ログは追加しない。

## 後続adapterと履歴への条件

ブラウザ側はresponseに帰属するPCMと出力時計の照合を行い、端末queueの引き渡し、
SDKへの配信、生成終了、経過時間だけからACKを作らない。
LiveKit接続・認証・新track・入力grantの検証と、ブラウザ出力観測は後続の小さなPRで接続する。
この時点で実ブラウザがACKする経路は存在しないため、完成したブラウザ会話とは扱わない。

実再生prefixの履歴連携も後続判断とする。完了したTTS区間だけを連続prefixとして採用する案と、
部分区間の文字対応を必要とする案を区別する。現行TTSには文字とsampleの厳密対応がない。
送出量・時間から再生済み文字数を推定してCoreへ送らない。
[Core c10bd09の履歴API](https://github.com/FYuki/digital-souls-core/blob/c10bd09052dea26af81a7110e2ad1a9808c80236/docs/history-api.md)
は保存後のバッファ配信であり、現行の逐次token/TTS経路へ置き換えない。
セッション内文脈と永続履歴の保存policyは別の契約として確認する。

## 検証と制限

`tests/test_playback_ack.py`で未配信、順序違反、再送、不正型、旧response、生成中ACK、
生成終了と配信終了だけでは完了できないこと、cancel/reconnect/close/新responseを検証する。
合成CLIはfixture上の区間完了ACKを明示的に発行する。実端末の受入証拠ではない。
devの実行主体はユーザー指定のWSLディストリビューション`Ubuntu`とする。
共有推論endpointの所在と、実接続テストを実行するディストリビューションを区別する。
