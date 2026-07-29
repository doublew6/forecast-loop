# Security Policy

## Supported Versions

forecast-loop 目前处于早期开发阶段。安全修复优先应用于默认分支和最新发布版本；旧版本
可能不会获得回溯修复。发布说明会在支持策略发生变化时更新本文件。

| Version | Supported |
| --- | --- |
| Default branch | Yes |
| Latest release | Yes |
| Older releases | No |

`v0.1.0` 是 early release，pre-1.0 接口仍可能演进。发布制品、兼容边界和升级步骤分别见
[releasing.md](docs/releasing.md)、[compatibility-policy.md](docs/compatibility-policy.md)
和 [upgrading-to-v0.1.md](docs/upgrading-to-v0.1.md)。

## Reporting a Vulnerability

请勿为疑似安全漏洞创建公开 Issue，也不要在公开 Pull Request、日志或测试 fixture 中
披露利用细节、密钥、个人数据或真实交易数据。

请优先使用仓库 Security 页面中的 **Report a vulnerability** 私密报告入口。如果该入口
尚未启用，请通过仓库所有者或维护者 GitHub 个人资料中列出的私密联系方式报告，并在主题
中注明 `forecast-loop security`。

报告应尽量包含：

- 受影响的版本、提交或组件；
- 最小复现步骤与预期/实际行为；
- 可能的影响、攻击前提和已知缓解方式；
- 不包含第三方敏感数据的最小 proof of concept；
- 如适用，建议的披露时间安排。

维护者会确认收到报告、评估严重性并协调修复与披露。由于项目当前没有承诺固定的安全响应
SLA，请在公开披露前等待维护者确认；如果迟迟没有响应，可以发送一次私密跟进。

## Security Boundaries

forecast-loop 是研究、证据和审计系统，不是交易执行系统。以下行为应当被视为安全缺陷：

- 获得下单、账户操作或生产交易数据库写入能力；
- 绕过证据截止时间、来源、schema、哈希或版本校验并发布正式预测；
- 允许模型直接修改 run、receipt、Wiki、checkpoint、输入包或上游数据库；
- 通过路径穿越、符号链接或不安全归档逃逸配置的数据根目录；
- 未经认证将管理、触发或 finalize 接口暴露到公网；
- 泄露模型密钥、真实 handoff、市场数据授权内容或本地运行数据库。

默认 Docker Compose 的回环绑定只是本机隔离，不是完整的公网安全方案。任何公开或多人
部署都必须单独配置认证、TLS、访问控制、密钥管理、备份和日志脱敏。

## Supply-chain and Release Security

- Python 与前端安装必须分别使用 `uv.lock` 和 `package-lock.json` 的 frozen/CI 模式；
- GitHub Actions 固定到完整 commit SHA，同行注释所核对的 release 版本；
- Pull Request、默认分支和每周计划任务运行 dependency review、Python/npm audit、
  Gitleaks 全 refs 扫描、通用隐私边界、Trivy filesystem/container scan 与 SPDX SBOM；
- `v0.1.0` 只接受 GitHub 验证通过的签名 annotated tag；
- source、wheel、sdist 和 frontend static archive 必须双构建逐字节一致，并发布
  `SHA256SUMS` 与 provenance attestation；
- release tag 和已发布 asset 不得移动或静默覆盖；发现问题时发布新的 patch 与撤回说明。

历史与制品审计只输出脱敏计数，不输出命中值、私有规则、个人路径或私有规则文件位置。
不要把未脱敏 finding、签名私钥、令牌或仓库外的 private-boundary 配置作为普通 CI
artifact 上传。

公开仓库必须启用 GitHub secret scanning 和 push protection；固定版本的 Gitleaks、PII
及制品扫描作为补充，而不是替代。私有研究边界由仓库外的维护者 hooks 和来源锁定的
required check 执行，公开 Actions 不保存禁词清单。每个 fork 或独立部署仍需单独配置
自己的 secret scanning、push protection 和私有边界。

## Safe Harbor

我们支持善意、最小化影响的安全研究。请只测试你拥有或获授权的环境，不访问、修改或删除
不属于你的数据，不中断其他用户服务，不进行社会工程，并通过上述私密渠道报告发现。
在遵守这些要求的前提下，维护者会将研究视为善意披露并努力协作解决问题。
