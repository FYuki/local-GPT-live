# 2026-10-05 ブラウザACK統合の検証

この記録は合成PCMによるローカル検証です。実ユーザー音声、Room接続、マイク、JWT、GPU、
共有サービスの変更は使用していません。物理出力装置の可聴性やRTPの元PCM対応は未受入です。

## 初回ローカル検証時の統合元

- 公開epic: `3f74ea79bd62a72993ca90707fbe34f7e06080d0`
- PR2公開head: `2459c9bf445b5bbfe006e220e0211f3c623a1444`
- 未公開browser依存: `e1503696c002e3b2fcaf35c5e38b6d770fed1f51`
- ローカル依存統合head: `686e36a`
- 独立worktree: `/home/asa/dev/local-gpt-live-browser-integration`
- branch: `feature/browser-playback-bridge-local`

初回は元worktreeと純粋モジュールの5ファイルを変更せず、PR2の別worktreeにある未push
`502cc88`も取り込みませんでした。未公開依存を含んでいたためpush/PR作成を行わず、
cloneのorigin push URLを`DISABLED_UNPUBLISHED_DEPENDENCIES`に設定していました。

## 公開時の依存整理

- [PR5](https://github.com/FYuki/local-GPT-live/pull/5)の公開head
  `e1503696c002e3b2fcaf35c5e38b6d770fed1f51`とremoteを再照合し、CI成功後に通常merge。
  epicのmerge commitは`154f9991f017c109db834945c4e5b74da9e32079`。
- [PR2](https://github.com/FYuki/local-GPT-live/pull/2)も公開head
  `2459c9bf445b5bbfe006e220e0211f3c623a1444`のCI成功を確認して別途通常merge。
  epicのmerge commitは`7f4254c7409a5728585ed2497be9e57029550cb0`。
- PR5統合後は[Voice foundation](https://github.com/FYuki/local-GPT-live/actions/runs/37263219540)、
  [Bootstrap](https://github.com/FYuki/local-GPT-live/actions/runs/37263219483)、
  [Browser ACK](https://github.com/FYuki/local-GPT-live/actions/runs/37263219541)が成功。
- PR2統合後も[Voice foundation](https://github.com/FYuki/local-GPT-live/actions/runs/37263311606)、
  [Bootstrap](https://github.com/FYuki/local-GPT-live/actions/runs/37263311603)が成功。
- 最新epic `7f4254c`から公開用branch `feature/browser-playback-bridge`を作成。
  worktreeは`/home/asa/dev/local-gpt-live-browser-publish`。
- 独自の変更`c9d666a`と`14d137f`だけをcherry-pickし、公開用commitはそれぞれ
  `26c697e`、`ad0ad0c`。古い依存merge `cb909ce`と`686e36a`は公開履歴へ持ち込んでいません。
- 移植直後のtreeは検証済み`14d137f`と完全一致。公開文書の時点表現だけを後続更新します。
- 未pushの`502cc88`は追加回帰テストと証跡だけで製品依存ではありません。今回も含めず、
  所有者による別途公開の残件として扱います。純粋ACKモジュール5ファイルは変更しません。

## 検証境界

1. 純粋controllerの既存Nodeテスト。
2. bridgeのNodeテスト。backend受理待ち、取消、遅延、期限、重複、連続性、容量回復。
3. Node bridgeからstdin/stdout fixtureを介して実Python VoiceSession/受信クラスへ渡す結合試験。
4. 実Chromium AudioWorkletの出力・出力時計・renderer/bridge接続。headlessのsoftware render。
5. 実Chromium renderer→bridge→Python受信クラス→VoiceSessionの全境界結合。

実ブラウザのtest harnessはPlaywrightで仮想loopback originをroute応答し、常駐HTTP serverを
起動しません。ChromiumのWorklet HTTP fetchはrouteを通らないため、実装の未変更本文を
Blob URLとしてロードします。default HTTP module配信経路の検証は含みません。
許可外originへのpage通信はabortし、マイクAPIを呼びません。
OfflineAudioContextの3件は、高速renderとpostMessage配送の競合を避けるため、実processorを
継承したfixtureのconstructorで既存入力handlerへPCMを渡します。processと出力bufferは実物です。
live AudioContextの9件は実MessagePortで投入し、成功ケースはnative出力時計を使います。

## 再現手順

Pythonの固定環境は`uv sync --frozen --extra livekit`で準備します。
NodeはUbuntuの24.20.0、Playwrightは既存1.61.1、Chromiumは既存build1228を使用します。
新規npm依存の導入・lockfile変更はありません。

```sh
node --test browser/tests/playback-ack.test.mjs
node --test browser/integration/playback-ack-bridge.test.mjs browser/integration/playback-ack-host.test.mjs
PLAYWRIGHT_MODULE=file:///home/asa/dev/digital-souls/frontend/node_modules/playwright/index.mjs \
CHROMIUM_EXECUTABLE=/home/asa/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome \
node --test browser/integration/pcm-renderer.browser.test.mjs
```

ブラウザ補助試験は既存Playwright/Chromiumの場所を環境変数で指定します。
未インストール時は自動インストールやskipを行わず、起動エラーになります。
新規workflowはNode bridge/実Python host結合の実行を定義します。
ブラウザ補助試験のCI導入は今後の固定toolchain整備が必要です。初回ローカル検証では
リモートCIは未実行でした。公開branchの最新CIは接続層PRのChecksと検証欄で追跡します。

## 結果

| 検査 | 結果 |
| --- | --- |
| Python全体 | 213 tests + 14 subtests成功 |
| 既存純粋controller | Node 23 tests成功 |
| 新規bridge | Node 26 tests成功 |
| Node→実Python host | 5 tests成功 |
| 実Chromium | 12 tests成功、skipなし |
| ruff / mypy | 成功、mypy対象17 source files |
| 文書・repository・diff検査 | 成功 |
| voice-demo | 合成7シナリオ成功 |
| uv build | wheel/sdist成功、worktree `.git` 非収録 |

全境界結合はactual AudioContext 16000Hz、既知PCM 1600samples×2です。
ACK0/ACK1受理時の`completed_count`はともに0、別の完了通知後だけ1となり、activeはnullでした。
部分出力は300sample中128sampleの先頭一致、underflowは173sampleの既知PCMと339sampleの
補充ゼロを別々に確認しました。同期disconnect後の実出力もゼロでした。
これらは生成済みと再生済みを混同しないsoftware renderの検証です。

ローカルレビュー指摘は反映済みです。CodeRabbit枠は使用していません。

## レビューで修正した点

- SDK送信成功ではなく、backend受理boolを待ってACKを確定。
- タイマーがイベントループ停止で遅れても、単調時計のdeadlineを送信前・受理後に再確認。
- AbortSignalを無視して残留する実送信を1件に制限し、settle後の明示retryで回復。
- rendererの証拠通知を同期observerに限定し、ネットワーク期限の失敗と描画失敗を分離。
- 出力時計の過去最大値は使わず、各通知時の実際の位置で判定。
- 満杯だったrendererの枠を通知前に解放し、同期observerから次PCMを投入できるように修正。
- 非同期失効callbackが新scope開始と競合しないよう、`onInvalidate`も同期契約を強制。
- 独立worktreeの`.git`ポインタがsdistに含まれていたため、
  [Hatchの除外設定](https://hatch.pypa.io/1.13/config/build/#patterns)で除外し、配布物を再検査。

後続は[接続手順](../browser-playback-bridge.md)と[提案ADR](../adr/0004-browser-playback-bridge.md)を参照。
