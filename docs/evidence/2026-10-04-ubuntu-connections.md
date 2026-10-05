# Ubuntuでの合成実接続検証（2026-10-04 UTC）

関連: [PR #2](https://github.com/FYuki/local-GPT-live/pull/2)、
[検証方針](../testing.md)、[Backend ACKの自動検証](2026-10-04-playback-ack.md)。
ユーザーが指定するdevはWSLディストリビューション`Ubuntu`。実行ユーザーは`asa`。
今回の実行はUbuntuから行い、Ubuntu-dogfoodは共有サービスの状態確認だけに使用した。

## revisionと分離

- local-GPT-liveの検証head: `917720230095e6a1d11c28e1d21c779afb88cf30`。
- 製品コード: `48fef7b79df2426cf5088bb93ba9648344fb0098`。それ以降は文書更新のみ。
- 一時Core: main `c10bd09052dea26af81a7110e2ad1a9808c80236`の独立checkout。
- 既存PoC checkout: `fce7382884d981c42be7fbd3ddaffe7469e27588`。
- 既存Ubuntu Core checkoutは`0152cde861a1b1f5733c0145f405cd62cad4d986`で、追跡ファイルはLICENSEのみ。

既存checkout・未追跡ファイル・ブランチを変更していない。task-3内に独立Core checkout、
検証venv、uv cache、合成設定、証跡を作成した。Coreの既存人格・会話履歴・長期記憶は読み取らず、
合成Character Cardと`synthetic-test` aliasだけを登録した。履歴storeは接続していない。
一時Coreは`127.0.0.1:18080`のみで起動し、access logを無効にし、検証後に所有プロセスだけを停止した。

## Ubuntuの自動検証

CPython 3.12.3、公式PyPIのuv 0.8.22。両repoの`uv sync --frozen`を分離venvへ実行し、
依存version・lockを変更していない。

| コマンド | 結果 |
| --- | --- |
| python -m pytest -q -p no:cacheprovider --basetemp 専用Linux一時領域 | PASS、90 passed、skipなし、4.00秒 |
| python -m ruff check --no-cache src tests | PASS |
| python -m mypy --cache-dir 専用検証領域 | PASS、14 source files |
| uv build --out-dir 専用検証領域 | PASS、sdistとwheel |
| python -m local_gpt_live.demo | PASS、合成7シナリオ |

初回pytestはDrvFS上の一時ファイルでFileNotFoundErrorとなり0件だったためFAILとして保持した。
Linuxの`/tmp`に専用一時領域を作り、再実行した90件の成功を採用した。
初回buildは`--no-build-isolation`で検証venvにhatchlingがなくFAIL。
pyprojectの固定build-systemを使う通常の`uv build`で成功した。
Windows worktreeのGit情報は、Linux側からGit管理ディレクトリを明示して読み取った。
worktreeの`.git`やユーザーGit設定は変更していない。

## 合成入力による実接続

実行開始: `2026-10-04T05:24:24.824076+00:00`。ネットワークmockは使用していない。

| 対象 | 条件と観測 | 結果・範囲 |
| --- | --- | --- |
| CoreChat → Core → llama.cpp | 一時Core :18080、既存llama.cpp :18081、gemma4-12b。合成確認文を送信 | PASS、実SSE 3 chunk、5文字の合成確認応答、6.443秒。API接続の確認であり人格・品質評価ではない |
| Whisper adapter → 既存dev Whisper | :50025、16kHz mono PCM16のプログラム生成1秒無音 | PASS、0.445秒、空transcript。通信と無音時の結果だけを確認し、発話認識精度は未検証 |
| LiveKit :7880 | 既存dev serverのHTTP `/` | HTTP 200、`OK`。readinessのみ。RTC join・media送受信はNOT RUN |

Whisper `/version`: serviceVersion `1.0`、model `medium`、modelRevision
`08e178d48790749d25932bbc082711ddcfdfbc4f`、CUDA deviceIndex 0、computeType `int8_float16`、
modelInstances 1、globalInflight 1。llama.cppのmodel metadataはcontext 4096、Q4_K Medium。

Core cold起動の初回20秒readiness待ちはFAIL。待ち時間を90秒に拡張した再実行で接続に成功した。
初回・再実行とも所有一時Coreの停止を確認した。共有llama.cpp・Whisperは停止・再設定していない。

## 未実施とブロッカー

VOICEVOX :50021とIrodori :50024は接続拒否。通常ユーザーで既存VOICEVOX unitを起動したが、
対話認証が必要として拒否された。sudo、別ユーザー、Docker直接起動による迂回は行っていない。
TTS実合成はNOT RUN。IrodoriのGPU起動も未実施。起動前の共有GPUは使用12002 MiB、空き4059 MiB、
利用率22%であり、並行配備もあるため共有サービスの変更は行っていない。
TTS検証には所有者が通常の管理経路でサービスを準備し、GPU利用条件を調整する必要がある。

LiveKit adapter、ブラウザの実出力に対応するACK、再生済みprefixの履歴連携は未実装。
今回のACKテストはBackendの契約と合成端末だけ。実ブラウザ再生、実マイク・カメラ、
人の聴感・遅延品質、音声入力から再生までの通し受入はNOT RUN。

生の実行記録は作業領域の`ubuntu-validation.json`、`ubuntu-connections.json`、
`ubuntu-*.txt`、初回FAIL記録に保存した。公開証跡に私的会話・認証情報・声資産を含めていない。
