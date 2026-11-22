# ziv

单二进制终端 AI 编码代理 —— Zig 实现,零运行时依赖,一个可执行文件带走全部能力。

## 安装

**方式一:发行版(推荐日常使用)**——ReleaseFast + strip,快且与分发产物一致:

```bash
cd agent && ./release.sh                      # 打包全平台 → agent/dist/ziv-v<版本>/
sudo cp agent/dist/ziv-v*/ziv-aarch64-macos /usr/local/bin/ziv   # macOS arm64 示例
# 或用打包目录里的离线安装器(默认 /usr/local,无权限时自动回落 ~/.local):
./agent/dist/ziv-v*/install.sh
```

**方式二:本地开发版**——Debug 构建,崩溃带完整符号栈(排障利器),改完源码即装:

```bash
cd agent && zig build
sudo cp zig-out/bin/ziv /usr/local/bin/ziv
```

验证:`ziv --version` → `ziv <版本> (Zig 0.17.0)`。配置与记忆存于 `~/.config/ziv/`(全局)与各项目的 `.ziv/` 目录,与二进制安装位置无关。

> 注意:两个版本别混装到不同 PATH 位置(如 `/usr/local/bin` 与 `~/.local/bin` 各一份)——靠前的会遮蔽另一个,出现"装了新版还是旧行为"的假象。

## 使用

```bash
ziv              # 进入交互 REPL(首次使用先在 ~/.config/ziv/settings.json 配 API key,或用 /config 菜单)
ziv -p "问题"     # 单发问询,答完即退(脚本友好)
ziv --version
```

会话内常用命令速览:

| 命令 | 作用 |
|---|---|
| `/mode [名]` | 权限模式:default / plan(只读规划,经 plan_approve 呈批出环)/ accept_edits / dont_ask / auto / bypass;无参轮换 |
| `/model [名]` | LLM 模型切换(自定义网关名如 glm 系可用);无参开交互菜单 |
| `/config` | 交互配置菜单(API key / Base URL / 输出风格,自动保存) |
| `/compact` | 压缩上下文(`/compact phi` 为无 LLM 的纯截断) |
| `/mcp add <名> <命令…>` | 接入 MCP 服务器(stdio/HTTP);配置后启动自动连接 |
| `/quit` | 退出(退出前自动做记忆提取与转录存档) |

完整命令、六档权限语义、工具清单、记忆系统见 [agent/USAGE.md](agent/USAGE.md)。

## 核心特性

- **20+ 内置工具**(Bash/文件三件套/搜索/后台任务/子代理 fan-out/计划呈批/自定时唤醒…),comptime 注册、并发安全分级
- **六档权限模式** + 规则引擎(allow/deny/ask glob)+ always-allow 学习(答一次 `a`,规则合成落盘)
- **上下文工程**:φ/e 双梯压缩、摘要熔断、关键事实保全、随行 `<system-reminder>` 注入(文件引用/后台通知/plan 契约)
- **记忆飞轮**:会话提取 → SQLite 记忆库 + 知识图谱 → 反思质量门 → 跨会话按衰减×相似度注入
- **MCP / Skills / Hooks** 生态:stdio+HTTP 服务器桥接、SKILL.md 兼容、PreToolUse/Stop 等生命周期钩子

## 文档

| 想了解 | 看这里 |
|---|---|
| 使用方法(命令/模式/工具) | [agent/USAGE.md](agent/USAGE.md) |
| 架构设计 | [agent/DESIGN.md](agent/DESIGN.md) |
| 目录导览 | [agent/docs/repo-layout.md](agent/docs/repo-layout.md) |
| TUI 内幕 | [agent/docs/tui-architecture.md](agent/docs/tui-architecture.md) |
| 设计参考(原 Claude Code 复刻文档) | [agent/docs/reference-claude-code/](agent/docs/reference-claude-code/) |

## 平台

macOS(arm64/x64)、Linux(x64/arm64 musl 静态)、Windows(gnu)——`release.sh` 一键全出。
