# BE送出済み音声ブロックの取得と進捗推定

LiveKitへ渡した応答音声について、送出済みsample範囲とBEの経過時間から進捗を取得する。
取得対象は音声ブロックの記述子であり、PCM本体や会話本文の複製保存ではない。
設計判断は[ADR 0005](adr/0005-sent-audio-progress.md)を参照する。

## APIと取得対象

```python
progress = transport.sent_audio_progress(response_id)
# 同じBE processの単調時計で指定した時点までの進捗を取得する。
progress_at_decision = transport.sent_audio_progress(response_id, at_ns=decision_ns)
if progress is None:
    # 公開trackに束縛済みで、保持中の応答だけを取得できる。
    pass
```

`at_ns`を省略すると、取得時のBE単調時計を使う。指定する場合も同じprocessの
`time.monotonic_ns()`を使い、UTC、ブラウザの時計、RTP timestampと混ぜない。
取得は音声送信や再生ACKを発生させない。

取得できる場合の返り値は`SentAudioSnapshot`で、ブロック一覧`blocks`は
`SentAudioBlock`のtupleとなる。track公開に成功して台帳へ束縛できた応答だけが対象で、
未知のID、まだ公開前の応答、保持対象より古い応答では`None`を返す。
主なfieldの意味は次のとおり。

| field | 意味 |
| --- | --- |
| `at_ns` | 呼出側が指定した評価時刻 |
| `effective_at_ns` | 取消による固定時刻を適用した実際の評価時刻 |
| `frozen_at_ns` | 最初に固定した時刻。未固定なら`None` |
| `submitted_sample_end` | 評価時刻までにSDK書込みが成功した連続範囲の末尾 |
| `estimated_sample_end` | 遅延とframe音声時間を反映した推定範囲の末尾 |
| `estimated_audio_sequence` | 完全に通過した連続ブロックの最後の0始まりsequence。1つもなければ`-1` |
| `estimated_complete` | 登録済みブロック全体の推定通過。ブロックなしでは`False` |

`estimated_complete`は登録済みブロックについての値であり、生成完了・応答完了・
Session完了の意味を持たない。後続ブロックが追加されれば判定対象も増える。

記述子は次の対応を保持する。

| 対象 | 内容 |
| --- | --- |
| 応答 | `response_id`と当該出力作成時のSession generation |
| 出力 | 当該応答のtrack SIDとsample rate |
| ブロック | 0始まりのaudio sequenceとtrack内の論理sample範囲 `[start, end)` |
| 送出進捗 | SDKへの書込み成功を確認した部分範囲とBE単調時計 |
| 推定進捗 | 完全に通過した連続ブロックのprefixと、途中ブロックの部分範囲 |
| 根拠 | `basis="sdk_submitted_elapsed"`、`real_playback_confirmed=False` |

完全ブロックのprefixと部分進捗は別々に扱う。途中まで送ったブロックを完了済みの
audio sequenceへ繰り上げない。末尾まで送っていないブロックも、その時点までの
部分進捗は取得対象とする。

各ブロックの部分範囲は`[sample_start, submitted_sample_end)`と
`[sample_start, estimated_sample_end)`。未送出・未推定なら末尾も`sample_start`となる。
`first_submitted_at_ns`と`last_submitted_at_ns`はSDK書込み成功時刻で、未送出なら`None`。
過去時刻を指定したsnapshotでも登録済みブロックの計画一覧は表示するが、進捗はその
評価時刻までに成功した記録だけから算出する。計画の登録時刻を復元するAPIではない。

台帳はtrack公開成功後に作成する。最初のブロック計画は`on_track` callbackが戻った後、
端末の出力ready待ちより前に登録する。このため、callback中の取得ではブロックなし、
ready待ち中の取得では未送出のブロック計画が見える場合がある。
計画が表示されてもSDKへ音声を渡したことにはならない。

## 「送出済み」の境界

既存の出力準備確認を通過した後、`AudioSource.capture_frame()`が正常完了した
約10msのframeごとにsample範囲と成功時刻を記録する。TTS生成済み、`Playback`の
queue投入済み、track公開済み、準備完了の通知だけでは進めない。

この時点が示すのはSDKがPCMを受け付けた事実である。FEへの到達、RTP packetへの
正確な対応、復号、ブラウザ出力時計の通過は別の事実として扱う。
frameごとのsample数から時間を求めるため、端数frameを一律10msと数えない。

既存の`SegmentSent`は、ブロック全体をSDKへ渡し終えた論理範囲の通知のままとする。
新しい取得APIは、その通知前に取消となった部分ブロックも観測するためのもの。
`SegmentSent`を再生確認へ変更せず、再生ACKの送信・受付も行わない。

## 時間による推定

`LiveKitConfig.estimated_downlink_delay`は秒単位の非負・有限値で、既定値は`0.3`。
実測済み遅延を表す設定ではなく、下りネットワーク、受信側buffer、出力経路について
置く仮定である。現在のSDK source queueは従来どおり100msを使用する。

