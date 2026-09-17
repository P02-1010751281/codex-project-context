# Context

## Codex 项目上下文插件：hook 归档与历史 session 补救

0.2.0 已提供原生 Codex rollout 的历史导入与显式 unassigned 保存：按 cwd 路由、原样分块、可幂等重跑。插件已安装启用，真实导入和完整性核对通过；剩余工作是新 task 的 Desktop 冒烟测试。

## Key points

- 插件根目录就是当前仓库，不需要 Projects/plugins/ 外层。
- .agents/memory 保持 Pi 兼容的项目上下文、记忆、handoff 和 session-logs 布局。
- session.jsonl 是当前 hook 归档事实源；native-codex-session 是历史原生 rollout 的不透明字节分块。
- import-codex 已把 89 个可归属 rollout 与 27 个显式 unassigned rollout 保存到项目归档。
- 原始 Codex 文件仍保留在 /home/user/.codex 下未改动；项目归档是可检索副本。
- 版本 0.2.0 已通过 18 个测试、静态校验、实际导入和无重复 ID 核对。

## Open tasks

- 在普通项目中创建新 Codex Desktop task，验证新 hook 的 SessionStart、工具结果归档和 Stop 维护。
- 确认真实 Desktop 版本按预期加载 0.2.0 hooks；当前已运行 task 不热加载新 hook。
- 以后若发现新的历史源文件，再重跑 import-codex；默认仍不把无法映射的 cwd 猜测归属给具体项目。
