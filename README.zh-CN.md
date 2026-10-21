# Hermes QQ Bot Suite

[English](README.md)

**实验性源码补丁 + 本地插件**，不是独立应用、通用即装即用扩展，也不是 Nous Research 官方发行。唯一目标基线为 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) 的 **`ab44d8acf7bca230aea8574f2f2ab4666ca293ce`**；其他版本须重新审查与移植。

## 功能范围

- 群聊观察、静默文本/图片/文件的类型化记录、受限背景回放；按群选择启用辅助模型判断是否接话，支持图片处理。
- 原生队列/历史接线，出站发送回执与自身消息受控撤回。内部方法存在不代表当前模型有可调用工具，仍须核验实际入口与授权。
- 基于真实请求主体的工具/指令门禁，公共与私人上下文分离。昵称、提示词标签、共享会话、默认投递对象均不授予管理权限。
- 按 profile、聊天与真实发起者分区的持久文件，可信附件导入、文本/CSV 操作、显式导出与实际投递状态、生命周期清理、断网 Docker Python 和受限匿名公网浏览器。

**核心 overlay 与插件必须配套审查安装**，单独复制插件不等于部署完成。公共群里的输出仍对群公开；工作区不是秘密保险箱，也不是操作系统沙箱。

## 安全起步

1. 先读[部署](docs/DEPLOYMENT.md)、[安全与数据流](docs/SECURITY.md)、[验证状态](docs/VERIFICATION.md)。
2. 在干净、可丢弃、固定提交的上游 checkout 中审查 manifest、overlay 与补丁，不直接覆盖运行目录。
3. `scripts/prepare_host.py --host PATH` 默认只检查；写源码必须显式 `--apply`。复制插件还须 `--install-plugin --plugin-dir PATH`，且目标目录不得存在。详见部署文档；脚本通过验收前不承诺安装器已可用。
4. [示例配置](examples/config.safe.yaml)默认关闭平台与插件、保持请求授权门禁开启且无公共工具授权，无凭据、管理员/群 ID。凭据私下配置；按规范完整主体与授权范围知情填写名单。
5. 镜像须自行构建/准备并核验实际完整 image ID；[构建文档](docs/BUILD.md)不复用私人机器上的镜像标识，也不附二进制。

不自动改运行配置、不安装服务、不自动重启网关。验证后由操作者决定何时手动载入。

## 限额与验证边界

每工作区 256 MiB、普通单文件 32 MiB、2048 文件，全体工作区 2 GiB。导出副本保留 7 天，显式临时导入保留 72 小时；传输证明有效 10 分钟、每次最多关联 4 文件。[完整限制](docs/CAPABILITIES.md)。

公开脱敏候选已在 Windows 通过 **45 个测试文件：909 passed、0 failed、1 项 POSIX-only skip**，另有8项发布工具测试通过。[VERIFICATION](docs/VERIFICATION.md)记录具体范围与剩余事项。这不等于 Linux/macOS CI、新公开 Docker 配方已重新构建、真实 QQ API 交付证明或安全认证。

上游 Hermes Agent 属于 Nous Research 与贡献者，采用 MIT 许可证；分发衍生源码须保留通知及仓库 LICENSE。Playwright、Chromium、Python 与镜像层各有自己的许可；本仓库不附第三方 wheel/浏览器/镜像、不附私人构建回执、会话、凭据或部署数据。[Hermes 官方文档](https://hermes-agent.nousresearch.com/docs/)用于核对宿主功能，本项目兼容性仍以固定源码为准。