判定に使う時計は次のとおり。

```text
判定cutoff = 指定時刻 − estimated_downlink_delay − SDK queueの100ms
frame推定終了 = max(capture成功時刻, 前frame推定終了) + frameの音声時間
```

frameの音声時間はsample数とsample rateから算出する。時間だけで未送出範囲を増やさず、
ns未満の端数は切り上げ、SDKへ渡せた範囲を上限とする。captureが短時間に続けて成功しても、全frameを
その瞬間に再生済みとは推定しない。供給が途切れたときは次のcapture成功時刻から
時間を数え直し、過去の無音待ち時間を新しい音声の進捗に繰り越さない。
推定終了時刻がcutoffを通過したframeだけを含め、frame途中を時間比例で補間しない。

この計算はメディア時間を下限として置くモデルであり、RTPと元PCMの対応を証明しない。
設定値より大きな通信・再生遅延、音声損失、PLC、ブラウザの停止や端末muteがあれば、
推定が実際の出力より先へ進む可能性がある。短い側へ丸めることだけで過大推定を
なくせるとは扱わない。

## 取消と保持期間

取消、割込み、旧応答の失効では、出力停止とともに台帳の判定時刻を固定する。
固定後に取得時刻を進めても、その時刻より後の経過時間や遅着captureによって
推定範囲を増やさない。失効後にSDK操作が戻った場合も旧応答へ追記しない。
新応答では新しいscopeへresponse、当該出力作成時のgeneration、trackを束縛し、
旧scopeを再利用しない。generationが同じまま次の応答へ進む場合も、旧音声を混ぜない。

保持対象は公開trackへ束縛できた最新の応答とその直前の応答に限定する。
新応答の生成開始だけでは入れ替わらず、新しいtrackの公開と台帳への束縛に成功した時点で
保持対象を更新する。さらに古い応答の進捗を永続保存する履歴APIではない。

台帳は1応答あたり既定20,000件のframe成功記録、256ブロックに制限する。
約10msのframeでは記録上限は約200秒分に相当するが、音声時間の保証やproviderの
期限設定ではない。上限超過は`ValueError`となり、既存の記録を変更しない。
transport内で台帳追記の上限超過や時計の逆行を検出した場合は、既存の出力失敗経路へ進む。
応答取消、queue消去、切断・資源回収を行い、既存の台帳を固定する。
保持できない範囲を送出済みへ補ったり、記録を捨てながら通常送出を継続したりしない。

## 既存ACK・Sessionとの関係

今回追加するのは取得APIと、そのための送出記録・進捗推定である。
推定結果から`acknowledge_playback()`や`playback_completed()`を呼ばず、
`Playback.all_confirmed`、Sessionの完了判定、Coreへ渡す履歴範囲を変更しない。
生成が終わり推定上の全ブロックが通過しても、実再生ACKがなければ再生確認は未確定。

[実再生ACK契約](adr/0002-playback-ack.md)と
[ブラウザbridge](browser-playback-bridge.md)は既存の意味を維持する。
PCM別配送への置換、browser UI、認証済みRPC endpointはこの変更に含まない。

## 旧PoCとの対応

参照元は`digital-souls`の固定commit
[`fce7382884d981c42be7fbd3ddaffe7469e27588`](https://github.com/FYuki/digital-souls/tree/fce7382884d981c42be7fbd3ddaffe7469e27588)。

- [旧推定ADR](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/voice-playback-estimation-speech-services-2026-09.md)
  はBE送出位置と下り遅延からの推定、区間境界への切り下げを定義する。
- [旧pacer](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/backend/app/livekit_transport/paced_audio.py#L53)
  はcapture成功時刻と累積sample数を記録する。
- [旧応答音声adapter](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/backend/app/livekit_transport/response_audio.py#L193)
  は中断判断時刻から下り遅延を引き、完了したブロックのprefixへ丸める。

旧PoCはSDK queueを0msにし、独自の10ms pacerで送出していた。今回は既存adapterの
100ms queueを維持するため、queue分と音声時間を推定モデルへ明示的に含める。
旧PoCが推定値を応答終了・履歴へ採用する部分は、今回の取得APIへ移植しない。
旧PoCの[受入記録](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/validation/issue-3-acceptance-20260929.md)
でも推定値と実会話出力の差分計測は未実施であり、既定300msを測定済み性能値としない。

## 検証

合成PCMと偽SDK、制御した単調時計で、送出成功前の0進捗、部分ブロック、連続prefix、
frame音声時間、供給空白、取消後の固定、遅着capture、応答交代、保持上限を確認する。
推定結果を取得しても実再生ACKやSession完了が生じないことも検証する。
実行手順は[検証と実音声受入](testing.md)を参照する。

この検証ではLiveKit接続、token発行、サービス変更、ブラウザやマイクの起動は行わない。
実ブラウザ出力区間との対応と、物理的に聞こえたことの確認はこの取得APIの保証対象外。
