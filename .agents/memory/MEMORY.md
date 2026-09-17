# Project Memory

## 项目定位

`codex-project-context` 是 Pi `pi-project-context` 的 Codex Desktop/CLI 移植。它把项目知识从 agent 隐式会话中分离出来，落盘为其他 agent 也能读取的上下文、记忆、原始 session archive、索引、技能候选和交接信息。

## 目录与运行时

插件仓库在 `/run/media/user/6b058d20-a617-484d-b7c6-cd7146baf77c/Projects/codex-project-context`。插件由 `.codex-plugin/plugin.json`、`hooks/`、`scripts/`、`skills/project-context-maintenance/`、`tests/` 和 README 组成；个人 marketplace 清单位于 `.agents/plugins/marketplace.json`。消费项目的数据目录位于项目根的 `.agents/memory/`，包括 `CONTEXT.md`、`MEMORY.md`、`HANDOFF.md`、`project-context.json` 与 `session-logs/`。

## 数据流

Codex hook 从 stdin 接收可见事件并归档到 `session.jsonl`；原始归档是事实源，`session.md` 和 `session-logs/INDEX.md` 是派生视图。SessionStart 注入有界上下文，Stop 阶段由 `project-context-maintenance` skill 生成 `project-context/2` 严格 JSON，再由 `contextctl.py apply-maintenance` 原子更新项目记忆。PreCompact/SessionEnd 用于交接和收尾。

## 历史补救

0.2.0 新增 `contextctl.py import-codex`：读取 Codex 本地 `sessions/` 与 `archived_sessions/` 的原生 rollout JSONL，依据 `session_meta.cwd` 路由到项目，按 4 MiB 原始字节分块写入 `native-codex-session` 记录，并以来源元数据标记。它不解析或改写原生 schema，重复运行通过记录 id 去重；无法明确归属项目的 rollout 也可以通过 `--unmatched-project-root` 显式保存为 unassigned，不会伪装成具体项目。

## 边界与验证

hook 归档只承诺保存 hook 可见内容；历史补救导入则保留原生文件中已有的完整字节，因此可回溯系统提示词、工具调用/结果等原生记录，但它们不等于当前 hook 可观察能力。凭据不应复制到摘要层。

插件已注册并启用为 `codex-project-context@codex-project-context-local`，源码版本 0.2.0；18 个单元测试和静态校验通过，安装缓存中有 0.2.0。源码更新后需要重新安装/刷新缓存，新 task 才会加载新版本。
