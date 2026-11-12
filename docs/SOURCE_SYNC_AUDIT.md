# 全量源码差异审计 / Full source-delta audit

本次不是只复制近期修复文件：逐个核对上一发布版全部 141 项 source hash、当前 overlay、完整 QQ 包及相关测试目录，再检查现用工作树的 tracked/untracked 本地补丁与外部 guest-workspace 入口。40 项原始来源摘要已变化；其中许多来自较新上游的接口/结构调整，并非遗漏的 QQ 功能。固定发布基线仍为 `ab44d8acf7bca230aea8574f2f2ab4666ca293ce`，没有合并新上游。

This audit compared all 141 previous source records, the public overlay, the complete QQ package/test discovery, tracked and untracked local changes, and the external workspace entrypoint. Forty preceding source hashes changed. A changed hash is not automatically an unsynchronized feature: newer upstream interfaces must not be copied onto the older pinned host.

逐文件摘要、分类及排除清单见 [SOURCE_SYNC_AUDIT.json](SOURCE_SYNC_AUDIT.json)。共移植 21 个源码/测试文件，含 6 个新增文件；当前 overlay 为 147 项。机器可读清单保留此前及此次来源摘要，不用当前源码摘要伪装整个公开文件与私人运行树相同。

## 已同步 / Ported

- 普通 QQ 消息统一独立 FIFO：不按管理身份拒绝/插队，不 steer/interrupt，不合并同人或不同人的图文。显式控制命令权限继续独立校验。
- 原生 burst 分类补齐 QQ 不合并规则。固定旧宿主上新增 QQ-only 已入队回合拦截保护，防止执行时误答上一轮澄清；真实入站回归先暴露失败，再修复通过。
- 连接中的适配器固定同代 sibling 类型、引擎与回执 ContextVar，覆盖插件真实 loader 重载后的 stamp/restore/bridge/回执反馈。
- 表情概率按有无正文分流：附图 PRD；纯 GIF 跳过 PRD、装饰冷却及最近图片排除。硬关闭、权限、每日额度、同事件去重及并发保护不变。
- 每轮候选用途与最多 50 次已确认发送计数；同事件准备快照稳定，发送事实仍实时刷新。后台不替换主模型选图。
- 访客公网 broker 使用操作系统 TLS 验证器，仍验证证书/域名、固定已验证公网 IP，未关闭 TLS 验证。

Independent QQ FIFO, generation-local expression/receipt dependencies, content-based GIF limits, per-turn semantic/diversity hints and OS-backed broker TLS are included with their regression closure. Public neutral labels, proxy opt-out, reviewed-catalog restrictions and independent public test-fixture repairs are preserved.

## 有意不发布 / Deliberate exclusions

- 19 个本地补丁/新增路径逐项列在 JSON 中，包括 provider/fallback 定制、私有单任务海报授权、通用媒体历史扫描优化、私人 fork 文档。不得把它们当作本次全部已发布。
- 较新上游的 HTTP client/setup/allowlist 接口及共享核心重构保持旧固定宿主版本；新上游中拆到 `run_background.py` 的权限包装在旧 overlay 的 `run_turn.py` 已保留。
- 每平台 reasoning 定制及对应混合测试此前就不在发布范围，本次仍未同步，明确列为排除项；不是已发布能力。
- 外部 guest-workspace 入口与来源一致；manifest 仅描述文案不同，保留公开说明。没有复制私人图库、配置、数据库、会话、镜像 ID 或原始日志。

The machine-readable inventory explicitly lists 19 excluded local paths. Provider policy, deployment-bound poster grants, generic host media optimization and per-platform reasoning customization are **not** advertised as published features. Public plugin/build assets remain independently maintained; a private deployment is not a public build receipt.

## 依赖与验收边界 / Dependency and acceptance boundary

本次公网 broker 的 OS TLS 路径需要 `truststore==0.10.4`；固定宿主仅在 Python 3.14 自动声明此依赖。本次完整验证使用 PM Python 3.14.7。Python 3.11–3.13 未验收，不能因上游 `requires-python` 较宽就宣称整套扩展可运行；必须另行准备和验证包含 truststore 的隔离依赖环境。发布脚本自身 Python 3.12+ 的下限不等于完整宿主依赖下限。

The OS TLS broker requires `truststore==0.10.4`, automatically declared by the pinned host only on Python 3.14. This candidate is verified with PM Python 3.14.7. Older interpreters require a separately prepared and verified dependency environment; release-helper compatibility is not full-suite runtime compatibility.

实际测试结果见 [VERIFICATION.md](VERIFICATION.md)。未重建公开 Docker 镜像、未调用真实 QQ 或模型、未观察客户端动画；未重启或改写现用源码。源码测试不能替代安装、载入或平台送达。
