---
id: VC-WIKI-INDUSTRY-AI-MEMORY
title: 全球 AI 算力与存储产业链
version: 1.1.0
updated_at: 2026-07-16
published_at: 2026-07-16T00:00:00+08:00
status: active
owners:
  - ai_storage_industry_agent
  - risk_critic_agent
tags:
  - ai
  - memory
  - hbm
  - semiconductor
  - storage
source_urls:
  - https://investor.nvidia.com/
  - https://investors.micron.com/quarterly-results
  - https://news.skhynix.com/press-center/press-release/
  - https://www.samsung.com/global/ir/financial-information/earnings-release/
  - https://investor.tsmc.com/english
  - https://computeexpresslink.org/about-cxl/
  - https://www.sec.gov/edgar/search/
---

<!-- section:scope -->
## 研究范围

本 Agent 跟踪全球 AI 基础设施需求如何影响存储、半导体制造和 A 股相关公司，再
映射至五个宽基指数。重点是 D1/D2 可定价的新增事件，不是给整个 AI 产业做长期
估值。

“AI存储”至少包含两条不同链路，不得混写：

- 高带宽内存与服务器内存：HBM、服务器 DRAM、内存接口、先进封装。
- 数据存储：企业级 SSD、NAND、存储控制器与数据中心存储系统。

某一链路景气并不自动证明另一链路同步改善。

<!-- section:value-chain -->
## 产业链地图

研究顺序如下：

1. 需求端：云服务商、模型训练与推理、企业 AI 基础设施预算。
2. 计算平台：GPU、专用加速器、CPU 与服务器平台。
3. 内存与存储：HBM、DRAM、NAND、企业级 SSD 和控制器。
4. 制造：晶圆代工、先进封装、测试、设备与材料。
5. 互连与系统：高速网络、CXL 内存扩展及整机集成。
6. A 股映射：目标指数当日成份中的设备、材料、封装测试、模组和系统公司。

每次只能对有明确证据支持的环节建立传导，不允许从“AI需求增加”跳过中间约束，
直接推导某家 A 股公司盈利上升。

<!-- section:observable-signals -->
## 可观察信号

| 信号 | 优先证据 | 需要的反证 |
| --- | --- | --- |
| 需求变化 | 客户公司财报、正式指引、监管披露 | 订单是否只是提前下单或重复预订 |
| HBM进展 | 存储厂商财报、量产或送样公告 | 认证、良率、产能和交付时间是否明确 |
| DRAM/NAND周期 | 厂商收入结构、库存、资本开支与指引 | 价格变化是否仅来自短期供给扰动 |
| 先进封装 | 代工厂或供应商正式披露 | 产能扩张是否匹配实际客户需求 |
| 产品路线 | 公司官方发布、标准组织文件 | 路线图是否等同商业化收入 |
| A股联动 | 交易所公告、公司互动与指数成份快照 | 业务占比、客户关系是否有原文证明 |

产品“发布、送样、认证、量产、放量”是不同阶段，必须使用原文中的准确动词。

<!-- section:cross-market-timing -->
## 跨市场时间规则

- 统一记录原事件时区和 Asia/Shanghai 转换时间。
- 在 A 股 data_cutoff 之后发布的信息，不得写入当日正式预测。
- 海外盘中价格只能证明市场反应，不能单独证明基本面原因。
- 同一财报被新闻、社交媒体和研究报告重复解读，只算一个事实源。
- 周末或节假日事件要映射至下一个 A 股交易日，不能用自然日替代交易日。

<!-- section:index-mapping -->
## 对目标指数的映射方法

先取得预测日有效的成份与权重，再按“直接业务、供应链业务、风险偏好代理”分层：

- 直接业务：公司已公开披露与相关产品或客户的业务联系。
- 供应链业务：存在可验证的订单、产品或产能关系，但传导仍有中间环节。
- 风险偏好代理：仅因科技风格或主题交易而相关，不得写成盈利证据。

科创50、创业板指可能更直接体现科技成长风格，中证500和中证1000可能包含更多
中小型产业链公司，沪深300可能通过大盘科技与整体风险偏好受到影响。该描述只是
待验证假设；最终权重与方向必须引用当日成份快照及各指数 Wiki。

<!-- section:event-score -->
## 事件判断模板

每个事件必须回答：

- 新事实是什么，原始来源在哪里？
- 相对市场已知信息发生了什么变化？
- 影响哪一环，瓶颈是需求、供给、良率、价格还是交付？
- 对哪一个指数有可验证暴露，权重是否足以影响 D1/D2？
- 海外相关资产是否已提前交易该信息？
- 最强反证与失效条件是什么？

若无法完成“事件—产业环节—A股公司—指数权重”链条，不得把该事件计入方向证据；若因此
缺少正式预测的关键输入，应阻断 run，不能用中性、abstain 或固定方向填补缺口。

<!-- section:failure-modes -->
## 常见错误

- 把 GPU 需求、HBM、普通 DRAM 和 NAND 当作同一个周期。
- 把公司宣传稿中的技术能力当作已实现订单。
- 用未经证实的客户名单或供应链传言建立映射。
- 忽略出口管制、认证、良率、产能和价格之间的约束。
- 看到海外半导体上涨就倒推出单一新闻原因。
- 用当前成份或后来披露的信息回填历史预测。

<!-- section:sources -->
## 来源

- [NVIDIA Investor Relations](https://investor.nvidia.com/)
- [Micron Quarterly Results](https://investors.micron.com/quarterly-results)
- [SK hynix 官方新闻与财务结果](https://news.skhynix.com/press-center/press-release/)
- [Samsung Electronics Earnings Releases](https://www.samsung.com/global/ir/financial-information/earnings-release/)
- [TSMC Investor Relations](https://investor.tsmc.com/english)
- [Compute Express Link Consortium：CXL 简介](https://computeexpresslink.org/about-cxl/)
- [SEC EDGAR](https://www.sec.gov/edgar/search/)

公司 IR 与标准组织页面用于确认公司披露和技术定义；具体数字必须引用预测当次
冻结的报告或公告，而不是仅引用本条目。
