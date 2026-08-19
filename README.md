## Agent Relay

Agent Relay 是运行在开发机上的远程 Agent 控制面。它将 Codex CLI 和 Claude Code CLI 统一为持久会话、事件流、审批、取消和交接模型，并通过 Telegram long polling 与本机 HTTP/SSE API 暴露这些能力。

Telegram Mini App 是主要交互界面，按设备提供不同布局：

- 桌面端使用常驻任务侧栏，手机端使用可展开抽屉；顶部显示项目与 Agent，底部固定输入框。
- 每条任务可以独立选择模型、推理强度和操作权限，提交后的快照不会被排队期间的界面调整覆盖。
- 运行卡片展示经过安全过滤的状态、工具调用、命令摘要和增量输出，不展示模型私有思维链。
- 切换会话或离开项目不会中断后台任务；连续发送的消息会持久排队，并在同一项目内串行执行。
- Agent 引用或本次任务更新的文件会生成预览卡。图片、文本/代码、PDF、音频和视频可以直接预览，其他文件可以安全下载。
- 未选择项目时可以进行通用只读对话；需要读取或修改代码时再进入具体项目。普通 Bot 聊天只保留快捷入口和任务通知。

默认安全主路径采用“只读规划—人工审批—受控执行”：Agent 先生成实施计划，远程用户审阅整份计划后决定批准或拒绝，获批后才启动一次有写权限的执行。Mini App 还提供两种需要明确承担风险的模式：

- `workspace_auto`：跳过人工计划批准，但保留 Codex 工作区沙箱或 Claude 工具白名单。
- `full_access`：跳过人工计划批准并绕过 CLI 沙箱或权限检查，必须由用户二次确认。

当前审批粒度是“整份计划的一次执行”，不是逐文件、逐命令或逐工具预批准。执行期间可以查看状态、工具摘要和结果，也可以请求停止；停止会终止进程组，但不会回滚已经写入的文件或已经发生的外部副作用。

## 能力与边界

- 同一个会话可以在 Codex 与 Claude Code 之间切换；每个 Agent 保留独立的原生 session id，切换后注入长度受限的跨 Agent 交接记录。
- 普通消息由 Agent 在只读阶段自动判断：可只读完成就直接回答，需要变更则展示计划并等待发起人审批。
- 获批执行使用的计划与 Telegram 展示文本是同一快照；超过 2400 字或协议解析异常的计划会失败关闭，不显示批准按钮。
- Claude Code 的 `stream-json` 可以提供文本增量；Codex `exec --json` 当前只提供工作状态、工具事件以及完成后的 Agent 消息，没有 token 级文本 delta。
- 隐藏思维链不会转发或保存。远程用户看到的是 `thinking/working` 状态、工具名称与脱敏参数/结果摘要、公开回答和 usage 事件。
- Telegram 同时校验 chat id 与 user id，并且只有任务发起人能批准、拒绝或停止该任务；HTTP 默认仅监听 `127.0.0.1`，业务接口始终要求 bearer token。
- SQLite 使用 WAL、外键、事务化审批、单会话唯一活跃任务和跨会话 canonical workspace 租约；同一仓库不会被两个 Agent 并发操作，服务重启时进行中的任务标记为 `interrupted`，不会自动重放。
- `WORKSPACE_ROOTS` 限制远程可选择的工作区，子进程只继承显式环境变量白名单。它们不是完整的操作系统沙箱，生产部署仍应使用专用 OS 用户或可选容器隔离。详见[安全模型](docs/安全模型.md)。

当前版本把本地 Codex/Claude CLI 视为受信执行依赖，控制面与 CLI 仍运行在同一个专用 OS 身份下。因此，一台 Relay 实例只应服务一个完全互信的团队或工作区信任域。

相互敌对的多租户场景需要把 worker 拆到不同 UID 或一次性容器，并且不向 worker 挂载控制面数据库和 token；当前实现不能作为这类场景的强隔离执行平台。

## 快速开始

运行前需要准备：

- macOS 或 Linux；
- Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)；
- 在同一 OS 用户下完成登录的 `codex` 和 `claude` CLI。

最低兼容版本为 Codex 0.148.0 与 Claude Code 2.1.163，可选镜像默认锁定 Codex 0.148.0 与 Claude Code 2.1.235。`doctor` 会拒绝版本过旧、未报告登录或位于不安全可写路径中的 CLI。

不要把 CLI 凭据、Bot token 或 API token 写入仓库；本项目也不会读取或复制参考项目中的任何真实凭据。

