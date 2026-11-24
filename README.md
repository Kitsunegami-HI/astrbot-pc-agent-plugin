# astrbot_plugin_pc_agent · 远程电脑代理 v0.3.4

> 不在电脑前，也让 **Codex / Claude Code** 继续替你干活。

你在 QQ 里说一句「帮我修一下那个报错」，插件就在你自己的电脑上把命令行 AI 后台跑起来；
跑完把输出推回给你，再问你下一步。额度用完自动等，恢复了自动接着跑；配了验收命令（如 `npm test`），
每轮跑完自动验，不过就带着失败日志让它自己接着修；也可以让它自己决定下一步，连着推进到目标完成。

```
你 (QQ)  ──→  AstrBot  ──→  本机命令行 (codex / claude / aider)  ──→  验收命令 (npm test …)
                    ↑                         │                              │
                    └──────── 输出 / 额度 / 验收结果 ─────────────────────────┘
```

**AstrBot 在云服务器、AI 工具在你自己的 Windows 上？** 用「远程节点」模式：Windows 上常驻一个小客户端主动连服务器，
校园网断线、宿舍半夜拉闸都不丢任务输出，见[第十节](#十远程节点windows-客户端--校园网--宿舍断电场景)。

各版本改了什么，见同目录的 `CHANGELOG.md`。

---

## 一、安装

1. 把整个 `astrbot_plugin_pc_agent` 文件夹放进 `AstrBot/data/plugins/`。
2. WebUI → 插件 → 刷新，找到「远程电脑代理」，打开设置，至少填：
   - **admin_qq**：你的 QQ 号（强烈建议填，见“安全须知”）
   - **working_dir**：让它在哪个项目目录干活，如 `D:\code\myproj`。**不填不开跑**——
     插件不会悄悄退回 AstrBot 自己的目录去跑，免得 AI 改到机器人本体。
3. 保存，在 QQ 里发 `/pc sw`，看到白名单和目录都正常就通了。

**依赖**（`requirements.txt` 里都写了）：
- 服务端：`aiohttp>=3.8`（远程节点监听用）。AstrBot 安装插件时会自动装插件目录下的 `requirements.txt`，
  AstrBot 本身一般也已带 aiohttp；如果没装上，在 AstrBot 所用的 Python 环境里手动执行 `pip install "aiohttp>=3.8"`。
  不用远程节点的话，缺了它也不影响本机模式。
- Windows 节点客户端：`websockets>=12`。`install_node.ps1` 会自动执行 `pip install --user -r requirements.txt`，
  失败时会停下并提示；也可以手动执行 `py -3 -m pip install --user "websockets>=12"`。

前提：跑 AstrBot 的机器上，`codex` / `claude` 等命令**在终端里本来就能用、已登录**。

---

## 二、配置

| 配置项 | 作用 | 默认 |
| --- | --- | --- |
| `admin_qq` | 谁能用（QQ 号列表）；留空则用 AstrBot 管理员 | 空 |
| `working_dir` | 默认工作目录（必填，或给软件单独配 `cwd`） | 空 |
| `projects` | 多个项目目录 `{"blog": "D:\\code\\blog"}`，用 `codex@blog` 指定 | 空 |
| `software` | **软件白名单**，只有这里的能被调用 | codex / claude / aider |
| `output_tail_lines` | 每轮保留输出的最后多少行（另保留开头 `push_head_lines` 行） | 200 |
| `push_tail_lines` / `push_head_lines` | 推送时展示结尾 / 开头多少行 | 40 / 20 |
| `push_chunk_chars` | 单条消息最大字数 | 1200 |
| `push_max_messages` | 一次推送最多拆几条，超出提示用 `/pc log` | 4 |
| `push_max_chars` | 一轮输出推送的总字数上限 | 3000 |
| `notify_session` | 兜底推送会话（一般留空） | 空 |
| `default_timeout` | 单轮超时秒数（软件 `timeout` 为 0 时用它） | 1800 |
| `live_push_seconds` | 运行中每 N 秒推一次最近几行，`0`=关 | 0 |
| `passive_reply` | 任务等你回话时，同一会话里直接说话就当新指令 | 开 |
| `queue_same_dir` | 同一目录同时只跑一个任务，其余排队 | 开 |
| `resume_interrupted` | 被重启 / 插件重载打断的任务，恢复后自动续跑 | 关 |
| `quota_default_wait_minutes` | 认出额度用完但读不出恢复时间时，等多久再试 | 60 |
| `verify_max_attempts` | 验收失败后最多自动再修几轮（`0`=只验不修） | 3 |
| `autopilot_max_rounds` | 自主模式每次最多连跑几轮（含验收修复轮） | 6 |
| `autopilot_max_minutes` | 自主模式每次最多连跑多久 | 120 |
| `autopilot_sentinel` | 模型表示“目标完成”的哨兵词 | `__DONE__` |
| `watch_interval_seconds` | 后台巡检间隔（额度恢复、排队、重启通知） | 30 |
| `enable_llm_tool` | 允许 AstrBot 的模型在聊天里自己调用本插件开任务 | 关 |
| `nodes` / `node_*` | 远程节点相关，见第十节 | 关 |

> 在 WebUI 保存插件配置时 AstrBot 会重载插件，正在跑的任务会被打断（进程树被杀掉），
> 恢复后插件会通知你，`/pc go <编号>` 即可续跑；或打开 `resume_interrupted` 让它自动续。

---

## 三、白名单怎么写

```json
{
  "codex": {
    "cmd": ["codex", "exec", "--skip-git-repo-check", "{prompt}"],
    "resume": ["codex", "exec", "resume", "--last", "{prompt}"],
    "prompt_via": "arg",
    "timeout": 0,
    "verify": ["npm", "test"]
  },
  "claude": {
    "cmd": ["claude", "-p"],
    "resume": ["claude", "-p", "--continue"],
    "prompt_via": "stdin",
    "cwd": "D:\\code\\myproj",
    "env": {"HTTP_PROXY": "http://127.0.0.1:7890"}
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `cmd` | **必填**。首轮命令（数组），`{prompt}` 换成指令；arg 模式下忘写 `{prompt}` 会自动放最后 |
| `resume` | 第二轮起（以及额度恢复、你接话时）用的命令；留空就继续用 `cmd` |
| `prompt_via` | `arg`＝指令拼进命令行参数；`stdin`＝指令从标准输入喂进去（推荐，最不怕特殊字符） |
| `cwd` | 这个软件专用的工作目录，优先于 `working_dir` |
| `timeout` | 单轮超时秒数，`0` = 用 `default_timeout` |
| `env` | 额外环境变量 |
| `verify` / `verify_timeout` / `verify_shell` | 验收命令，见第四节 |
| `session_pattern` | 从输出里抓会话 id 的正则（第一个分组），配合 `resume` 里的 `{session}` |
| `quota_patterns` | 额外的“额度用完”特征正则（输出末尾命中、且退出码非 0 或整段输出不超过 5 行才算） |

偷懒写法：`"aider": "aider --yes-always --message {prompt}"`，按空格切成数组。

### 多个任务别串会话：`{session}`

`codex exec resume --last` / `claude --continue` 接的是“最近一次会话”。同一台机器上同时有几个任务、
或者你自己也在终端里用过，就可能接错。能从输出里读出会话 id 的工具，可以这样按任务精确接续：

```json
"codex": {
  "cmd": ["codex", "exec", "--skip-git-repo-check", "{prompt}"],
  "resume": ["codex", "exec", "resume", "{session}", "{prompt}"],
  "session_pattern": "session id:\\s*([0-9a-f-]{8,})"
}
```

还没抓到 id 时会退回 `cmd` 新开会话，并自动把“总目标 + 本轮要求”一起交给它。
各工具输出格式会变，**配之前先手动跑一次，确认输出里真有会话 id**。

### Windows 上的 `.cmd`

npm 装的 `codex.cmd` / `claude.cmd` 不能直接 exec。插件会先尝试把 npm 垫片还原成
`node xxx.js` 直接启动（不经过 cmd.exe）；还原不了才走 cmd.exe，这时如果指令里有引号、`&`、`%`、
换行等会被 cmd 解释的字符，插件会**拒绝执行**并提示你改成 `"prompt_via": "stdin"`——
验收失败日志被包进下一轮指令时几乎一定带这些字符，所以 Windows 上推荐一律用 stdin。

**只有白名单里的软件能被调用**——它能挡住配置之外的软件，但白名单里的 AI 工具本身就能执行任意命令，所以这只是护栏、不是沙箱（详见第十节第 5 小节）。

---

## 四、跑到绿为止：验收关卡

**每轮跑完都自动执行验收命令**（`npm test`、`cargo build`、`tsc --noEmit`……）。
通过才算这一轮真的完成；不通过就把失败输出（末尾 2500 字）连同总目标一起包成下一轮指令，让它自己接着修。

```
AI 改完 ──→ 跑验收 ──→ 通过 ──→ 普通模式：问你下一步 / 自主模式：让模型拟下一步
               │
               └─ 不通过 ──→ 带着失败日志再修一轮 ──→ 再验 ──→ …
                               （最多 verify_max_attempts 轮，用完停下问你）
```

```json
"codex": {
  "cmd": ["codex", "exec", "--skip-git-repo-check", "{prompt}"],
  "verify": ["npm", "test"],
  "verify_timeout": 600
}
```

需要 `&&`、管道时用 shell 模式（命令按原样交给系统 shell，只执行你自己写的配置，不会拼入任何 AI 输出）：

```json
"verify": "npm run lint && npm test", "verify_shell": true
```

| `verify_max_attempts` | 行为 |
| --- | --- |
| `3`（默认） | 验收不过自动再修，最多 3 轮；第 4 次还不过就停下问你 |
| `0` | 只验不修，不过就报给你 |

- 本轮 AI 运行**超时**时不跑验收，直接停下问你。
- **验收命令根本没启动起来**（命令名写错、节点名写错、被节点白名单拒绝）或在节点上被断电打断：单独报错并停下，
  **不算验收失败、不占修复次数、不会把报错喂给 AI**——否则 AI 会去改一段没坏的代码，白烧额度。
- 你接话（`/pc go`、直接回复）会把失败次数清零重新算。
- 修复指令里明确要求“只改必要的地方，不要为了通过去删测试”。

### 挑验收命令的原则

- **要快**：每轮都跑，用单测 / 类型检查 / lint，别用 e2e。
- **要确定**：随机挂的测试会让它去“修”不存在的 bug。
- **只检查不修改**：别配 `--watch`、`--fix`、`rm` 这类。

---

## 五、指令

| 指令 | 作用 |
| --- | --- |
| `/pc run <软件>[@项目] <指令>` | 开个任务，跑完推给你 |
| `/pc auto <软件>[@项目] <指令>` | 自主模式：自己决定下一步，连干到目标完成 |
| `/pc ls [all]` | 进行中的任务；`all` 连已结束的一起 |
| `/pc log [编号]` | 最近一轮输出（含最近一次失败的验收输出） |
| `/pc go [编号] [指令]` | 接着跑；不给指令就“继续” |
| `/pc stop [编号]` | 停下（连子进程树一起掐） |
| `/pc done [编号]` | 收工，不再接管你的聊天（在跑的也会停掉） |
| `/pc sw` | 白名单、目录、项目、开关状态（含节点在线情况） |
| `/pc node` | 远程节点：在线/离线多久、心跳、本地待补发条数、在跑的任务、上次断线原因 |
| `/pc clear` | 清掉已结束的任务记录 |
| `/pc reload` | 重新读配置 |
| `/pc help` | 指令一览 |

- 只配了一个软件时可以省略软件名：`/pc run 把报错修了`（这时整句都当指令，回执里会注明「按白名单里唯一的 xxx 处理」）。但只要第一个词里有 `@`，就一定按“软件@项目”解析：
  软件不在白名单、项目没配置都会明确报错，不会把整句当指令、悄悄丢掉 `@项目` 跑到默认目录里。
- 编号是 `/pc ls` 里的六位短号，输前 4 位以上也认。**省略编号**时取当前会话里最近一条合适的任务。
- 指令正文里的换行会原样保留。`/pc` 也可以写成 `/电脑`、`/远程`。

状态：**排队中 / 跑着 / 等你回话 / 等额度 / 完成 / 失败 / 已停**。

```
· a3f9c1 [跑着] codex@blog 第2轮 · 自主
  把构建脚本里的硬编码路径改成配置项
· 7b2e04 [等额度] claude 第1轮 · 11-23 18:40 恢复
  给登录接口补单元测试
· 5c11d0 [排队中] codex 第0轮
  顺手更新 README
```

---

## 六、典型用法

**下班路上，让它把报错修了**

```
你：/pc run codex 跑一下 npm test，把报错修了
我：收到，a3f9c1 开始跑了 · codex · 普通模式
（几分钟后）
我：✅ a3f9c1 · codex 第1轮结束 · 用时 4分12秒 · 退出码 0 · 接下来验收
    ……它的总结……
我：❌ 验收没过（第1次 · 退出码 1 · 用时 18秒）
    FAIL src/auth/session.test.ts …
我：♻️ 自己再修一轮（自动修复第 1/3 次）
我：✅ a3f9c1 · codex 第2轮结束 …
我：✅ 验收通过（npm test · 用时 21秒）
我：👉 a3f9c1 接下来怎么办？直接回我一句就是给它的新指令 …
你：顺手把 README 里的用法也更新一下        ← 直接说话就是新指令
```

**你说「你自己决定」**：`/pc auto codex 把整个项目的 any 换成具体类型，npm test 必须全绿`。
每轮结束（且验收通过）后，把「长期目标 + 验收结果 + 这轮输出」交给 AstrBot 当前的模型拟下一句；
模型回 `__DONE__` 就收工。到了轮数 / 时长上限会停下等你，`/pc go <编号>` 再给它一份新预算。
模型调用失败时会停下等你，而不是误判成“完成”。

**额度没了**

```
我：⏳ codex 额度用完了（a3f9c1）。
    预计 11-23 18:40 恢复，到点我自动接着跑。
    不想等：/pc stop a3f9c1；想立刻重试：/pc go a3f9c1
```

撞额度的那一轮不计入轮数，原来的指令会保留，到点原样重跑（并告诉它上次被额度打断）。

**多个项目**：配置 `projects: {"blog": "D:\\code\\blog", "api": "D:\\code\\api"}`，
然后 `/pc run codex@blog 修首页样式`。同一目录同时只跑一个任务，后来的会排队，轮到时通知你。

---

## 七、它是怎么工作的

```
/pc run → 选软件、定目录（项目 > 软件 cwd > working_dir，不存在就拒绝）→ 同目录有人在跑？排队
   │
   ├─ 后台子进程跑 codex / claude（独立进程组；超时、/pc stop 时整组杀掉）
   │     按块读输出（超长行不会出错）、去 ANSI、UTF-8/GBK 自动识别，保留开头 + 结尾
   │
   ├─ 撞额度？（只看输出最后几十行，强特征和弱特征都要求非 0 退出码；
   │     只有整段输出寥寥几行时强特征才不看退出码，避免 AI 写限流代码时被误判）
   │     解析恢复时间：try again in 2 hours 13 minutes / resets 3pm / Nov 25, 9:00 AM /
   │     |1732370400 时间戳 / 45分钟后 / 3 days；解析不出按默认等 → 巡检到点自动重跑
   │
   ├─ 推送「开头 + 省略 + 结尾」（有行数、字数、条数三道上限）
   │
   ├─ 配了 verify → 跑验收 → 不过且还有次数 → 失败输出包成下一轮指令 → 回到开头
   │                        └ 次数用完 → 停下问你
   │
   ├─ 自主模式 → 检查轮数/时长预算 → 模型拟下一句 / __DONE__ 收工
   └─ 普通模式 → 等你回话（同会话直接说话，或 /pc go）
```

任务存在 `data/plugin_data/astrbot_plugin_pc_agent/tasks.json`（进行中的全留，结束的留最近 50 条）。
AstrBot 重启或插件重载时，正在跑的那轮会被打断、进程树被杀掉，任务记为“等你回话”并在恢复后通知你；
等额度、排队中的任务会继续等。

**LLM 工具（可选）**：打开 `enable_llm_tool` 后，你在正常聊天里说「帮我在电脑上把测试修了」，
AstrBot 的模型可以自己调用 `remote_pc_agent` 开任务。工具内部同样只认管理员，非管理员的请求会被拒绝。

---

## 八、出问题了怎么查

| 现象 | 原因 / 怎么办 |
| --- | --- |
| `没开跑：还没配置工作目录` | 填 `working_dir`，或给软件配 `cwd`；`/pc sw` 能看到每个软件实际用哪个目录 |
| `❌ 起不来：找不到可执行文件` | 命令不在 AstrBot 进程的 PATH 里：`cmd` 写绝对路径，或在 `env` 里补 `PATH` |
| `只能经过 cmd.exe 启动……` | Windows 上改 `"prompt_via": "stdin"`，或 `cmd` 直接写 `node xxx.js` / `xxx.exe` |
| 报 unknown option | 命令行工具升级改了参数，照 `--help` 改 `cmd`，先 `/pc run <软件> 你好` 单独试 |
| 输出乱码 | 已按 UTF-8 → GBK 依次尝试；仍乱码就在 `env` 里加 `PYTHONIOENCODING: utf-8` 等 |
| 一直「跑着」不动 | 多半卡在交互式确认。`/pc stop`，确认 `cmd` 带了非交互参数（`-p` / `exec` / `--yes-always`） |
| 明明没撞额度却「等额度」 | `/pc log` 看最后几行；`/pc go <编号>` 立刻重跑。把那段输出发给作者改识别规则 |
| 撞了额度却没认出来 | 在该软件的 `quota_patterns` 里加它实际输出的那句话 |
| 新任务一直「排队中」 | 同目录有任务在跑；`/pc ls` 看是谁。不需要排队就关掉 `queue_same_dir` |
| 消息太多 | 调小 `live_push_seconds`（或 0）、调小 `push_max_messages` / `push_tail_lines` |
| 验收一直挂 | 先自己手动跑一遍验收命令，确认它本身能绿 |
| 直接说话没被当成指令 | 群聊里要 @机器人；同一会话有多个任务在等时会让你用 `/pc go <编号>` 指明 |
| 自主模式跑飞 | `/pc stop`，调小 `autopilot_max_rounds`，指令里把范围写死 |

所有日志都带 `[pc_agent]` 前缀。

---

## 九、已知限制与安全须知

### 限制

- 命令行 AI 的参数和输出格式经常变：**升级 codex / claude 后先试一句 `/pc run <软件> 你好`**。
- 额度识别靠输出文字，无法 100% 准确；识别规则可以用 `quota_patterns` 补。
- 自主模式依赖 AstrBot 当前配置的聊天模型；模型挂了会停下等你。
- 被动接话：同一会话里只有一个任务在等时，你的下一句普通聊天会被当成给它的指令（不再进普通对话），
  不想要就 `/pc done` 或关掉 `passive_reply`。

### 安全

**这个插件等于把你电脑的执行权限交给一条 QQ 消息。**

- `admin_qq` **一定要填**；留空就交给 AstrBot 的管理员配置，哪天那边改了就是全开。
- **尽量只在私聊里用**。群里被动接话要求 @机器人，但仍不建议放进群。
- `working_dir` / `projects` 指到具体项目，别指到 `C:\` 或整个用户目录；项目最好在 git 里，出事好回滚。
- `verify` 也是一条会被自动执行的命令，只填你自己会在终端里敲的检查命令。
- `enable_llm_tool` 默认关：打开后模型可以根据聊天内容自己开任务（仍只认管理员）。
- 白名单里的软件要能非交互运行，否则会在后台卡到超时。

---

## 十、远程节点（Windows 客户端 · 校园网 / 宿舍断电场景）

典型情况：AstrBot 跑在 Linux 云服务器上，codex 装在你宿舍的 Windows 电脑上。服务器连不进校园网，
所以反过来——**Windows 上跑一个客户端主动连出去**，服务器把每一轮命令（AI 指令、验收命令）派给它执行，
输出实时传回来。排队、验收关卡、额度等待、多项目、会话接回、自主模式全部照常工作，只是“在哪台机器上跑”变了。

```
QQ ──→ AstrBot（云服务器，插件监听 wss） ←══ wss 长连接（Windows 主动连出）══ pc_agent_node.py（宿舍 Windows）
                                                                                      │ 本地 SQLite 队列（落盘）
                                                                                      └─ codex / npm test …
```

### 1. 服务端配置

插件配置里：

```json
"node_listen_enable": true,
"node_listen_host": "127.0.0.1",
"node_listen_port": 8765,
"nodes": { "mypc": { "token": "用密码生成器生成的 32 位以上随机串", "machine_id": "" } },
"software": {
  "codex": {
    "node": "mypc",
    "cmd": ["codex", "exec", "--skip-git-repo-check", "{prompt}"],
    "resume": ["codex", "exec", "resume", "--last", "{prompt}"],
    "prompt_via": "stdin",
    "cwd": "D:\\code\\myproj",
    "verify": ["npm", "test"]
  }
}
```

- 软件里加 `"node": "mypc"` 就派给那台电脑；`cwd`、`projects` 里写的是 **Windows 上的路径**（服务端不检查，客户端检查）。
- **wss**：推荐用 Caddy / Nginx 反代并自动申请证书，插件只监听 `127.0.0.1`。Caddy 一行就够：
  `bot.example.com { reverse_proxy /pc-agent/ws 127.0.0.1:8765 }`。也可以直接填 `node_ssl_cert` / `node_ssl_key` 让插件自己提供 wss。
  记得在云服务器安全组里放行 443。
- 只接受登记过的节点：`node_id` 不在 `nodes` 里、token 不对都会被拒绝；`machine_id` 填上后只认那一台机器
  （客户端上报的机器码在 `/pc node` 里能看到，复制过来即可）。同一个节点重复连上来时，新连接顶掉旧连接。

| 配置项 | 作用 | 默认 |
| --- | --- | --- |
| `nodes` | 节点登记：`{节点名: {token, machine_id}}` | 空 |
| `node_listen_enable` / `_host` / `_port` / `node_ws_path` | 监听开关、地址、端口、路径 | 关 / **127.0.0.1** / 8765 / `/pc-agent/ws`（要让客户端直连才填 0.0.0.0，且必须配证书） |
| `node_ssl_cert` / `node_ssl_key` | 插件直接提供 wss 时的证书 | 空 |
| `node_offline_notify_minutes` | 节点离线超过多久、且有任务在等它时提醒一次 | 10 |
| `node_interrupted_policy` | 节点断电 / 崩溃打断了一轮：`ask` 停下等你；`resume` 重连后自动续跑（最多 3 次） | ask |

### 2. Windows 客户端安装

1. 把插件里的 `node_client` 文件夹复制到 Windows 上，比如 `D:\pc_agent_node\`。需要 Python 3.9+（已装）。
   **只复制源码**：如果你在别的机器上跑过它，**不要**把 `node_data/`（本地队列、日志）和 `client_config.json`（含 token）
   一起带过去——宿舍电脑上要重新填配置，旧队列带过去会把别处的积压当成这台机器的补发。
2. 复制 `client_config.example.json` 为 `client_config.json`，填：
   - `server_url`：`wss://bot.example.com/pc-agent/ws`
   - `node_id` / `token`：和服务端 `nodes` 里一致
   - `allowed_roots`：允许干活的目录根，如 `["D:\\code"]`——服务端发来的工作目录不在这里面就拒绝执行。
     比较前会用 realpath 解开符号链接和 junction。**留空 = 不限制目录**，启动日志和 `/pc node` 都会明确警告
   - `allowed_commands`：允许执行的程序名（只比对程序名）；`allow_shell`：是否允许 shell 形式的验收命令（`verify_shell`）。
     这两项只是防误操作的护栏，不是沙箱，见下面“安全”
3. 可以先执行 `py -3 pc_agent_node.py --check`：**只读**检查配置、目录白名单、本地队列（已存在时）和网络，
   结果只打到屏幕，不建 `node_data/`、不写日志；配置有错时退出码为 2。
4. 再双击 `run_console.bat` 前台跑一次，看到“注册成功”，在 QQ 里 `/pc node` 能看到它在线，就通了。
5. 关掉窗口，**右键以管理员身份运行 PowerShell**，进入目录执行：
   ```
   powershell -ExecutionPolicy Bypass -File .\install_node.ps1
   ```
   它会：装依赖 → 只读检查配置（有错就停下）→ 注册计划任务 `PCAgentNode`（**开机 30 秒后自动启动，不需要登录**；登录时也会再拉一次；
   崩溃后每分钟自动重启；不限运行时长；用电池也照跑）→ 关闭睡眠、休眠和快速启动，插电合盖不睡眠。
   过程中会让你输入 Windows 账户密码，这是“没登录也能运行”所必须的（用 PIN 登录的也填账户密码）；
   不想保存密码就加 `-NoPassword`，但这样断电重启后要有人登录才会运行。
6. 客户端运行期间还会主动阻止系统睡眠（`keep_awake`），双保险。
7. 卸载：`uninstall_node.ps1`。日志在 `node_data\node.log`（自动轮转），本地队列在 `node_data\spool.db`。

> 同一时刻只会有一个客户端实例（重复启动的会自动退出），所以“开机启动 + 登录启动”两个触发器不会冲突。

### 3. 校园网断线：会发生什么

- **任务照常跑完**：命令在 Windows 本机执行，跟网络无关。输出每 0.5～1 秒成批写进本地 SQLite 队列并 fsync 落盘。
- **重连后按顺序补发**：每条事件有递增编号，服务端处理后回确认，客户端才删除；断线期间积攒的全部按顺序补发，
  服务端按编号去重，所以不丢、不乱、不重。补发完，QQ 里照常收到“第 N 轮结束 / 验收结果”。
- **无限重试、永不退出**：断开后指数退避（5 秒起，翻倍，最长 5 分钟）并加随机抖动；稳定连上超过 1 分钟后退避重新计算。
- **日志里写清原因**：每次断开都会做一次探测，并只在原因变化时写一行日志：
  | 日志里的代码 | 含义 |
  | --- | --- |
  | `NET_DOWN` | 本机没网：网线/Wi-Fi 断了、路由器没起来、校园网整体断 |
  | `NET_PORTAL` | 校园网要重新认证（联网检测被重定向到登录页）——去浏览器登录一下 |
  | `DNS_FAIL` | 外网通，但服务器域名解析失败 |
  | `SERVER_DOWN` | 外网正常，但服务器端口连不上：服务器挂了 / 安全组 / 反代没起 |
  | `SERVER_REJECT` | 端口通但 WebSocket 握手失败：插件没开监听、路径或证书不对 |
  | `REJECT` | 服务器明确拒绝：node_id / token / machine_id 不对（重试间隔拉长到约 2.5～5 分钟） |
  | `FLAP` | 连接中途断了一下，但现在网络和服务器都可达（校园网闪断的典型情况） |
  | `HEARTBEAT` | 连接没断但 60 秒收不到服务器任何回应（半死连接），主动断开重连 |
- **服务端这边**：`/pc node` 看在线/离线多久、上次心跳、节点本地还有多少条待补发、服务端在等几条结果、上次断线原因。
  节点离线期间派给它的活会记着，连上后自动送达；`/pc stop` 的取消也会在连上后送达。
  离线超过 `node_offline_notify_minutes` 且有任务在等时，QQ 提醒一次，重连后再告诉你补发情况，闪断不刷屏。
- **服务端重启也不怕**：插件重载时不会取消 Windows 上正在跑的命令；这一轮的结果补发回来后**接回正常流程**——
  照常做额度识别、照常跑验收（验收不过照常自动修），和没重启过一样。任务已经往前走了的过期结果只存进记录。
- **不会“确认了却没存”**：服务端先把收到的位置（eid）fsync 落盘，再回确认；客户端收到确认才删本地记录。
  服务端在任何时刻崩掉，重启后客户端都会把没落盘的部分重新补发。
- **取消不会丢**：重连补发时“派活”和“取消”几乎同时到，或者取消比派活先到，客户端都会先记下取消，那一轮不会启动。

### 4. 宿舍 24:00 拉闸断电：会发生什么、要怎么设

断的是总电，电脑和路由器一起被直接拉闸，不是正常关机。

**断电那一刻**：正在跑的命令跟着电脑一起没了。已经回传或已经落盘的输出都在；没落盘的最多是最后不到 1 秒。

**第二天来电**：
1. 电脑开机（需要 BIOS 设置，见下）→ 30 秒后计划任务启动客户端（不需要登录）。
2. 客户端发现上次是非正常退出，并且队列里记着某个任务“开始了但没结束”，就**明确上报“被中断”**（带开始时间和发现时间），
   绝不当成正常结束、也不会拿去跑验收。客户端给每一轮都记了进程号和进程启动时间：如果只是客户端自己崩溃重启、
   那一轮的命令其实还活着，会先把它（连同子进程）结束掉再上报，避免续跑时同一个项目里跑出两个 AI 进程。
3. 服务端收到后：`node_interrupted_policy = ask`（默认）时，任务停在“等你回话”，QQ 收到
   `⚡ xxx 这一轮在节点 mypc 上被中断，没有正常结束…`，你 `/pc go xxx` 就接着跑（会先让 AI 检查项目当前状态）；
   设成 `resume` 则连上后自动续跑。`/pc node` 里也会标出“客户端上次是非正常退出”。

**开机时的顺序问题**：来电后电脑通常比路由器先就绪，路由器还要一两分钟，校园网可能还要重新认证。所以客户端在
**开机 10 分钟内启动时，先安静地等到开机约 75 秒后（再加随机 0～15 秒）才第一次连接**，之后从 15 秒起慢慢退避；整个过程日志里只有一行
“开机不久，等待网络就绪”，连上后再一行“已连上服务器（断了多少秒）”。如果早上校园网要网页认证，日志会出现 `NET_PORTAL`，
登录一下即可，客户端会自己连上。

**必须做的设置**：
- **BIOS 来电自动开机**：开机按 Del/F2 进 BIOS，找 `Restore on AC Power Loss` / `AC Power Recovery` / `After Power Failure`，
  设为 **Power On**。笔记本一般没有这个选项：插着电源时，来电后笔记本不会自己开机，只能靠早上有人按电源键
  （或者给笔记本保持电池供电过夜，它根本就不会断）。
- 用 `install_node.ps1` 的默认方式安装（保存密码 → 不需要登录也能自启），否则断电重启后会停在登录界面。
- 安装脚本已关闭快速启动和休眠（拉闸后快速启动的“混合关机”状态反而可能出问题）。
- 想让 AI 少受中断影响：把大任务拆小，或在 24:00 前避开长任务；被中断的那一轮重跑时，AI 会先看项目当前状态再继续。

### 5. 安全（请如实理解边界）

**真正的安全边界只有一条：谁能拿到 AstrBot 服务器（和插件配置里的 token）。**
能控制服务器或拿到 token 的人，就能让你的 Windows 执行命令，客户端拦不住。原因如下：

- `allowed_commands` 只比对程序名（`argv[0]` 去掉路径和扩展名）。默认名单里就有 `python` / `py` / `node` / `npx`，
  `python -c "任意代码"` 照样放行；codex、claude 这类 AI 工具本身也能执行任意命令。
- `allowed_roots` 只限制**启动时的工作目录**，进程启动后能读写你这个 Windows 账户能碰到的整台机器。
- 打开 `allow_shell` 等于把服务端给的整串命令原样交给 cmd.exe。

所以这两个白名单是**防误操作的护栏**（配错路径、手滑写错命令时会被挡下），**不是沙箱**。实际建议：

1. 服务器本身要安全：SSH 用密钥、及时更新，插件配置（含 token）别外传；一律用 **wss**（客户端默认拒绝明文 `ws://`），
   token 用 32 位以上随机串，并填上 `machine_id`。
2. **单独建一个普通（非管理员）Windows 账号专门跑节点客户端**，只把项目目录放在它能访问的地方，
   别用存着你个人资料、浏览器登录态、各种密钥的主账号；`install_node.ps1` 在那个账号下运行即可。
3. `allowed_roots` 一定要填（留空时日志和 `/pc node` 会警告），`allow_shell` 非必要不开。
4. 项目放在 git 里并经常推到远端，出事能回滚。

---

## 附：发版前检查

每次打包发布前，在插件目录的上一级执行：

```
python astrbot_plugin_pc_agent/tools/release_check.py --build astrbot_plugin_pc_agent.zip astrbot_plugin_pc_agent
```

它按排除规则打包（跳过 `node_data/`、`__pycache__/` 等），然后扫一遍刚打好的包：
- **必须处理**（退出码 1）：包里有 `node_data/`、`spool.db*`、`client_config.json`、`*.log`、`__pycache__/`、`*.pyc`、
  `.DS_Store`、`node.lock` 等运行时产物，或者内容里出现本机绝对路径、邮箱、看起来像真实 token / API 密钥的字符串；
- **人工确认**：长数字（是不是 QQ 号）、不在白名单里的域名。

只扫已有的包：`python astrbot_plugin_pc_agent/tools/release_check.py astrbot_plugin_pc_agent.zip`。
仓库里的 `.gitignore` 已忽略这些运行时文件。
