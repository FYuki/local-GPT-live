# 固定VAD資産

音声録音・声合成モデルではなく、既存PoCのCPU発話検出資産です。

| 資産 | 配布元・SHA256 | 許諾 |
| --- | --- | --- |
| silero_vad_legacy.onnx | @ricky0123/vad-web 0.0.30 dist / a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28 | [MIT](LICENSE.silero) |
| libfvad.wasm | PoC frontend vendor固定版 / 3fadafc9c5c1c3117d0178ae631484c6dabc72e0f8742b95c370cd39f46cf171 | [BSD](LICENSE.libfvad) |

runtimeでhashとI/Oを検証し、モデル取得はしません。h/c・WASM状態はSessionごとに分離します。