```bash
$ cd /Users/yoyo/Workspace/jun_workspace/agent-relay
$ uv sync --locked --all-groups
$ cp .env.example .env
$ chmod 600 .env
```

编辑 `.env`，至少设置一个绝对 `DEFAULT_WORKSPACE`、只包含必要目录的 `WORKSPACE_ROOTS`，并生成 `API_BEARER_TOKEN`：

```bash
$ openssl rand -hex 32
```

把生成值写入 `.env` 后运行：

```bash
$ uv run agent-relay doctor
```

本地先做无 Telegram 的演示：

```bash
$ uv run agent-relay demo --agent codex --workspace /absolute/path/to/project --mode ask --prompt "概括这个项目的入口和测试方式"
$ uv run agent-relay demo --agent claude --workspace /absolute/path/to/project --mode run --prompt "给 README 增加本地测试说明"
```

`run` 演示会先输出只读计划，再在终端明确询问是否执行；没有危险的自动批准选项。确认 CLI 与工作区后启动守护进程：

```bash
$ uv run agent-relay serve
```

`serve` 会再次执行 doctor 门禁；配置、版本、登录报告或 CLI 路径安全任一失败都会拒绝启动，不能通过跳过单独的 doctor 命令绕过。

默认 HTTP 地址为 `http://127.0.0.1:8787`。`/health/live` 只表示进程存活；`/health/ready` 还会检查数据库、两种 CLI、API token，以及启用时的 Telegram polling 是否仍在运行。

## Telegram 配置

1. 在 Telegram 中打开官方 `@BotFather`，执行 `/newbot`，保存得到的 token。不要把 token 发到群聊或提交到 Git。
2. 先直接给新 Bot 发一条消息，再临时调用 `getUpdates` 获取 `message.chat.id` 和 `message.from.id`。URL 中包含 token，避免放入 shell history；推荐用临时环境变量并在使用后清除：

   ```bash
   $ read -s TELEGRAM_SETUP_TOKEN
   $ curl -sS "https://api.telegram.org/bot${TELEGRAM_SETUP_TOKEN}/getUpdates"
   $ unset TELEGRAM_SETUP_TOKEN
   ```

3. 在 `.env` 设置 `TELEGRAM_ENABLED=true`、`TELEGRAM_BOT_TOKEN`、逗号分隔的 `TELEGRAM_ALLOWED_CHAT_IDS` 与 `TELEGRAM_ALLOWED_USER_IDS`。两份白名单必须同时命中；群聊中不同成员不能借用已授权 chat id。
4. 重启 `agent-relay serve`。同一个 Bot token 只能有一个 long-polling 实例；HTTP 409 表示另一个进程或部署正在消费更新。

启用 Telegram 内嵌工作台时，还需要为本机 `/app` 提供稳定 HTTPS 地址，并设置 `TELEGRAM_WEBAPP_URL=https://your-relay.example/app`。Bot 会自动把菜单按钮配置为“打开工作台”。

工作台 API 会校验 Telegram `initData` 的签名和有效期，直接访问公网 API 无法获得会话数据。Cloudflare Quick Tunnel 只适合演示；生产环境应使用固定域名的命名 Tunnel 或等价 HTTPS 反向代理。

打开工作台后可以直接发送自然语言，无需预先选择项目：

- 无项目状态只用于通用只读对话，不检查文件或执行命令。
- 需要结合代码库处理时，从顶部工作范围或“选择项目”进入项目。
- 选择项目后，可以为下一条任务选择“请求批准”“工作区自动执行”或“完全访问”。
- “工作区自动执行”跳过人工批准，但保留 Codex 工作区沙箱或 Claude 工具白名单。
- “完全访问”绕过 CLI 权限检查，选择时需要二次确认。
- 权限模式随 run 持久化；通用对话会由服务端强制降为只读。

```text
普通文字                     自动回答，或在需要变更时请求确认
/home                        打开主页
/new                         开始无项目的新对话
/projects                    选择项目并开始项目对话
/sessions                    用按钮查看和切换最近会话
/agent                       用按钮切换 Codex 或 Claude
/run 内容                    强制先展示计划，再用按钮决定是否执行
/status                      查看当前任务
/stop                        停止当前任务
/help                        查看简明帮助
```

