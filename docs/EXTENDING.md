# 扩展指南

这份文档的三个目标：**加数据源**、**换 LLM**、**换邮件通道**，另外附上调参技巧。

---

## 一、新增一个数据源（约 40 行）

在 `paper_radar/sources/` 下新建文件即可，不需要改任何其它代码 —— `sources/__init__.py` 会用 `pkgutil` 扫描本目录，把所有 `Source` 子类自动注册。

以假设的 "DBLP 镜像站" 为例：

```python
# paper_radar/sources/my_source.py
from __future__ import annotations
from ..models import Paper
from .base import Source, year_in_range


def parse_my_api(payload: dict) -> list[Paper]:
    """把响应转成 Paper。**纯函数**，便于离线测试。"""
    papers = []
    for item in payload.get("results") or []:
        papers.append(
            Paper(
                title=(item.get("title") or "").strip(),
                authors=item.get("authors") or [],
                year=item.get("year"),
                venue=item.get("venue") or "",
                doi=item.get("doi") or "",
                url=item.get("url") or "",
                abstract=item.get("abstract") or "",
                citations=item.get("cited_by"),
                item_type="conferencePaper",
                source="my_source",       # 必须与 name 一致
                sources=["my_source"],
            )
        )
    return papers


class MySource(Source):
    name = "my_source"                    # 配置里的键名：sources.my_source
    label = "My Source"                   # UI 上显示的名字
    homepage = "https://example.org"
    kinds = ("conference",)

    def search(self, query, *, limit=None, year_from=None, year_to=None) -> list[Paper]:
        limit = limit or self.limit
        section = self.section or {}      # 自动读取 config 的 sources.my_source
        data = self.http.get_json(
            section.get("endpoint", "https://example.org/api/search"),
            params={"q": query, "rows": limit},
        )
        return [p for p in parse_my_api(data) if year_in_range(p.year, year_from, year_to)][:limit]
```

然后在 `config.json` 里：

```json
{
  "sources": {
    "enabled": ["arxiv", "google_scholar", "openalex", "crossref", "my_source"],
    "my_source": { "endpoint": "https://example.org/api/search" }
  }
}
```

**约定与注意事项**

| 事项 | 说明 |
|---|---|
| `name` | 必须唯一，且与配置键、`Paper.source` 一致 |
| `self.limit` | 基类已从 `sources.per_source_limit` 读好 |
| `self.section` | 基类已从 `sources.<name>` 读好，插件自己的参数放这里 |
| 限速 | 不要自己 `sleep`，把域名写进 `sources.http.min_interval` 即可，`Fetcher` 会串行化 |
| 失败处理 | 直接抛异常即可，编排层会捕获并记进 `errors`，只影响这个源 |
| 需要 key | 设 `needs_key = True`，并在 `is_configured()` 里判断；密钥请放 `secrets.json`（可加进 `config.SECRET_PATHS` 以便自动脱敏） |
| 测试 | 加一个用固定响应喂 `parse_my_api()` 的用例到 `tests/test_core.py` |

**新增数据源到默认启用列表？** 请先确认它在"无 key、常见网络"下真的能用 —— 否则用户第一次跑就满屏失败。不稳定但有用的源，建议写进 README 的"数据源可用性"表并默认关闭（dblp / Semantic Scholar 就是这么处理的）。

---

## 二、切换或新增 LLM

### 换成任意 OpenAI 兼容服务（不用改代码）

```json
{
  "llm": {
    "provider": "moonshot",
    "base_url": "https://api.moonshot.cn/v1",
    "model": "moonshot-v1-8k"
  },
  "llm.providers": {
    "moonshot": { "style": "openai", "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k" }
  }
}
```

内置了 `deepseek` / `openai` / `moonshot` / `zhipu` / `ollama` / `none` 六个预设；**顶层 `llm.base_url` / `llm.model` 只作用于 `llm.provider` 指定的那一家**，不会污染 fallback。

### 用本地模型（零成本、可离线）

```json
{ "llm": { "provider": "ollama", "fallback_provider": "none",
           "providers": { "ollama": { "style": "ollama", "base_url": "http://127.0.0.1:11434", "model": "qwen2.5:7b" } } } }
```

