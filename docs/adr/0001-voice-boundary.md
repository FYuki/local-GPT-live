# ADR 0001: かぶせ音声基盤の分離

状態: 採用。これは受入完了を意味しない。

## 根拠

digital-souls `fce7382884d981c42be7fbd3ddaffe7469e27588` の
`voice-backend-migration-contract.md`、`voice-backend-authority-2026-09.md`、
`voice-quality-improvement-2026-09.md` と実装を基準とする。
旧PoCの「Core」は音声会話制御を含むが、新digital-souls-coreとは別物である。

## 決定

- 本repoはPCM入力、CPU VAD、発話境界、STT、相槌/発話権取得、応答の取消、TTS、出力世代を所有する。
- 人格prompt、model profile、LLM通信は新Core APIを利用する。人格・長期記憶・Agent loop・履歴DBはコピーしない。
- GPU推論はUbuntu-dogfoodの既存サービスを利用する。開発はUbuntu、初期検証はfixture/mockのみ。
- 「かぶせ有り」は回答再生中も入力を受け、相槌は回答継続、take-turnは旧回答を取消して新回答へ進むこと。
  VAD開始だけで先行duck/pauseしない。曖昧な短反応は保留する。
- 取消は出力の失効・端末queue消去を先に行い、Core HTTP streamを閉じる。
  provider取消が遅れても失効したresponseの結果を再生しない。
- 再接続では入力trackと入力世代を更新し、旧PCM/STT/出力を排除する。再生完了と生成完了は別の事実。
- 既存VAD/区間検出/相槌分類/PCM準備を移植する。LiveKit Agentsへの全面書換えは行わない。
  transport固有の認証・track統計・ブラウザ実再生ACKは独立した統合対象とする。
- Coreのcancelは専用endpointを仮定せず、確認済みSSE切断による取消を利用する。
  Core mainのOllama stream未対応を隠す非stream fallbackは作らない。

## 受入契約

| 条件 | 観測する結果 |
| --- | --- |
| 無音 | 発話確定・STT呼出しなし、buffer上限内 |
| 回答への相槌 | 同じresponseの出力継続、余分なCore要求なし |
| 回答への割り込み | stop→Core取消→次応答、遅い旧TTS/旧tokenは排除 |
| 連続発話 | 確定入力の順序保持、待機容量超過は明示拒否 |
| reconnect | 旧入力grant無効、旧再生を再開しない、新track ACK後だけ入力 |
| cancel/timeout/失敗 | 旧出力無効化、後続会話で復帰、暗黙engine切替なし |

自動fixtureの合格、人の実マイク・聴感、実STT/TTS/LiveKitの受入は別に記録する。
新しい高度機能、品質閾値緩和、実会話録音、権利不明な声資産のコピーは対象外。