普通文本默认按 `auto` 处理。Agent 的第一阶段始终只读，只能返回直接回答或一份可审批计划；结构异常时会失败关闭，不会进入写入阶段。`/ask` 可强制只读，`/run` 可强制规划；`/switch`、`/approve` 和 `/reject` 仍作为兼容命令保留。Telegram 只更新一条过程消息，避免把每个 token 变成一条新消息。

## HTTP/SSE 演示

业务接口使用 bearer token；服务端 `API_ACTOR_ID` 把该 token 固定映射为一个逻辑所有者，客户端自报的 `X-Actor-ID` 会被忽略。一个 token 仍只代表一个完全互信域，不要分发给互不信任的租户。

```bash
$ export RELAY_TOKEN='从 .env 读取的 API_BEARER_TOKEN'
$ export RELAY_URL='http://127.0.0.1:8787'

$ curl -sS -H "Authorization: Bearer ${RELAY_TOKEN}" \
    -H 'Content-Type: application/json' \
    -d '{"workspace":"/absolute/path/to/project","agent":"codex","title":"demo"}' \
    "${RELAY_URL}/v1/conversations"
```

记下返回的 `id` 为 `CONVERSATION_ID`，提交需要审批的任务：

```bash
$ export CONVERSATION_ID='完整会话 ID'
$ curl -sS -H "Authorization: Bearer ${RELAY_TOKEN}" \
    -H 'Content-Type: application/json' \
    -d '{"mode":"run","prompt":"为项目补一条只读且可重复的健康检查"}' \
    "${RELAY_URL}/v1/conversations/${CONVERSATION_ID}/runs"
```

记下 `RUN_ID`，订阅 SSE。断线重连时可以把最后收到的事件序号放入 `after`：

```bash
$ export RUN_ID='完整任务 ID'
$ curl -N -H "Authorization: Bearer ${RELAY_TOKEN}" \
    "${RELAY_URL}/v1/runs/${RUN_ID}/events/stream?after=0"
```

确认 `status=awaiting_approval` 并人工审阅 `plan` 后批准；也可以把 `approve` 换成 `reject`：

```bash
$ curl -sS -X POST -H "Authorization: Bearer ${RELAY_TOKEN}" \
    -H 'Content-Type: application/json' \
    -d '{"decision":"approve"}' \
    "${RELAY_URL}/v1/runs/${RUN_ID}/decision"

$ curl -sS -X POST -H "Authorization: Bearer ${RELAY_TOKEN}" \
    "${RELAY_URL}/v1/runs/${RUN_ID}/cancel"
```

完整端点、事件和错误语义见 [HTTP API](docs/接口说明.md)。

## 生产部署

推荐原生部署，因为本地 CLI 的登录态、OS 沙箱和工作区权限最容易保持一致：

- macOS：使用当前开发者账号的 LaunchAgent，参考 [launchd 模板](deploy/com.agent-relay.plist)；不要使用 root LaunchDaemon。
- Linux：使用专用、无登录 shell 的 `agent-relay` 用户和 [systemd 模板](deploy/agent-relay.service)，只授权一个工作区根目录。
- Docker：仅作为隔离更强但兼容性需要单独验收的选项。Compose 明确 bind mount 一个工作区、Codex 状态目录和 Claude 状态文件/目录，非 root、只读根文件系统、无额外 capabilities；默认不会启用危险权限，只有任务发起人明确选择并二次确认“完全访问”时才会为该次 run 启用对应 CLI 参数。

详细安装、升级、备份和容器限制见[部署指南](docs/部署指南.md)与[运行手册](docs/运维手册.md)。

## 自测门禁

```bash
$ uv lock --check
$ uv run ruff check .
$ uv run ruff format --check .
$ uv run pytest
$ uv build
$ AGENT_RELAY_ENV_FILE=.env.example docker compose --env-file .env.example config
```

单元测试使用模拟 CLI 事件和受控子进程，不消耗真实 Codex/Claude 额度。`doctor` 检查本机配置和 CLI 可用性；`demo` 才是需要真实 CLI 登录态的手工集成验收。完整测试分层和交付口径见[测试指南](docs/测试指南.md)。

## 文档

完整阅读顺序和各文档职责见[文档索引](docs/README.md)。按主题可直接进入：

- [架构、状态机与数据库](docs/架构设计.md)
- [配置项](docs/配置说明.md)
- [HTTP API 与 SSE 事件](docs/接口说明.md)
- [生产部署](docs/部署指南.md)
- [威胁模型与安全边界](docs/安全模型.md)
- [运行、备份与故障排查](docs/运维手册.md)
- [测试与发布门禁](docs/测试指南.md)
