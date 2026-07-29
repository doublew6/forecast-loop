import type {
  AgentOpinion,
  AgentScorecard,
  Citation,
  Forecast,
  ForecastBatch,
  Meeting,
  PredictionDirection,
  RunSummary,
  WikiEntry,
} from './types'

const asOf = '2026-07-13T15:30:00+08:00'

const wikiCitation: Citation = {
  id: 'cite-wiki-ai-memory',
  title: 'AI 存储产业链领先指标',
  kind: 'wiki',
  wiki_entry_id: 'WIKI-IND-AIMEM-001',
  section: '§3 HBM 价格与需求映射',
  version: '1.2.0',
  excerpt: 'HBM 合约价和供给利用率变化，通常先于相关指数盈利预期调整。',
}

const sourceCitation: Citation = {
  id: 'cite-source-samsung',
  title: '2Q26 Earnings Guidance',
  kind: 'source',
  publisher: 'Samsung Electronics IR',
  published_at: '2026-07-09T08:00:00+09:00',
  source_url: 'https://www.samsung.com/global/ir/',
  excerpt: '存储业务需求由 AI 服务器相关产品带动，先进产品组合继续改善。',
}

const forecasts: Forecast[] = [
  ['000300.SH', '沪深300', 'D1', 'up', 0.31, 0.45, 0.24, 0.0042, '涨跌二选一时正向证据略占优；小波动概率仍较高。'],
  ['000300.SH', '沪深300', 'D2', 'up', 0.48, 0.34, 0.18, 0.0059, '政策预期与权重科技形成轻微正向共振，仍保留较高小波动概率。'],
  ['000905.SH', '中证500', 'D1', 'up', 0.35, 0.42, 0.23, 0.0058, '中盘成长情绪改善，涨跌二选一时偏向上涨，但短周期噪声较高。'],
  ['000905.SH', '中证500', 'D2', 'up', 0.51, 0.30, 0.19, 0.0082, '产业催化向中盘制造扩散，正面证据略占优。'],
  ['000852.SH', '中证1000', 'D1', 'up', 0.33, 0.43, 0.24, 0.0072, '风险偏好回暖略占优，二元方向选择上涨，同时保留较高小波动概率。'],
  ['000852.SH', '中证1000', 'D2', 'up', 0.47, 0.30, 0.23, 0.0102, '主题扩散有利于小盘，但成交持续性仍需验证。'],
  ['399006.SZ', '创业板指', 'D1', 'up', 0.52, 0.29, 0.19, 0.0066, '全球 AI 硬件景气信号与成长风格修复共同支持。'],
  ['399006.SZ', '创业板指', 'D2', 'up', 0.57, 0.26, 0.17, 0.0093, '存储与算力链联动增强，对创业板盈利预期构成边际支撑。'],
  ['000688.SH', '科创50', 'D1', 'up', 0.55, 0.27, 0.18, 0.0074, '半导体景气跟踪指标转强，但需警惕拥挤交易。'],
  ['000688.SH', '科创50', 'D2', 'up', 0.61, 0.23, 0.16, 0.0105, 'AI 存储景气是当前最强证据，产业 Agent 与资讯 Agent 方向一致。'],
].map((item, index) => {
  const [index_code, index_name, horizon, direction, up, neutral, down, threshold, rationale] = item as [
    string,
    string,
    'D1' | 'D2',
    PredictionDirection,
    number,
    number,
    number,
    number,
    string,
  ]
  return {
    id: `forecast-${index + 1}`,
    index_code,
    index_name,
    horizon,
    target_date: horizon === 'D1' ? '2026-07-14' : '2026-07-15',
    direction,
    probabilities: { up, neutral, down },
    threshold,
    confidence: Math.max(up, down) / (up + down),
    rationale,
    citations: [wikiCitation, sourceCitation],
  }
})

export const demoForecastBatch: ForecastBatch = {
  run_id: 'RUN-20260713-001',
  as_of: asOf,
  data_cutoff: '2026-07-13T15:00:00+08:00',
  forecasts,
}

