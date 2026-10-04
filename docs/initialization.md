# 空repoの初期化・公開手順

確認時点でFYuki/local-GPT-liveはPUBLIC、空、default branchなし。visibilityを変更していない。

ローカルmainの初期commitはREADME、AGENTS、運用docs、ignore/attributes、bootstrap CI、
CodeRabbit設定、PR templateだけ。実装はそこから分けたepic/worktreeにある。

初回main pushは「main直push禁止」の例外なので、明示承認を得てから一度だけ実行する。
承認まで以下は実行しない。force push・既存ref上書きは不要。

設定PRと音声実装PRは分け、次の順で準備する。公開・PR作成の承認が成立するまでremoteへ書き込まない。

1. 最小mainの初期化後、`main → epic/development-foundation → infra/ci-baseline`で設定draft PRを準備する。
2. 全PR・main/epic pushのCI、開発手順、レビュー設定を検証する。音声packageがない段階の音声jobはskipと明記する。
3. 設定epicからmainへの統合はCI成功と実CodeRabbitレビュー・指摘対応を確認してから行う。
4. 設定統合済みmainから`epic/voice-foundation`を分岐し、音声実装を更新した作業ブランチでdraft PRを準備する。
5. 音声PRは設定PRの変更を含めず、対象headのCIと未実施の実接続・実音声受入を別々に記録する。

作業PRのbaseは対応するepic、epic PRのbaseはmainとする。古い初期commitから分岐した音声ブランチには
READMEとCI workflowの競合があるため、設定統合後のbaseで解消してから公開対象を再確認する。

mainとepicのCI、作業PRのCIを確認し、親作業者が管理する1時間枠の調整後にCodeRabbit差分レビューを依頼する。
CodeRabbit設定の存在はGitHub App接続の証拠ではない。インストール範囲または実際のreview/checkで確認する。
未接続なら管理者による接続が必要。設定ファイルから自動接続を推測しない。
