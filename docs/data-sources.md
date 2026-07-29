# 数据源与时间规则

## 原则

forecast-loop 只把“在预测截止时点真实可见、能回到原始出处、能够保存哈希”的资料
放入正式 run。免费公开来源可能延迟、变更或失败，因此每类数据都通过可替换 provider
接入，且失败不能静默降级成旧数据。

Demo provider 用于本地演示和端到端测试。Demo 数据必须显著标记，且永远不进入正式
成绩单。

真实 LLM 不是数据源。若模型上下文只有 Wiki，或行情仍采用固定演示波动率，该 run
仍属于 demo，不得仅因配置了 API key 就标记为可验证 live。live 必须先生成行情与
资讯的不可变 Evidence Snapshot，再允许研究节点运行。

## Provider 契约

### 行情 provider

每条指数观察至少返回：

- index_code 和 trade_date；
- close，必须为正数；
- source 与可回到原始记录的 source_url；
- observed_at 与 ingested_at；
- 原始响应的 64 位小写 SHA-256 source_hash。

正式预测需要五个指数当日收盘，以及各自至少 21 个有效收盘价。若来源只提供自然日
数据，适配器必须先与交易日历对齐。

支持本地 CSV 回补，但文件须有 index_code、trade_date、close、source 字段，并把
文件哈希保存为来源。CSV 回补生成新 run，不修改旧 run。

### 交易日历 provider

交易日以沪深交易所公告为准。provider 返回日期、是否开市和可选的异常说明。临时
休市与节假日调整必须覆盖一般工作日规则。

每个 live 快照必须冻结恰好两个、严格递增且晚于 base_session 的目标交易日：第一个
用于 D1，第二个用于 D2。工作日推算只允许用于 Demo；正式快照应由交易日历 provider
给出，避免把调休或临时休市当成目标日。快照还须保存该 provider 的原始 URL、
source_hash、observed_at、ingested_at，以及 `[base_session, D1, D2]` 三个 sessions；
服务端要求它们与快照日期字段完全一致。

### 资讯 provider

每条事件至少包含：

- event_id、title、publisher、source_url；
- event_time、published_at、ingested_at；
- entities 与 event_type；
- source_tier、language；
- raw_content 或不可变快照引用；
- content_hash。

缺少明确发布时间的网页不得进入严格历史回放；可以作为线索归档，但须降低可信等级。

## Live Evidence Snapshot 契约

一期没有内置自动采集器。live run 只接受预先组装并封存的 JSON 快照；采集、授权与
来源适配仍由外部 provider 负责。快照至少冻结：

- as_of、data_cutoff、created_at、base_session 和恰好两个 target_sessions；
- 交易日历的三个 sessions、source_url、source_hash、observed_at 与 ingested_at；
- 五个指数各自的 volatility_20d，以及等于 base_session 的 trade_date、独立的
  source_url、source_hash、observed_at、ingested_at；
- 至少一条动态 EvidenceItem，包含稳定 ID、短摘录、原始 URL、三个时间字段和
  content_hash；
- 快照整体的 canonical JSON content_hash。

系统按排序键、UTF-8 和固定 JSON 表示重算整体及 EvidenceItem 哈希。任何字段变化都
产生不同哈希；只填写一个看似合法的摘要值不能通过门禁。动态来源和行情来源都必须
属于代码审查过的域名 allowlist，不能由模型在运行中扩展。

新鲜度门禁要求 snapshot.as_of 与请求完全一致，所有资料的 ingested_at 不晚于
data_cutoff，并满足 `ingested_at <= data_cutoff <= as_of <= created_at <= prepare_time`。
`as_of` 是收盘数据通过上游 manifest 与质量回执后实际构建快照的时间，不是硬编码的
15:00；data_cutoff 与 as_of 必须属于同一
本地日期。过期快照、未来资料、
无时区时间戳或哈希不匹配都会阻断 live run。

正式适配器必须在仓库外完成 provider 专属的完整性、交易日历、发布时间与质量门禁，
然后只输出本项目的公共快照契约。私有表名、指标名、目录布局和 provider 凭据不得进入
公共 JSON schema、日志或测试 fixture。公共核心仍会独立验证五指数完整性、时间边界、
来源 URL、内容哈希和整体封印，不能只信适配器自报。

## 一期来源清单

### 指数与交易所