const agents = [
  {
    agent_id: 'macro_policy_agent',
    agent_name: '宏观政策研究员',
    role: '宏观、流动性与政策传导',
    direction: 'up' as const,
    probabilities: { up: 0.34, neutral: 0.46, down: 0.2 },
    summary: '国内政策预期托底略占优；涨跌二选一判断上涨，但海外利率与人民币波动限制置信度。',
    evidence: ['公开市场操作保持平稳', '风险资产相关性未显著恶化'],
    counter_evidence: ['海外实际利率仍处高位'],
    contribution: '提供宏观与流动性研究输入',
    weight: 1,
  },
  {
    agent_id: 'market_news_agent',
    agent_name: '市场资讯研究员',
    role: '事件、公告与跨市场映射',
    direction: 'up' as const,
    probabilities: { up: 0.53, neutral: 0.29, down: 0.18 },
    summary: 'AI 硬件相关增量信息偏正面，A股成长板块盘后公告未出现系统性负面。',
    evidence: ['海外存储链指引改善', '科技成交占比回升'],
    counter_evidence: ['部分热门标的短线涨幅偏大'],
    contribution: '提供当日事件与跨市场研究输入',
    weight: 1,
  },
  {
    agent_id: 'ai_storage_industry_agent',
    agent_name: 'AI 存储产业研究员',
    role: 'AI 算力、HBM 与存储产业链',
    direction: 'up' as const,
    probabilities: { up: 0.64, neutral: 0.22, down: 0.14 },
    summary: 'HBM 供需与先进封装利用率延续强势，对科创50和创业板指的映射最直接。',
    evidence: ['主要厂商高端存储产品组合改善', '全球半导体指数强于大盘'],
    counter_evidence: ['现货价到A股盈利兑现存在时滞'],
    contribution: '提供产业景气与指数映射输入',
    weight: 1,
  },
  {
    agent_id: 'strategy_agent',
    agent_name: '市场策略研究员',
    role: '市场状态、风格与五指数配置',
    direction: 'up' as const,
    probabilities: { up: 0.503333, neutral: 0.323333, down: 0.173334 },
    summary: '综合宏观、资讯与产业观点后，成长风格证据略占优，但共同来源限制置信度。',
    evidence: ['三位有效研究员输入齐全', '成长指数的产业暴露更直接'],
    counter_evidence: ['基础观点存在共同来源', '部分正面事件可能已经定价'],
    contribution: '综合基础研究，作为 CIO 的唯一方向输入',
    weight: 1,
    strategy_context: {
      market_regime: 'balanced' as const,
      style_bias: 'growth' as const,
      relative_rank: 3,
      rank_tied: false,
      allocation_score: 0.33,
    },
  },
  {
    agent_id: 'risk_critic_agent',
    agent_name: '风险反证官',
    role: '反证、拥挤与数据污染检查',
    direction: 'up' as const,
    probabilities: { up: 0.25, neutral: 0.55, down: 0.2 },
    summary: '反证后正向证据仍略占优，因此风险倾向为上涨；跨市场时滞和拥挤限制置信度。',
    evidence: ['有效研究意见已齐备'],
    counter_evidence: ['跨市场传导存在时滞', '热门方向交易拥挤'],
    contribution: '不做方向投票；用于反证并提高小波动结果概率',
    weight: 0,
  },
]

export const demoOpinions: AgentOpinion[] = agents.flatMap((agent, index) =>
  (['D1', 'D2'] as const).map((horizon) => ({
    id: `opinion-${index + 1}-${horizon}`,
    ...agent,
    status: agent.agent_id === 'quant_agent' ? 'placeholder' as const : 'active' as const,
    index_code: '000300.SH',
    horizon,
    invalidation_conditions: ['关键来源被更正', '下一交易日成交结构与预期方向明显背离'],
    citations: agent.agent_id === 'quant_agent' ? [] : [wikiCitation, sourceCitation],
  })),
)

export const demoMeeting: Meeting = {
  run: {
    id: demoForecastBatch.run_id,
    as_of: demoForecastBatch.as_of,
    data_cutoff: demoForecastBatch.data_cutoff,
    status: 'completed',
    duration_seconds: 86,
    data_quality: 'passed',
    data_quality_details: {
      citations_validated: 40,
      wiki_entries: 7,
      wiki_has_sources: 6,
      future_information_check: 'passed',
      warning: '演示数据仅用于验证界面，不可用于投资决策。',
    },
    mode: 'demo',
    forecasts_count: 10,
  },
  opinions: demoOpinions,
  forecasts,
  workflow_steps: [
    { id: 'freeze', label: '冻结数据与 Wiki 版本', status: 'completed', detail: '41 条当日资料，7 个 Wiki 条目' },
    { id: 'research', label: '研究 Agent 并行分析', status: 'completed', detail: '3 个有效研究意见；Quant 待接入' },
    { id: 'strategy', label: '市场策略研究员综合', status: 'completed', detail: '形成市场状态、风格与指数配置判断' },
    { id: 'critic', label: '风险反证检查', status: 'completed', detail: '提出 4 条反证，未发现未来信息' },
    { id: 'evidence', label: '引用与时间校验', status: 'completed', detail: '全部引用在数据截止时间前可见' },
    { id: 'cio', label: 'CIO 形成最终判断', status: 'completed', detail: '生成 5 个指数 × 2 个周期预测' },
  ],
}

