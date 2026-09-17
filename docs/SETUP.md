# 安装与配置手册

## 1. 环境要求

- Python **3.10+**（开发环境为 3.12），无需 `pip install`。
- 能访问 arXiv / Google Scholar / OpenAlex / Crossref（国内网络通常直连即可；arXiv 官方 API 被限流时会自动走 RSS）。
- 可选：本地 [Ollama](https://ollama.com/)（作为 LLM 兜底）、一个 QQ/163 邮箱的 **SMTP 授权码**、一个 Zotero **API key**。

```bash
git clone https://github.com/blueroaring/paper-radar.git
cd paper-radar
copy config.example.json config.json     # Linux/macOS: cp
python -m paper_radar serve
```

浏览器打开 <http://127.0.0.1:8848>。端口被占用就改 `config.json` 的 `app.port`，或 `python -m paper_radar serve --port 8899`。

> 只想先看看效果？什么 key 都不填也能跑：LLM 会退化成启发式卡片，Zotero/邮件功能保持关闭，检索与推荐表照常出。

---

## 2. 配置 LLM

推荐任意一家 OpenAI 兼容服务（DeepSeek 便宜、中文好）：

1. 到控制台 **⑤ 设置 → 大模型**，或直接编辑 `config.json`：

```json
{
  "llm": {
    "provider": "deepseek",
    "fallback_provider": "ollama",
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "enabled_for": { "profile": true, "summary": true }
  }
}
```

2. 把 key 写进 `secrets.json`（或在 UI 的 api_key 框里填、保存）：

```json
{ "llm": { "api_key": "sk-..." } }
```

3. 自检：

```bash
python -m paper_radar selftest
```

想省钱：把 `llm.batch_size` 调到 6~8（一次请求写多篇卡片），并把 `search.top_n` 控制在 10~15。
想完全离线：`"provider": "ollama"`，`"model": "qwen2.5:7b"`（先 `ollama pull qwen2.5:7b`）。

---

## 3. 配置 Zotero

1. 打开 <https://www.zotero.org/settings/keys/new>，勾选 **Allow library access** 和 **Allow write access**，创建 key。
2. 填进控制台 **⑤ 设置 → Zotero** 的 api_key，或者：

```json
{ "zotero": { "enabled": true, "api_key": "xxxxxxxxxxxxxxxxxxxxxxxx", "default_collection": "" } }
```

`user_id` 可以留空 —— 程序会用 `GET /keys/<key>` 自动解析。
`default_collection` 留空表示进"我的文库"根目录；填**分类名**或**分类 key** 都行（UI 的「拉取分类列表」能帮你确认名字）。

3. 验证：`python -m paper_radar selftest`，看到 `Zotero : OK userID=... 分类 N 个` 就成了。

> 入库走的是官方 Web API，Zotero 桌面端**没开也能写**，下次同步会自动回到本地库。重复的论文会按 DOI / 标题自动跳过。

---

## 4. 配置邮箱

先拿到 **SMTP 授权码**（不是登录密码）：

| 邮箱 | SMTP | 端口 | 加密 | 授权码在哪 |
|---|---|---|---|---|
| QQ | smtp.qq.com | 465 | ssl | 设置 → 账户 → POP3/SMTP服务 → 生成授权码 |
| 163 | smtp.163.com | 465 | ssl | 设置 → POP3/SMTP/IMAP → 开启并获取授权码 |
| Gmail | smtp.gmail.com | 587 | starttls | 需要应用专用密码 |
| Outlook | smtp.office365.com | 587 | starttls | 账户安全 → 应用密码 |

填进 **⑤ 设置 → 邮件**（授权码会写进 `secrets.json`），或：

```json
{
  "mail": {
    "enabled": true,
    "transport": "smtp",
    "smtp": { "host": "smtp.qq.com", "port": 465, "security": "ssl",
              "username": "you@qq.com", "from_name": "Paper Radar" },
    "to": ["you@qq.com"]
  }
}
```

**先干跑再真发**（强烈建议）：

```bash
python -m paper_radar digest --topic 1        # 只生成 data/reports/xxx.html，不发信
python -m paper_radar selftest --mail         # 真发一封短测试信
```

或者把 `mail.transport` 设为 `"file"`：邮件会写成 `data/outbox/*.eml`，用 Outlook 打开就能看排版。

---

## 5. 让"每天定时"真正跑起来

### 方案 A：常驻进程（最简单）

```bash
python -m paper_radar serve     # 控制台 + 定时器
python -m paper_radar daemon    # 只要定时器，不开网页
```

开机自启（Windows）：把 `run.bat` 的快捷方式丢进 `shell:startup`。

#### 桌面快捷方式：双击直接打开网页

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1
```

双击桌面上的 **Paper Radar** 会：读 `config.json` 里的端口 → 探测服务是否真的在跑 →
没跑就用 `pythonw` 无窗口拉起来 → 轮询到可用后再打开默认浏览器。约 3～8 秒；失败会弹框告诉你下一步怎么做。

| 我想… | 怎么做 |
|---|---|
| 换个端口 | 改 `config.json` 的 `app.port`，快捷方式自动跟随（端口不写死在脚本里） |
| 停掉服务 | `powershell -File scripts\open-console.ps1 -Stop` |
| 只起服务、不开浏览器 | `powershell -File scripts\open-console.ps1 -NoBrowser` |
| 换个快捷方式名字 | `install_shortcut.ps1 -Name "My Radar"` |
| 删掉快捷方式 | `install_shortcut.ps1 -Remove` |
| 确认浏览器真的请求到了 | `config.json` 里 `app.access_log: true` → 看 `data/access.log` |

### 方案 B：Windows 计划任务（推荐给不常开控制台的人）

```powershell
# 以当前用户身份注册：每天 08:30 跑一次并真的发信
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Time 08:30 -Send

# 查看 / 立即试跑 / 删除
Get-ScheduledTaskInfo -TaskName PaperRadar-Digest
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Test
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove
```

> **笔记本用户必读**：注册时会显式打开"错过补跑"（`StartWhenAvailable`）并解除电池限制。
> 不设这些，到点时电脑关着/用电池，任务会**静默跳过且不补跑** —— 详见第 9 节的排障表。

### 方案 C：Linux / macOS cron

```cron
30 8 * * * cd /path/to/paper-radar && /usr/bin/python3 -m paper_radar digest --send >> logs/cron.log 2>&1
```

> **同时开两个调度器会重复发信吗？不会。** 每日简报只推"这个方向从未推送过"的论文，
> 第二次运行会发现没有新论文而静默跳过。所以计划任务和内置定时器可以并存。
> 想核对实际跑了什么：看控制台 **③ 每日简报 → 最近运行**，或 `data/paper_radar.db` 的 `runs` 表。

---

## 6. 目录与数据

```
paper-radar/
├── config.json          你的配置（gitignore）
├── secrets.json         密钥（gitignore，权限 0600）
├── data/
│   ├── paper_radar.db   SQLite：方向、论文目录、推荐记录、运行日志
│   ├── reports/*.html   每日简报报告（可在 UI 里点开）
│   └── outbox/*.eml     mail.transport=file 时的邮件副本
└── logs/                计划任务输出（若你用了）
```

备份就备份 `config.json` + `secrets.json` + `data/paper_radar.db`。
想重新开始：删掉 `data/` 即可（已推荐的论文记忆也会清空）。

---

## 7. 高分记忆怎么用

**它解决的问题**：检索结果会被下一次检索冲掉，而真正值得精读的通常就是相关度最高的那几篇。

### 自动记忆（默认开启）

任何一次检索或每日简报跑完，相关度 ≥ `remember.min_score`（默认 **0.6**）的论文会自动记入
`remembered` 表，跨检索、跨简报长期保留。控制台 **④ 高分记忆** 里可以查看、写批注、导出、批量入 Zotero。
每日简报的邮件与报告里，达标论文会被打上 **★ 高分记忆** 标记。

阈值在页面上直接改，不用编辑 JSON：

| 阈值 | 效果 |
|---|---|
| 0.7~0.8 | 只记最强命中的几篇，清单很干净 |
| **0.6（默认）** | 与方向明显相关的工作，推荐日常使用 |
| 0.4~0.5 | 宁可多记，回头再筛 |

> 阈值调高**不会删记录**，只是这些论文不再显示 ★。想真正清掉用「忘记所选」。

### 手动记忆

因为阈值只是"自动线"，不达标的论文也能手动记：

- 在 **② 检索推荐** 勾选论文 → 点 **「★ 记入高分记忆」**；
- 批量补记历史推荐：**④ 高分记忆 → 按当前阈值回填历史推荐**；
- 或者把阈值临时调低 → 回填 → 再调回去（已记下的不会因调高而消失）。

### 导出

| 格式 | 适合 |
|---|---|
| Markdown | 贴进 Obsidian / Notion / 组会周报（含概要、特色、理由、批注） |
| BibTeX | `\input` 进 LaTeX，或拖进 Zotero / EndNote |
| CSV | Excel / pandas 二次加工 |
| JSON | 自己写脚本处理 |

```bash
python -m paper_radar remembered                            # 列出清单
python -m paper_radar remembered --export bibtex --out refs.bib
python -m paper_radar remembered --export markdown --out 精读清单.md
python -m paper_radar remembered --all --threshold 0.4       # 临时改阈值看看
python -m paper_radar remembered --backfill                  # 把历史推荐按阈值补记
python -m paper_radar remembered --forget <paper-key>
```

### 可选的"越用越准"

打开 `remember.feedback_as_seeds` 后，新记住的高分论文会被追加为该方向的**种子论文**，
下次「生成检索方案」会参考它们 —— 方向画像会随你的实际偏好漂移。
上限 `feedback_max_seeds`（默认 5）防止被带偏。
**默认关闭**：它会改变后续检索结果，属于要你显式开启的行为。

---

## 8. 常见问题

**Q：检索很慢？**
一次检索 = 检索式条数 × 数据源个数 个请求，外加若干次 LLM 调用。默认 6 条检索式 × 4 源 ≈ 30 秒（Google Scholar 限速 5 秒是主要开销），LLM 写 15 篇卡片再花 1~2 分钟。嫌慢就减检索式、减 `search.top_n`、关掉 `llm.enabled_for.summary`。

**Q：提示某个数据源失败？**
正常。每个源独立失败，UI 会把失败原因列出来。Google Scholar 会偶发人机校验，arXiv API 会限流（已自动降级 RSS），dblp/Semantic Scholar 在部分网络不可达（默认关闭）。

**Q：推荐的论文不相关？**
先看「生成检索方案」产出的关键词和检索式是否跑偏 —— 这是最关键的环节，直接手改即可。其次调 `search.weights`，把 `relevance` 权重拉高、`recency` 拉低。

**Q：邮件里能不能直接入库？**
不能，这是有意的：入库动作需要 Zotero 写权限，把它放进邮件链接等于把一个可被转发的 URL 变成写入口。邮件里给的是论文链接 + 本机控制台地址，勾选入库在本地完成。

**Q：换电脑要重配吗？**
把 `config.json` + `secrets.json` + `data/` 拷过去即可。仓库本身不含任何个人配置。

---

## 9. 排障：某天没收到邮件

**先按这三条各查 10 秒，能定位九成情况：**

```powershell
# ① 计划任务到底跑没跑？（LastTaskResult=0 才是成功；267011=从未运行）
Get-ScheduledTaskInfo -TaskName PaperRadar-Digest

# ② 任务日志有没有今天的新内容？（cmd 的 >> 重定向，跑过就一定会写）
Get-Item D:\paper-radar\logs\digest.log | Select-Object LastWriteTime
Get-Content D:\paper-radar\logs\digest.log -Tail 15

# ③ 有没有生成今天的报告？（有报告但没邮件 = 邮件配置问题；两者都没有 = 任务没跑）
Get-ChildItem D:\paper-radar\data\reports | Sort-Object LastWriteTime -Descending | Select-Object -First 3
```

| 现象 | 原因 | 处理 |
|---|---|---|
| `LastRunTime` 停在几天前，日志/报告也没有新的 | **到点时电脑是关机/睡眠的**，而且任务没设"错过补跑" | 见下方"任务设置"；临时补一次：`scripts\install_task.ps1 -Test` |
| `LastTaskResult` 不是 0 | 命令本身失败了（Python 路径变了、配置坏了……） | 看 `digest.log` 末尾的报错；再手动跑一次 `python -m paper_radar digest --send` 复现 |
| **有报告、日志里写"邮件发送失败"** | 见下方"SMTP 被代理拦截" —— 这是最阴的一种 | `python -m paper_radar diag-mail` 定位，修好后 `python -m paper_radar requeue-mail` 退回重发 |
| 有报告、但没邮件（日志里连失败都没有） | SMTP 授权码过期 / 收件人为空 / `mail.enabled=false` | 控制台 ⑤ 设置 → 邮件 →「发一封测试邮件」 |
| 报告里 0 篇，所以没发信 | 当天确实没有**没推送过**的新论文（`only_new`） | 正常行为。想放宽：加大 `digest.lookback_days` 或降低 `digest.min_score` |

### SMTP 被代理拦掉（"有报告但没收到邮件"的最常见原因）

**症状**：`digest.log` 里出现 `SSLEOFError: [SSL: UNEXPECTED_EOF_WHILE_READING]` 或
`handshake operation timed out`，报告正常生成，但邮件发不出去。

**根因**：代理软件（Clash / v2rayN 等）开了 **TUN 模式**时，它会接管所有 TCP。
若 SMTP 域名被解析到 fake-IP 段（`198.18.x.x`）而代理进程里没有对应映射
（典型场景：代理刚随开机重启、系统 DNS 里还留着上一轮的 fake-IP），
流量就还原不出域名、匹配不到 `DOMAIN-SUFFIX,qq.com,DIRECT` 这类规则，
掉进兜底的 `MATCH,<代理>` → 代理节点封 SMTP 端口 → TLS 握手被中断
（甚至直连真实 IP 也会被 TUN 接住而失败）。

**一键诊断**：

```bash
python -m paper_radar diag-mail      # 依次查 DNS / 465 / 587 / 真实登录，并给出结论
```

看到「解析到 198.18/198.19 段」且两个端口都失败，就是这个问题。

**处理**（按优先级）：

1. `ipconfig /flushdns` 后重试 —— 让系统 DNS 重新走一遍代理、把 fake-IP 映射补上（最简单，常见有效）；
2. 在代理规则里给邮件域名加 DIRECT，并用 Parsers 的 `prepend-rules` 固化
   （Clash 示例：`DOMAIN-SUFFIX,qq.com,DIRECT`），避免订阅更新后规则被冲掉；
3. 临时关闭 TUN 模式（改用系统代理）后重试。

**修好后补发漏掉的那批**：

```bash
python -m paper_radar requeue-mail   # 把"发信失败"的那几批退回待推送
python -m paper_radar digest --send  # 立即重发（不加 --send 只出报告）
```

> 发信失败时，Paper Radar 会把这些论文保持为 `pending`（待推送），**不会**标记成"已推送"，
> 所以下次定时运行也会自动重发。`requeue-mail` 是用来修复**旧版本**留下的、
> 已经被错误标成"已推送"的历史记录。

### 任务设置：笔记本上必须改默认值

`schtasks /Create` 的默认设置会让笔记本**静默跳过**每日任务 —— 不报错、不提醒：

| 设置 | schtasks 默认 | 后果 | 应该设为 |
|---|---|---|---|
| `StartWhenAvailable` | `false` | 关机/睡眠错过了就**永远不补跑** | `true` |
| `DisallowStartIfOnBatteries` | `true` | **用电池时根本不启动** | `false` |
| `StopIfGoingOnBatteries` | `true` | 跑一半拔电源就被杀 | `false` |

`schtasks.exe` **没有**设置这三项的开关，必须用 PowerShell 的 ScheduledTasks 模块。本仓库的 `scripts\install_task.ps1` 已经正确处理：

```powershell
# 重新注册（默认就带上面三项的正确值）
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Send

# 额外让它在到点时把电脑从睡眠唤醒（笔记本用电池时会吓人，默认关）
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Send -Wake
```

核对是否真的生效（**别只看注册时的回显**）：

```powershell
$s = (Get-ScheduledTask -TaskName PaperRadar-Digest).Settings
$s | Select-Object StartWhenAvailable, DisallowStartIfOnBatteries, StopIfGoingOnBatteries, WakeToRun
```

### 双保险：让控制台也常驻

计划任务之外，控制台自己带的定时器也会在到点时发信（改时间立刻生效，且有 180 分钟补跑窗口）。把它加到开机启动，就多一层保险：

```
Win+R → shell:startup → 把 run.bat 的快捷方式丢进去
```

两层同时开**不会重复发信**：每日简报只推"这个方向从未推送过"的论文，第二次运行会发现没有新论文而静默跳过。

> ⚠️ 注意：控制台的补跑窗口是 180 分钟（`digest.catch_up_window_minutes`），
> 所以上午 11:30 之后才开机的话，它不会补发；**计划任务的 `StartWhenAvailable` 没有这个窗口限制**，
> 开机后就会补跑一次。这也是为什么推荐用计划任务作为主力。
