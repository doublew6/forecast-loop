# 中证1000 V2 简短日报

日报只发布中证1000（`000852.SH`）D1 预测。沪深300只用于相对强弱背景，不作为第二个预测标的；中证500、创业板指和科创50不出现预测结论。

```text
【forecast-loop｜中证1000预测日报】
日期：YYYY-MM-DD｜目标：下一交易日 YYYY-MM-DD

结论：偏涨 / 小波动 / 偏跌
概率：涨 XX%｜小波动 XX%｜跌 XX%

核心依据：
1. 一句话说明最重要的冻结证据与传导逻辑。
2. 一句话说明沪深300比较背景或风险偏好。

主要风险：一句话说明反证或失效条件。

运行状态：V2 Shadow 第 N/20 个前瞻样本｜数据截至 HH:mm｜Forecast ID: <short-id>
说明：研究预测，不构成投资建议。
```

未形成可信预测时不补写方向，改发：

```text
【forecast-loop｜中证1000预测日报】
今日预测未发布：<确定性阻断原因>。
当前状态：等待数据 / 等待草稿 / 校验失败 / 休市。
系统不会用旧五指数结果回填。
```

## 飞书 owner-only 投递

日报由确定性命令从已完成的 Live `csi1000-absolute-d1` 数据库记录生成。它只读取
Strategy 与 Risk Critic 的已封签字段，不要求 Codex 再写一份摘要，也不会读取盘后
资料。未指定 `--run-id` 时选择最新一条已完成预测。

先在本地渲染，不访问飞书：

```bash
make research-v2-notify ARGS="--dry-run"
```

正式发送给飞书应用的 owner：

```bash
make research-v2-notify ARGS="--env-file /absolute/private/feishu-owner.env"
```

私有 env 文件使用以下字段：

```dotenv
FORECAST_LOOP_FEISHU_APP_ID="cli_xxx"
FORECAST_LOOP_FEISHU_APP_SECRET="..."
FORECAST_LOOP_FEISHU_OWNER_ID_TYPE="open_id"
FORECAST_LOOP_FEISHU_OWNER_ID="ou_xxx"
```

发送器不会读取任何 group chat 配置，forecast-loop 日报始终只发给 owner。若私有环境
沿用另一组变量名，可通过 `--env-prefix PREFIX` 显式选择；标题可通过 `--title` 配置。
成功后会在私有 `data/notification-delivery/feishu-owner/<target-date>/` 写入幂等标记；
同一目标交易日的 18:30 主任务、20:30 补偿任务或重复 handoff 最多发送一次。飞书
失败不回滚已完成的预测，也不写成功标记，补偿任务可以安全重试。旧版按 Forecast ID
保存的旧版标记仍会被识别，不会因升级重复发送。
