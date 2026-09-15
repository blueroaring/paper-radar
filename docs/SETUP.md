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

1. 到控制台 **④ 设置 → 大模型**，或直接编辑 `config.json`：

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
2. 填进控制台 **④ 设置 → Zotero** 的 api_key，或者：

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

填进 **④ 设置 → 邮件**（授权码会写进 `secrets.json`），或：

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

# 查看 / 删除
schtasks /Query /TN PaperRadar-Digest /V /FO LIST
schtasks /Delete /TN PaperRadar-Digest /F
```

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

## 7. 常见问题

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
