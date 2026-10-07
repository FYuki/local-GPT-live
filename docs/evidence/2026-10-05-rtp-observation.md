# RTP観測境界のオフライン検証

2026-10-05、WSL Ubuntu。baseは`epic/transport-playback`の`b4a4b8e`。
専用branch `feature/rtp-observation-harness`で実施した。

最終公開検証対象HEADは`ba141d92aeeaa6bd9bddfc84d4178b3cf901eb2a`。
[同HEADのVoice foundation CI](https://github.com/FYuki/local-GPT-live/actions/runs/37267971489)も成功した。
当日のローカル実行時HEADは未記録のため、公開HEADと同一だったとは断定しない。
下記の最終HEAD再確認と、当日のローカル試験記録を区別する。

## 実行した検証

- Python 3.12.3、uv 0.8.22、`livekit==1.1.16`。既存lockで`uv sync --frozen --extra livekit`成功。
- Python全体237件＋14 subtests成功。統計clockの未確認表示とfixture名の修正後、追加5件を再実行して成功。
- ruff、mypy（17 source files）、docs相対リンク、build成功。合成`voice-demo`の7シナリオも成功。
- `tools/rtp_observation.py`のCLI成功。通常CIのpytest対象に追加5件が含まれる。

## 確認した境界

実SDKのAudioFrame bufferはpointer、channel数、rate、sample数のfieldを持ち、
`userdata`内の元PCM位置やresponse IDをそのbufferへ渡さない。
CLIはpointerの値やPCM本文を出さず、field名だけを記録する。

連続受信、同数の置換、取消後遅着、受信rate相違の4fixtureで、元PCM帰属とACKは常に未確認。
protobufで未設定の統計はnullとして保持する。統計のcodecとsample時計は未観測なので、
raw counterとAudioFrameのsample数を同じ単位とは扱わない。

数値は合成fixtureであり、encode/decode、PLC、resampling、実際の取消処理は実行していない。
CLI試験ではRoom・AudioSource・socket生成を禁止して成功することも確認した。
新規接続、token、マイク、共有サービス、transport、production/browserコードは変更していない。

## 未確認

元PCM→送出RTP原点→受信packet→復号区間→実ブラウザ出力時計の対応は未確認。
物理可聴性を保証しない。次の観測点と親確認が必要な実接続範囲は
[測定計画](../rtp-observation-plan.md)を参照する。

## 最終HEADの再確認（2026-10-07）

保持されたWSL Ubuntuの専用worktreeで、HEAD
`ba141d92aeeaa6bd9bddfc84d4178b3cf901eb2a`を照合して再実行した。
既存lockの`uv sync --frozen --extra livekit`、Python全体237件＋14 subtests、
`tools/rtp_observation.py`のCLIが成功した。4fixtureすべてで元PCM帰属とACKは未確認のまま、
`connection_attempted=false`を確認した。実行前後のHEADとtracked/untracked状態も変化していない。
