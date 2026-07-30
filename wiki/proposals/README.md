# Wiki proposal layout example

This public directory documents the proposal layout only. Store real proposals
under `data/wiki/proposals/` or another configured local Wiki root. Runtime
predictions never read files below `proposals/`.

每个提案应说明：

- 目标 Wiki 稳定 ID 与当前版本；
- 建议的新版本和变更等级；
- 触发来源、原始 URL、发布时间、抓取时间和内容哈希；
- 新结论、被替代结论、矛盾与未解决问题；
- 对历史预测无回写、对未来预测何时生效；
- lint、链接、时间与引用校验结果。

After review, update the local Wiki entry and its local `index.md`, then append
the promotion to the local `log.md`. Do not copy operator proposals into this
public example directory.
