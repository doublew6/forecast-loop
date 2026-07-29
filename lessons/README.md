# Lesson proposals

这里保存反省形成的 Markdown 经验候选及其状态。经验库不是正式 Wiki，
任何候选都不会自动改变预测权重或历史结论。

普通方向规律只有在至少 5 个独立市场事件、20 个不同目标日期的重放，
且平均 Brier / 校准改善并且重要子组不退化后，才具备提交 Wiki 审核的
资格。单次极端事件只能立即提出数据门禁、风险检查表或失效条件。

同一目标日的五个指数共享一个 `episode_key`，只计一个独立市场事件。
候选默认 60 个交易日半衰期，并在每月或每 20 个独立日期复核。状态只可
沿人工审核流程进入 `candidate`、`active`、`challenged`、`retired` 或
`superseded`；正式 Wiki 晋升另行执行，并更新 Wiki 版本、索引和日志。

这里的文件由 completed Live 反省在 `reflection-finalize` 成功后，再通过
独立的 `reflection-render` 命令生成。finalize 本身不写 Markdown；Codex
也不能直接编辑候选文件。
