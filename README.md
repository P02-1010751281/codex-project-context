# Codex Project Context

一个面向 Codex Desktop/CLI 的本地插件，把 Pi project-context 的文件组织和数据流适配到 Codex hook 生命周期。

插件根目录就是本目录，不需要再套一层 `plugins/`。

## 数据流

```text
Codex hook stdin
  -> .agents/memory/session-logs/<session-id>/session.jsonl
  -> session.md + INDEX.md
  -> SessionStart 有界注入 CONTEXT/MEMORY/active skills
  -> Stop 触发当前 Codex 继续轮次执行 maintenance skill
  -> CONTEXT.md / memory.jsonl -> MEMORY.md / skills / HANDOFF.md
```

运行期状态写入使用插件的项目根目录（优先 Git root）：

```text
.agents/
├── skills/<name>/SKILL.md
└── memory/
    ├── MEMORY.md
    ├── memory.jsonl                 # MEMORY 的 append-only 权威源
    ├── memory-log-*.jsonl           # journal 轮换归档
    ├── MEMORY.md.memory-backup-*     # 写前字节备份
    ├── CONTEXT.md
    ├── HANDOFF.md
    ├── project-context.json
    ├── migration-manifest.json
    ├── errors.log
    ├── .gitignore                   # 本地产物忽略规则
    └── session-logs/
        ├── INDEX.md
        └── <session-id>/
            ├── session.jsonl
            ├── session.md
            ├── session-state.json
            └── maintenance-request.json
```

`session.jsonl` 是唯一权威归档源。transcript 可读时保存 hook 看到的新增原始字节；不可读或格式变化时保存结构化 hook 事件。hook 能看到的会话信息都会保留，包括 transcript 中的消息、工具调用、工具输出和压缩记录（若它们存在于 `transcript_path`）。插件无法获得 Codex 内部未暴露给 hook 的系统提示词、工具 schema 或后端状态。

记忆层与 Pi 保持同一数据流：`memory.jsonl` 是 append-only 记录源，`MEMORY.md` 是可重建渲染。每次 maintenance 写入前保留原始 `MEMORY.md` 字节；旧版本误写入的 JSON consolidation reply 会在内存中恢复并把原文件留作备份。journal 存在不可恢复的损坏时 fail closed，不覆盖现有记忆；可识别的损坏记录会保留并写入 `errors.log`。

`maxMemoryChars` 控制渲染后的长期记忆上限，默认 32,000 字符，可在 `.agents/memory/project-context.json` 中配置（4,000–200,000）。超限时按整行截断并在末尾写入 `_[memory truncated at ...]_` marker；session 原始归档和工具数据不受此投影上限影响。

插件也提供一次性历史补救导入：`import-codex` 会读取 Codex 本地 `sessions/`/`archived_sessions/` 中的原生 rollout JSONL，依据首个 `session_meta.cwd` 路由到项目，并以不解析 schema 的原始分块写入项目归档。它会保留原生文件中已有的系统提示词、工具调用/结果等字节，但这些内容只作为原始证据，不会被当作当前 hook 能力的承诺；没有可靠项目 cwd 的临时 rollout 会跳过。

哈希只用于三件事：判断 transcript 是追加还是重写、对原始 payload 去重、让 Pi 迁移幂等。不会建立目录树 digest，也不把它当作安全认证。

## 本地开发

```bash
python3 tests/run_tests.py
python3 scripts/contextctl.py --help
```

历史 Codex session 补救导入示例：

```bash
python3 scripts/contextctl.py import-codex \
  --source-root /home/user/.codex/sessions \
  --source-root /home/user/.codex/archived_sessions \
  --project-root /run/media/user/6b058d20-a617-484d-b7c6-cd7146baf77c/Projects/CipherCat \
  --project-root /run/media/user/6b058d20-a617-484d-b7c6-cd7146baf77c/Projects/Quantum_Matrix \
  --project-root /run/media/user/6b058d20-a617-484d-b7c6-cd7146baf77c/Projects/UniField \
  --project-root /home/user/Documents/Codex \
  --unmatched-project-root /home/user/Documents/Codex
```

