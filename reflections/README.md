# Reflection cases

这里保存已完成 `mode=live` 预测的版本化反省案例。它与预测时使用的
`wiki/` 严格分离，不能被历史预测当作事前知识读取。

规则：

- Demo 不生成案例。
- 每份案例绑定 `ReflectionRun`、目标交易日、D1/D2、冻结行情快照、
  来源快照和不可变回执。
- 修订必须新增文件并声明 `supersedes`，不得覆盖旧案例。
- Codex 只能提交交接目录中的两个 `drafts.json`。`finalize` 负责数据库和
  私有 `receipt.json`；这里的 Markdown 由独立的确定性
  `make reflection-render ARGS="<reflection-id>"` 从 completed Live 记录生成，
  不是 finalize 的副作用。
- 同一已校验内容可以幂等重渲染；目标文件内容不同则拒绝覆盖。
- 案例可以说明原因尚未证实，不得用事后相关性编造因果。

运行期私有交接包位于 `data/reflections/`，不提交 Git，也不通过 Web
服务暴露写入口。
