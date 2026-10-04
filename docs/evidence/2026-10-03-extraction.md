# 音声基盤抽出の検証記録（2026-10-03）

検証対象コード: `5b417b4dc8db96f86725154a83bfc407c8bdd87f`。
環境: WindowsからWSL Ubuntu、Python 3.12.3、専用workspace/worktree、独立venv。
PoC参照版: `fce7382884d981c42be7fbd3ddaffe7469e27588`。

## 実行結果

一次証跡は[検証出力](2026-10-03-checks.txt)。

| 検証 | 結果 |
| --- | --- |
| pytest | 72 passed |
| ruff | 合格 |
| mypy strict | 14 source files、合格 |
| 日本語docsの入口・相対リンク | 合格 |
| sdist/wheel build | 合格 |
| wheel内VAD資産・ライセンス・fixture | 同梱確認 |
| CLI合成イベント | 無音・かぶせ・割込・連続・reconnect・cancel・timeoutの7/7成功 |
| git diff --check | 合格 |

実CPU VADの資産初期化・状態分離・sample境界・buffer上限、HTTP mockによる
Whisper/Core/VOICEVOX/Irodori契約、実loopback socketによるCore取消切断を検証した。
取消を無視する遅延TTS、旧世代STT、800ms preview、STT待機上限、
TTS部分失敗時の旧queue消去と後続復帰も確認した。

## 未実施・未統合

- 実STT/TTS・GPU・Core実モデルの試験は未実施。Ubuntu-dogfoodのサービス・Docker・設定は変更していない。
- LiveKit RTC接続、認証済みtrack・受信統計、ブラウザのdevice gate/再生ACKは未統合。
- 実マイク・スピーカー・人の聴感受入は未実施。合成fixtureから品質・遅延の合格を推定しない。
- 300ms静音でのSTT準備先行、実再生prefixの履歴連携、読み辞書は未統合。
- CodeRabbit独立レビューは未依頼。新repoへのApp接続は未確認。
- remoteが空でmainがないため初回main公開承認待ち。PRとGitHub CIは未作成・未実行。
  ローカル成功をepic/mainのCI greenとして報告しない。

## 公開対象の確認

声model・参照音声・録音・会話DB・env・秘密keysは追加していない。
VADのみPoCの配布元/hash/同梱MIT・BSD許諾を照合し、ライセンスを保持した。
キャラクターカード/画像も今回の基盤に不要なので含めていない。
repoのPUBLIC visibilityは変更していない。

公開とdraft PR作成の手順は[初期化手順](../initialization.md)を参照。