export const demoScorecards: AgentScorecard[] = [
  ['cio_agent', 'CIO 投委会', '最终组合判断', 'D1', null, null, 0, null, null, null],
  ['ai_storage_industry_agent', 'AI 存储产业研究员', '产业链与指数映射', 'D1', null, null, 0, null, null, null],
  ['market_news_agent', '市场资讯研究员', '事件与跨市场映射', 'D1', null, null, 0, null, null, null],
  ['macro_policy_agent', '宏观政策研究员', '宏观与政策传导', 'D1', null, null, 0, null, null, null],
  ['strategy_agent', '市场策略研究员', '市场状态、风格与指数配置', 'D1', null, null, 0, null, null, null],
  ['risk_critic_agent', '风险反证研究员', '反证与数据污染检查', 'D1', null, null, 0, null, null, null],
  ['quant_agent', '量化研究员（待接入）', '未接入验证数据前不产生判断', 'D1', null, null, 0, null, null, null],
  ['cio_agent', 'CIO 投委会', '最终组合判断', 'D2', null, null, 0, null, null, null],
  ['ai_storage_industry_agent', 'AI 存储产业研究员', '产业链与指数映射', 'D2', null, null, 0, null, null, null],
  ['market_news_agent', '市场资讯研究员', '事件与跨市场映射', 'D2', null, null, 0, null, null, null],
  ['macro_policy_agent', '宏观政策研究员', '宏观与政策传导', 'D2', null, null, 0, null, null, null],
  ['strategy_agent', '市场策略研究员', '市场状态、风格与指数配置', 'D2', null, null, 0, null, null, null],
  ['risk_critic_agent', '风险反证研究员', '反证与数据污染检查', 'D2', null, null, 0, null, null, null],
  ['quant_agent', '量化研究员（待接入）', '未接入验证数据前不产生判断', 'D2', null, null, 0, null, null, null],
].map((row) => {
  const [agent_id, agent_name, role, horizon, accuracy, brier, sample_size, up_precision, neutral_precision, down_precision] = row as [
    string,
    string,
    string,
    'D1' | 'D2',
    number | null,
    number | null,
    number,
    number | null,
    number | null,
    number | null,
  ]
  return {
    agent_id,
    agent_name,
    role,
    workflow_role:
      agent_id === 'cio_agent'
        ? 'decision'
        : agent_id === 'strategy_agent'
          ? 'strategy'
          : agent_id === 'risk_critic_agent'
            ? 'critic'
            : 'research',
    source_type:
      agent_id === 'cio_agent'
        ? 'deterministic'
        : agent_id === 'quant_agent'
          ? 'quant'
          : 'ai',
    status: agent_id === 'quant_agent' ? 'placeholder' : 'active',
    horizon,
    accuracy,
    sign_sample_size: 0,
    sign_correct: 0,
    sign_accuracy: null,
    material_sample_size: 0,
    material_correct: 0,
    material_direction_accuracy: null,
    brier,
    sample_size,
    sample_sufficient: false,
    expected_calibration_error: null,
    agent_version:
      agent_id === 'cio_agent' || agent_id === 'risk_critic_agent'
        ? '0.3.0'
        : '0.2.0',
    model_name:
      agent_id === 'cio_agent'
        ? 'deterministic-committee-aggregation-v0.3.0'
        : agent_id === 'quant_agent'
          ? 'unavailable-no-quant-signal-v1'
          : 'deterministic-binary-demo-v3',
    note:
      agent_id === 'strategy_agent'
          ? '新角色尚无已到期样本。'
          : agent_id === 'risk_critic_agent'
            ? 'Risk Critic 负责反证，不计算方向成绩。'
            : agent_id === 'quant_agent'
              ? 'Quant 尚未接入验证数据，不产生方向判断，也不进入成绩单。'
              : 'API 未连接，静态 fallback 不提供虚构历史成绩。',
    up_precision,
    neutral_precision,
    down_precision,
    calibration: [],
  }
})