`--unmatched-project-root` 是显式的“未归属归档”入口；它只把无法映射到项目的原生 rollout 保存到指定工作区，并在元数据中保留原始 `cwd`，不会把它们算作任何具体项目的上下文。

在 Codex Desktop 的本地插件入口载入此目录后，hook 会由 Codex 设置 `PLUGIN_ROOT`；不要把本目录复制到项目的 `plugins/` 子目录。

## 加载与测试

### 1. 静态校验和单元测试

```bash
PLUGIN=/run/media/user/6b058d20-a617-484d-b7c6-cd7146baf77c/Projects/codex-project-context

python3 /home/user/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py "$PLUGIN"
python3 "$PLUGIN/tests/run_tests.py"
```

### 2. Codex Desktop 加载

在 Codex Desktop 的插件页面进入本地插件/创建插件入口，选择下面这个目录（目录内必须直接包含 `.codex-plugin/plugin.json`）：

```text
/run/media/user/6b058d20-a617-484d-b7c6-cd7146baf77c/Projects/codex-project-context
```

审查并信任插件的 hooks，然后启用插件。首次启用或更新插件后，创建一个新的 Codex task 再测试；插件 hook 由 Codex 在新会话中加载。若当前 Desktop 版本没有本地目录入口，则需要把本目录放进一个本地 marketplace 后，从该 marketplace 安装。

本目录已经带有本地 marketplace 清单，因此也可以直接用 CLI 完成注册和安装：

```bash
MARKET=/run/media/user/6b058d20-a617-484d-b7c6-cd7146baf77c/Projects/codex-project-context

codex plugin marketplace add "$MARKET"
codex plugin add codex-project-context --marketplace codex-project-context-local
codex plugin list --json | rg -n -C 4 'codex-project-context'
```

这里的 marketplace 清单位于 `.agents/plugins/marketplace.json`，插件源码仍直接位于 `codex-project-context/`，没有增加 `Projects/plugins/` 层。安装完成后刷新或重启 Desktop，在已安装插件中启用它。

### 3. 不启动模型的 hook 冒烟测试

```bash
PLUGIN=/run/media/user/6b058d20-a617-484d-b7c6-cd7146baf77c/Projects/codex-project-context
TMP=$(mktemp -d)
printf '%s\n' '{"role":"user","content":"smoke"}' > "$TMP/transcript.jsonl"

printf '%s\n' "{\"session_id\":\"smoke-1\",\"cwd\":\"$TMP\",\"transcript_path\":\"$TMP/transcript.jsonl\",\"hook_event_name\":\"SessionStart\",\"source\":\"startup\"}" \
  | python3 "$PLUGIN/hooks/context_hook.py"

find "$TMP/.agents/memory" -maxdepth 3 -type f -print | sort
```

应能看到 `CONTEXT.md`、`MEMORY.md`、`session-logs/.../session.jsonl`、`session.md` 和 `INDEX.md`；首次 maintenance 写入后还会出现 `memory.jsonl` 和写前备份。这个测试只验证 hook 和落盘，不调用模型。

### 4. 真实项目回归

在一个实际项目中启动新的 Codex task，先执行几次会产生工具调用的操作，然后检查：

```bash
find .agents/memory -maxdepth 3 -type f -print | sort
sed -n '1,120p' .agents/memory/session-logs/INDEX.md
sed -n '1,160p' .agents/memory/CONTEXT.md
```

再关闭当前 task，在同一个项目中创建另一个 task，或更换模型/agent，确认新的 `SessionStart` 仍能注入同一份项目上下文。`session.jsonl` 是原始归档，`session.md` 和 `INDEX.md` 是可重建的派生视图。
