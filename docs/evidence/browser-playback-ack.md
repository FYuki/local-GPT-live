# ブラウザ再生 ACK の合成検証

2026-10-05、`browser/playback-ack.mjs` の純粋な ES モジュールを、Node 標準テストの合成イベントで確認した。外部通信、音声機器、実音声、LiveKit は使用していない。

| 実行コマンド | 結果 | 確認範囲 |
| --- | --- | --- |
| `node --check browser/playback-ack.mjs` | 終了コード 0 | モジュールの構文 |
| `node --test browser/tests/playback-ack.test.mjs` | 終了コード 0 | この環境ではファイル単位の 1 件が成功と表示された |
| `node browser/tests/playback-ack.test.mjs` | 終了コード 0、17 件成功、失敗 0 件 | 個々の合成テストの実行 |

合成テストは、順序外のメタデータ・描画証拠からの個別 ACK、部分描画と不一致の拒否、重複の冪等性、世代失効、遅延した Promise、同期・非同期の ACK 失敗、明示的な再試行、有限の保留上限を観測した。

ホスト側の固定チェックは **pending**。この文書はホスト検証、実ブラウザ描画、実音声再生、LiveKit 接続の成功を示さない。