export const demoWiki: WikiEntry[] = [
  {
    id: 'WIKI-IND-AIMEM-001',
    slug: 'ai-memory-leading-indicators',
    title: 'AI 存储产业链领先指标',
    category: 'AI 存储',
    version: '1.2.0',
    updated_at: '2026-07-12T18:20:00+08:00',
    summary: '定义 HBM、DRAM、NAND 与先进封装指标，以及它们向 A 股宽基指数传导的证据边界。',
    sections: [
      { id: 'supply', heading: '§1 供给约束', content: '跟踪主要厂商资本开支、晶圆投片和先进封装产能，区分计划产能与实际可交付产能。' },
      { id: 'hbm', heading: '§3 HBM 价格与需求映射', content: 'HBM 合约价、产品组合和服务器出货共同确认景气，单一现货报价不构成充分证据。' },
      { id: 'mapping', heading: '§5 指数暴露映射', content: '科创50和创业板指为一级映射，中证500和中证1000为扩散映射，沪深300主要通过权重科技与风险偏好间接传导。' },
    ],
    sources: [
      { id: 'SRC-SAMSUNG-IR', title: 'Samsung Electronics Investor Relations', publisher: 'Samsung Electronics', url: 'https://www.samsung.com/global/ir/', published_at: '2026-07-09', content_hash: 'sha256:91f2…cf28' },
      { id: 'SRC-MICRON-IR', title: 'Quarterly Results', publisher: 'Micron Technology', url: 'https://investors.micron.com/', published_at: '2026-06-25', content_hash: 'sha256:772a…ab09' },
    ],
    cited_by_count: 31,
  },
  {
    id: 'WIKI-IDX-EXPOSURE-001',
    slug: 'broad-index-exposures',
    title: '五个宽基指数暴露地图',
    category: '指数框架',
    version: '1.0.3',
    updated_at: '2026-07-10T11:00:00+08:00',
    summary: '记录成分结构、行业暴露、风格敏感度及不同事件到指数的映射规则。',
    sections: [
      { id: 'method', heading: '§1 映射方法', content: '事件先映射到收入、成本或风险偏好，再映射至指数权重与拥挤度，不允许直接以叙事替代传导路径。' },
      { id: 'growth', heading: '§4 成长指数', content: '创业板指与科创50对盈利久期、半导体景气和流动性条件更敏感。' },
    ],
    sources: [
      { id: 'SRC-CSI-INDEX', title: '指数编制方案', publisher: '中证指数有限公司', url: 'https://www.csindex.com.cn/', published_at: '2026-06-30', content_hash: 'sha256:02ab…ea4f' },
    ],
    cited_by_count: 44,
  },
  {
    id: 'WIKI-MACRO-POLICY-001',
    slug: 'macro-policy-transmission',
    title: '宏观政策传导检查表',
    category: '宏观政策',
    version: '1.1.0',
    updated_at: '2026-07-11T09:10:00+08:00',
    summary: '将政策文本拆为工具、力度、执行时点、受益部门和市场已定价程度。',
    sections: [
      { id: 'checklist', heading: '§2 五步检查', content: '确认原文、识别新增信息、估计执行窗口、映射盈利或贴现率、寻找价格与成交确认。' },
    ],
    sources: [
      { id: 'SRC-PBOC', title: '公开市场业务公告', publisher: '中国人民银行', url: 'https://www.pbc.gov.cn/', published_at: '2026-07-13', content_hash: 'sha256:c22a…119b' },
    ],
    cited_by_count: 27,
  },
  {
    id: 'WIKI-METHOD-LABEL-001',
    slug: 'forecast-labels',
    title: 'D1 / D2 二元方向与结果标签',
    category: '评价方法',
    version: '2.0.0',
    updated_at: '2026-07-16T15:30:00+08:00',
    summary: '新预测必须涨跌二选一；实际收益继续按上涨、小波动、下跌评价。',
    sections: [
      { id: 'neutral', heading: '§2 评价噪声带', content: 'D1 使用 ±0.25×σ20；D2 使用 ±0.25×σ20×√2。小波动是实际结果桶，不是预测立场；direction 只允许上涨或下跌。' },
    ],
    sources: [],
    cited_by_count: 52,
  },
]

export const demoRuns: RunSummary[] = [
  { id: 'RUN-20260713-001', as_of: asOf, data_cutoff: '2026-07-13T15:00:00+08:00', status: 'completed', duration_seconds: 86, data_quality: 'passed', forecasts_count: 10 },
  { id: 'RUN-20260712-002', as_of: '2026-07-12T15:30:00+08:00', data_cutoff: '2026-07-12T15:00:00+08:00', status: 'failed', duration_seconds: 31, data_quality: 'failed', forecasts_count: 0, error: '关键行情源的数据日期未通过新鲜度检查' },
  { id: 'RUN-20260712-001', as_of: '2026-07-12T15:28:00+08:00', data_cutoff: '2026-07-12T15:00:00+08:00', status: 'completed', duration_seconds: 92, data_quality: 'warning', forecasts_count: 10 },
  { id: 'RUN-20260711-001', as_of: '2026-07-11T15:31:00+08:00', data_cutoff: '2026-07-11T15:00:00+08:00', status: 'completed', duration_seconds: 79, data_quality: 'passed', forecasts_count: 10 },
]