| 数据 | 优先来源 |
| --- | --- |
| 沪深300、中证500、中证1000、科创50方案与调样 | [中证指数](https://www.csindex.com.cn/) |
| 创业板指方案与调样 | [国证指数](https://www.cnindex.com.cn/) 与 [深交所](https://www.szse.cn/) |
| 沪市公告与交易日信息 | [上交所](https://www.sse.com.cn/) |
| 深市公告与交易日信息 | [深交所](https://www.szse.cn/) |

公开页面不是稳定低延迟行情 API。正式行情适配器上线前，应使用经验证的 provider
或带来源的 CSV，不应通过解析展示页假装获得生产级行情。

### 中国宏观与监管

| 类型 | 原始入口 |
| --- | --- |
| 货币政策与操作 | [中国人民银行](https://www.pbc.gov.cn/) |
| 官方宏观数据 | [国家统计局](https://data.stats.gov.cn/) |
| 国务院政策 | [国务院政策文件库](https://sousuo.www.gov.cn/zcwjk/policyRetrieval) |
| 资本市场监管 | [中国证监会](https://www.csrc.gov.cn/) |
| 汇率与跨境管理 | [国家外汇管理局](https://www.safe.gov.cn/) |

### 全球 AI 算力与存储

| 环节 | 原始入口 |
| --- | --- |
| 加速计算与需求指引 | [NVIDIA IR](https://investor.nvidia.com/) |
| 美国存储厂商财报 | [Micron Quarterly Results](https://investors.micron.com/quarterly-results) |
| 韩国存储厂商披露 | [SK hynix Newsroom](https://news.skhynix.com/press-center/press-release/) |
| 综合存储与半导体披露 | [Samsung Electronics IR](https://www.samsung.com/global/ir/financial-information/earnings-release/) |
| 晶圆代工与先进制造 | [TSMC IR](https://investor.tsmc.com/english) |
| 美国监管文件 | [SEC EDGAR](https://www.sec.gov/edgar/search/) |
| 内存互连标准 | [CXL Consortium](https://computeexpresslink.org/about-cxl/) |

具体判断应引用具体财报、公告或规范页面；只引用入口首页不能证明一个动态事实。

## 三时间字段

三个时间不能互相替代：

- event_time：事件实际发生或数据所属时间。
- published_at：来源首次公开该信息的时间。
- ingested_at：forecast-loop 取得并冻结该信息的时间。

纳入正式 run 必须同时满足：

1. published_at 不晚于 data_cutoff；
2. ingested_at 不晚于 data_cutoff；
3. 原始 URL 在信任域名 allowlist 内，规范化内容哈希可以重算并匹配；
4. 来源时区可转换为 Asia/Shanghai。

历史网页声称更早发布，但系统当时没有取得的资料，不得加入严格历史回放。

## 截止与运行节奏

一期默认在 A 股收盘且行情确认后运行。具体 data_cutoff 由运行记录给出，不把“约
15:00”写死为所有来源的可用时间。采集器应等待行情 provider 确认当日收盘数据，而
不是只按机器时钟触发。

- 盘前和盘中资料可以进入当日收盘后预测。
- data_cutoff 之后的盘后公告进入下一次 run。
- 海外信息按原时区记录，再转换到 Asia/Shanghai。
- 周末事件映射到下一个 A 股交易日。

## 去重与事实拆分

事件先按规范化 URL、内容哈希、引用关系、主体和发布时间去重。同一公告的转载不算
独立证据。

标准化结果必须分开：

- 原始事实；
- 来源自己的展望或观点；
- 可审计的市场预期；
- Agent 推断。

摘要必须保留限定词、统计口径和时间范围。无法取得原文时，不得把二手标题改写成
确定事实。

## 数据质量门禁

以下情况阻断正式发布：

- 任一目标指数缺少当日有效收盘或 20 个日收益；
- 最新行情日期不是预测基准交易日；
- close 非正、重复日期冲突或来源口径混用；
- 不是恰好两个已冻结的目标交易日，或目标日不严格晚于基准日；
- 任一指数缺少与 base_session 绑定的 trade_date、独立行情 URL、时间或 64 位
  source_hash；
- 交易日历 sessions 与 base/D1/D2 不一致，或缺少日历来源、时间和哈希；
- 交易日历与行情日期冲突；
- 关键证据没有可信 URL、时间或可重算的 canonical content_hash；
- 抓取返回登录页、错误页或旧缓存却被误识别为正文；
- 引用发布时间或系统抓取时间晚于 data_cutoff。

资讯不足但行情和时间完整时，可以提高实际结果的小波动概率并降低涨跌条件置信度；
若已不足以形成可审计判断则阻断正式 run。不能用 neutral direction、abstain 或固定涨跌
掩盖缺口；数据身份损坏时必须失败。

## 快照与回放

原始资料、标准化事件、Wiki 版本、实际模型名、Agent 版本/权重、prompt、schema 与
聚合版本共同生成 input_hash。历史回放只读取该次快照，不重新访问网页后覆盖内容。
网页更新或链接失效时，新增快照和审计记录。

正式成绩单查询必须排除 demo，以及缺少行情快照 ID、事件快照 ID 或完整 input_hash
清单的运行。该门禁由确定性代码执行，不能交给 CIO 或模型自行声明“资料完整”。

到期评分同样需要来源快照。调用方提交基准日与目标日两条正收盘价、各自交易日、
原始 URL 和 source_hash；系统只在目标日收盘后自行计算累计收益。接口不接受调用方
预先计算的 actual_return，避免把不可追溯结果写入成绩单。

推荐目录语义：

- data/raw：原始响应与下载文件，按内容哈希命名；
- data/snapshots：每个 run 的输入清单；
- data/checkpoints：LangGraph 恢复状态；
- SQLite：规范化元数据、决策与评分。

这些运行产物不提交 Git；仓库只提交 provider 代码、Wiki 和可复现的测试夹具。

## 合规与运维

- 遵守网站条款、robots 规则、速率限制和内容许可。
- 不把付费研报或受版权保护全文复制进仓库。
- 保存必要的短引用和原始 URL，正文采用自己的结构化摘要。
- provider 应设置超时、有限重试和清晰错误，不使用无限循环。
- allowlist 的来源变更须代码审查；模型不能自行扩展信任边界。