### 完全不用 LLM

把 `llm.provider` 设为 `"none"`，或把 `llm.enabled_for.profile` / `.summary` 设为 `false`。
此时走 `summarize.heuristic_profile()` 和 `_heuristic_card()`：词频抽关键词 + 摘要前两句当概要 + 关键词命中当理由。能用，但文笔机械。

### 新增一种调用风格（例如 Anthropic Messages API）

在 `summarize.py` 里加一个类并注册到 `BACKEND_STYLES`：

```python
class AnthropicBackend(Backend):
    style = "anthropic"

    def chat(self, system: str, user: str) -> str:
        status, data = self.http.post_json(
            f"{self.spec['base_url'].rstrip('/')}/v1/messages",
            {"model": self.spec["model"], "max_tokens": 2048, "system": system,
             "messages": [{"role": "user", "content": user}]},
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
        )
        if status >= 400:
            raise RuntimeError(str(data)[:300])
        return "".join(block.get("text", "") for block in data.get("content", []))


BACKEND_STYLES["anthropic"] = AnthropicBackend
```

配置里把该 provider 的 `"style"` 写成 `"anthropic"` 即可。

### 改提示词

`config.json` 的 `prompts.*` 就是完整模板，改完刷新（UI 保存设置后会热重载）。注意：模板用 `str.format()` 渲染，**字面量大括号要写成 `{{` `}}`**（看看 `config.example.json` 里的 JSON 示例即可）。

可用的占位符：

| 模板 | 占位符 |
|---|---|
| `profile_system` | 无 |
| `profile_user` | `{direction}` `{seeds}` `{max_queries}` |
| `summary_system` | 无 |
| `summary_user` | `{direction}` `{keywords}` `{count}` `{papers}` |
| `relevance_user` | `{direction}` `{keywords}` `{papers}` |

---

## 三、新增邮件通道

```python
# paper_radar/mailer.py 里追加
class WebhookTransport(Transport):
    """把简报推到企业微信 / 飞书 / Slack 机器人。"""

    style = "webhook"

    def send(self, message: EmailMessage) -> dict:
        html = message.get_body(preferencelist=("html",)).get_content()
        url = (self.section.get("webhook") or {}).get("url", "")
        if not url:
            return {"ok": False, "transport": "webhook", "error": "未配置 mail.webhook.url"}
        status, data = self.http.post_json(url, {"msgtype": "markdown", "markdown": {"content": html[:4000]}})
        return {"ok": status < 400, "transport": "webhook", "error": "" if status < 400 else str(data)[:200]}


TRANSPORTS["webhook"] = WebhookTransport
```

配置里 `"mail": { "transport": "webhook" }`。

现成的 `file` 通道值得一用：它不发信，把邮件写成 `data/outbox/*.eml`，可以用 Outlook/Foxmail 打开检查排版，也适合放进自动化测试。

---

## 四、调参技巧（不改代码就能显著改效果）

| 想要的效果 | 怎么调 |
|---|---|
| 结果太泛、想更聚焦 | 提高 `search.weights.relevance`，降低 `recency` / `citations`；或改成 `search.require_title_keyword: true` |
| 想更看重顶会 | 提高 `search.weights.venue`，并把 `search.tier_score` 的 S/A 差距拉大 |
| 想换成 CCF 分区 | 直接替换 `search.venue_tiers` 整张表（键是等级名，值是会议/期刊名的**词**，按词边界匹配） |
| 时序噪声太多 | 用 `profile.stopwords` 追加你领域里的高频水词（如 `system`、`performance`） |
| 检索式不够准 | 不依赖 AI，直接在 UI 的「检索式」框里手写，例如 `ti:"buffer management" AND cat:cs.NI`（arXiv 语法）或 `"dynamic threshold" switch -optical` |
| 某类论文总混进来 | 在方向的「排除词」里填（会扣相关性分），或在 `filters.exclude_venues` 里填期刊名片段 |
| Google Scholar 老是不稳 | 把 `sources.http.min_interval.scholar.google.com` 调到 8~10 秒，并减少检索式条数 |
| 每日简报太多/太少 | `digest.top_n`、`digest.lookback_days`、`search.year_lookback` 三个一起调 |
