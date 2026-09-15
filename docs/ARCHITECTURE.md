# 架构说明

## 一张图看懂数据流

```
                    ┌──────────────────────────┐
   用户写方向概要 ──►│ profile.py / LLM 方向画像 │──► 关键词 keywords
   用户给种子论文 ──►│  (种子解析 + 画像归纳)     │──► 检索式 queries
                    └──────────────────────────┘
                                │
                                ▼
        ┌───────────────────────────────────────────────┐
        │ engine.collect()  并发扇出                     │
        │   queries × sources（线程池 + 按域名限速）      │
        │   arxiv / google_scholar / openalex /         │
        │   crossref / dblp / semantic_scholar          │
        └───────────────────────────────────────────────┘
                                │  list[Paper]（脏数据、重复、跨源）
                                ▼
        ┌───────────────────────────────────────────────┐
        │ rank.py                                        │
        │   dedupe()  DOI/arXiv 指纹 + 归一化标题两轮合并 │
        │   apply_filters()  年份 / 引用 / venue / 排除词 │
        │   score_papers()  相关度+时效+引用+发表处 加权  │
        └───────────────────────────────────────────────┘
                                │  Top-N Paper
                                ▼
        ┌───────────────────────────────────────────────┐
        │ summarize.py  批量生成卡片                     │
        │   内容概要 summary / 文章特色 highlights /     │
        │   推荐理由 reason（LLM 失败→启发式兜底）        │
        └───────────────────────────────────────────────┘
                    │                        │
                    ▼                        ▼
        ┌────────────────────┐   ┌────────────────────────┐
        │ store.py (SQLite)  │   │ zotero.py / mailer.py   │
        │ 论文目录/推荐记录   │   │ 一键入库 / 每日邮件     │
        │ 运行日志/已推送去重 │   │ render.py 负责渲染      │
        └────────────────────┘   └────────────────────────┘
```

## 模块职责

| 模块 | 职责 | 关键点 |
|---|---|---|
| `config.py` | 三层配置（example ← config.json ← secrets.json/环境变量） | `save_secrets()` 保证密钥只进 `secrets.json`；`as_dict(redact=True)` 给 UI 用 |
| `models.py` | `Paper` / `Topic` / `Recommendation` | `fingerprint()` 决定跨库去重口径；`Paper.merge()` 保留"自身优先、缺失才补" |
| `net.py` | `Fetcher` | 重试、指数退避、**按域名限速**（Google Scholar 的命门）、`host_timeout` / `host_retries` 按域名微调、可选代理 |
| `sources/` | 数据源插件 | 自动发现；每个实现只需 `search()`；解析函数与网络调用分离，便于离线测试 |
| `rank.py` | 去重 / 过滤 / 打分 | 权重全在 `search.weights`；分区表用**词边界**正则匹配，避免 `TC` 命中 `DCTCP` 这类误伤 |
| `summarize.py` | LLM 抽象 | 三种 style：`openai`（任意兼容接口）/ `ollama` / `heuristic`；厂商表在配置里，主备自动降级 |
| `zotero.py` | Zotero Web API | 免填 userID（用 key 换）、DOI/标题查重、批量 50 条提交、`paper_to_zotero_item()` 是纯函数 |
| `mailer.py` | 邮件通道插件 | `smtp` / `file`（落 `.eml`），HTML + 纯文本双版本 |
| `render.py` | HTML 渲染 | 内联样式 + table 布局（邮件客户端兼容），同时输出 `render_text()` |
| `engine.py` | 编排 | `collect()` 只检索不花钱；`summarize_papers()` 单独可调 —— 每日简报因此能"先过滤再摘要" |
| `digest.py` | 每日简报 | 只推"该方向从未推荐过"的论文；报告落盘 + 发信 + 运行日志 |
| `scheduler.py` | 定时器 | 每 20 秒比对 `HH:MM` + 当天是否已跑过，改配置立即生效、休眠错过会补跑 |
| `jobs.py` | 后台任务 | 检索这类几十秒到几分钟的操作异步跑，UI 轮询日志 |
| `server.py` | 控制台 API | 只监听 127.0.0.1，可选 `app.token`；静态页面直接读 `web/` |

## 为什么这样设计

**1. `collect()` 与 `summarize_papers()` 分离。**
LLM 调用是整条链路里最贵、最慢的一环。每日简报的正确顺序是"先按规则筛掉 95% 的候选，再为剩下的 5 篇写卡片"，而不是"先给 50 篇写卡片再挑 5 篇"。分离之后，这个优化只改调用方的顺序，不用动检索逻辑。

**2. 数据源必须能单独失败。**
Google Scholar 会拦人机校验、arXiv 会 429、dblp 会弹反爬页 —— 这是常态而非异常。所以每个插件在自己的线程里跑，异常被收敛成 `errors = {源: 原因}` 返回给 UI，绝不让一个源拖垮整次检索。

**3. 解析函数与网络分离。**
`sources/*.py` 里每个站点都有一个纯函数（`parse_scholar_html` / `parse_arxiv_feed` / `parse_arxiv_rss` / `item_to_paper` / `hit_to_paper`）。离线单元测试直接喂固定 HTML/XML，不需要网络，也不会因为对方改版而"测试通过但线上全挂"。

**4. 指纹 + 标题两级去重。**
只用 DOI 去重：Google Scholar 的条目没有 DOI，会与 OpenAlex 的同名条目重复出现。
只用标题去重：会把同名不同文的综述误合并。
所以先按 DOI/arXiv 指纹分组，再用**归一化标题完全相同**做第二轮合并。

**5. 密钥与配置物理分离。**
`config.json` 可以随便分享/同步，`secrets.json` 单独存在且被 `.gitignore` 覆盖。UI 保存设置时按字段分流，避免"改一次模型名顺手把 key 提交上 GitHub"这种事。

## 数据表

| 表 | 用途 | 关键字段 |
|---|---|---|
| `topics` | 研究方向 | `direction` / `seeds` / `keywords` / `queries` / `filters` / `enabled` |
| `papers` | 论文目录（全局去重后） | `key`（指纹）主键、`first_seen` / `last_seen` |
| `recommendations` | 某方向推荐过哪些论文 | `(topic_id, paper_key)` 主键、`status`（shown/sent/accepted）、三列卡片文案 |
| `runs` | 每次运行的审计日志 | `kind` / `status` / `stats` / `report_path` / `error` |

"每天只推新论文"就是靠 `recommendations` 里是否已存在 `(topic_id, paper_key)` 判断的 —— 因此手动检索看过的论文，第二天不会再推给你。
