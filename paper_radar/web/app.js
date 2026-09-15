/* Paper Radar 控制台 —— 纯原生 JS，无构建步骤。 */

const $ = (id) => document.getElementById(id);
const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const WEEKDAY_LABEL = { mon: "周一", tue: "周二", wed: "周三", thu: "周四", fri: "周五", sat: "周六", sun: "周日" };

const S = {
  state: null,
  topics: [],
  currentTopicId: null,
  results: [],
  enabledSources: new Set(),
  pickedSources: new Set(),
  digestDays: new Set(),
};

/* ------------------------------------------------------------------ */
/* 基础                                                                */
/* ------------------------------------------------------------------ */
async function api(path, options = {}) {
  const opts = Object.assign({ headers: { "Content-Type": "application/json" } }, options);
  if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
  const resp = await fetch(path, opts);
  const text = await resp.text();
  let data;
  try { data = JSON.parse(text); } catch (e) { data = { ok: false, error: text.slice(0, 400) }; }
  if (!resp.ok && !data.error) data.error = "HTTP " + resp.status;
  return data;
}

function showStatus(el, message, kind = "ok") {
  const node = typeof el === "string" ? $(el) : el;
  if (!node) return;
  node.className = "status show " + kind;
  node.textContent = message;
}
function hideStatus(el) { const n = typeof el === "string" ? $(el) : el; if (n) n.className = "status"; }

function showLog(el, text) {
  const node = typeof el === "string" ? $(el) : el;
  if (!node) return;
  node.style.display = "block";
  node.textContent = text;
  node.scrollTop = node.scrollHeight;
}

function esc(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* 轮询后台任务，实时把日志打到 <pre> 上 */
function pollJob(jobId, logEl, onDone) {
  let stopped = false;
  const tick = async () => {
    if (stopped) return;
    const data = await api("/api/jobs/" + jobId);
    if (!data.ok) { showLog(logEl, "任务查询失败：" + data.error); return; }
    const job = data.job;
    showLog(logEl, job.logs.join("\n"));
    if (job.status === "ok" || job.status === "error") {
      stopped = true;
      onDone(job);
      return;
    }
    setTimeout(tick, 1200);
  };
  tick();
}

/* ------------------------------------------------------------------ */
/* Tab 切换                                                            */
/* ------------------------------------------------------------------ */
document.querySelectorAll("nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "settings") loadSettings();
  });
});

/* ------------------------------------------------------------------ */
/* 全局状态刷新                                                        */
/* ------------------------------------------------------------------ */
async function refresh() {
  const data = await api("/api/state");
  if (!data.ok) { console.error(data.error); return; }
  S.state = data;
  S.topics = data.topics || [];
  S.enabledSources = new Set((data.sources || []).filter((s) => s.enabled).map((s) => s.name));

  renderTopicList();
  renderTopicSelect();
  renderSourceChips($("r-sources"), S.enabledSources, (set) => { /* 检索页临时选择 */ });
  renderDigestPanel(data);
  renderZoteroInfo(data);
  $("search-hint").textContent =
    `相关度权重 ${data.config_masked?.search?.weights?.relevance ?? "-"} · 回溯 ${data.search?.year_lookback ?? "-"} 年 · 默认取 ${data.search?.top_n ?? "-"} 篇`;
}

/* ------------------------------------------------------------------ */
/* 研究方向                                                            */
/* ------------------------------------------------------------------ */
function renderTopicList() {
  const box = $("topic-list");
  if (!S.topics.length) {
    box.innerHTML = '<p class="muted">还没有研究方向。</p>';
    return;
  }
  box.innerHTML = S.topics
    .map((t) => `
      <div class="topic-item ${t.id === S.currentTopicId ? "active" : ""}" data-id="${t.id}">
        <div class="name">${esc(t.name)} ${t.enabled ? "" : '<span class="pill off">已暂停</span>'}</div>
        <div class="meta">${t.keywords.length} 关键词 · ${t.queries.length} 检索式 · ${t.seeds.length} 种子论文</div>
      </div>`)
    .join("");
  box.querySelectorAll(".topic-item").forEach((node) => {
    node.addEventListener("click", () => selectTopic(Number(node.dataset.id)));
  });
}

function selectTopic(id) {
  S.currentTopicId = id;
  const t = S.topics.find((x) => x.id === id);
  if (!t) return;
  $("topic-form-title").textContent = "编辑方向：" + t.name;
  $("t-name").value = t.name || "";
  $("t-direction").value = t.direction || "";
  $("t-seeds").value = (t.seeds || []).map((s) => s.value || s.title || s.doi || s.arxiv_id || "").filter(Boolean).join("\n");
  $("t-keywords").value = (t.keywords || []).join(", ");
  $("t-negative").value = (t.negative_keywords || []).join(", ");
  $("t-queries").value = (t.queries || []).join("\n");
  $("t-collection").value = t.zotero_collection || "";
  $("t-enabled").checked = !!t.enabled;
  const sum = $("t-summary");
  if (t.profile_summary) { sum.className = "status show info"; sum.textContent = "AI 归纳：" + t.profile_summary; }
  else sum.className = "status";
  renderTopicList();
  $("r-topic").value = String(id);
  hideStatus("topic-status");
}

function newTopic() {
  S.currentTopicId = null;
  $("topic-form-title").textContent = "新建研究方向";
  ["t-name", "t-direction", "t-seeds", "t-keywords", "t-negative", "t-queries", "t-collection"].forEach((id) => ($(id).value = ""));
  $("t-enabled").checked = true;
  $("t-summary").className = "status";
  renderTopicList();
  hideStatus("topic-status");
}

function collectTopicForm() {
  return {
    id: S.currentTopicId,
    name: $("t-name").value.trim(),
    direction: $("t-direction").value.trim(),
    seeds: $("t-seeds").value.split("\n").map((s) => s.trim()).filter(Boolean),
    keywords: $("t-keywords").value.split(",").map((s) => s.trim()).filter(Boolean),
    negative_keywords: $("t-negative").value.split(",").map((s) => s.trim()).filter(Boolean),
    queries: $("t-queries").value.split("\n").map((s) => s.trim()).filter(Boolean),
    zotero_collection: $("t-collection").value.trim(),
    enabled: $("t-enabled").checked,
  };
}

async function saveTopic(silent = false) {
  const payload = collectTopicForm();
  if (!payload.name) { showStatus("topic-status", "请先填方向名称。", "err"); return null; }
  const data = await api("/api/topics", { method: "POST", body: payload });
  if (!data.ok) { showStatus("topic-status", "保存失败：" + data.error, "err"); return null; }
  S.currentTopicId = data.topic.id;
  await refresh();
  selectTopic(data.topic.id);
  if (!silent) showStatus("topic-status", "已保存。", "ok");
  return data.topic;
}

async function buildProfile() {
  const saved = await saveTopic(true);
  if (!saved) return;
  $("build-log").style.display = "block";
  showStatus("topic-status", "正在生成检索方案…", "info");
  const data = await api(`/api/topics/${saved.id}/build`, { method: "POST", body: {} });
  if (!data.ok) { showStatus("topic-status", "失败：" + data.error, "err"); return; }
  pollJob(data.job.id, "build-log", async (job) => {
    if (job.status === "error") { showStatus("topic-status", "生成失败：" + job.error, "err"); return; }
    await refresh();
    selectTopic(saved.id);
    showStatus("topic-status", "检索方案已生成，可以直接去「检索推荐」了。", "ok");
  });
}

async function deleteTopic() {
  if (!S.currentTopicId) return;
  if (!confirm("确定删除这个方向及其推荐记录？（Zotero 里已入库的文献不受影响）")) return;
  await api("/api/topics/" + S.currentTopicId, { method: "DELETE" });
  S.currentTopicId = null;
  newTopic();
  await refresh();
}

/* ------------------------------------------------------------------ */
/* 数据源 chips                                                        */
/* ------------------------------------------------------------------ */
function renderSourceChips(container, activeSet, onChange) {
  const all = (S.state && S.state.sources) || [];
  container.innerHTML = all
    .map((s) => `<span class="chip ${activeSet.has(s.name) ? "on" : ""}" data-name="${s.name}" title="${esc(s.homepage || "")}">${esc(s.label)}</span>`)
    .join("");
  container.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const name = chip.dataset.name;
      if (activeSet.has(name)) activeSet.delete(name); else activeSet.add(name);
      chip.classList.toggle("on");
      if (onChange) onChange(activeSet);
    });
  });
}

/* ------------------------------------------------------------------ */
/* 检索推荐                                                            */
/* ------------------------------------------------------------------ */
function renderTopicSelect() {
  const sel = $("r-topic");
  const keep = S.currentTopicId;
  sel.innerHTML = S.topics.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
  if (keep) sel.value = String(keep);
}

async function runSearch(rebuild = false) {
  const topicId = Number($("r-topic").value);
  if (!topicId) { showStatus("search-status", "先去「研究方向」建一个方向。", "err"); return; }
  const sources = Array.from(S.enabledSources);
  $("search-log").style.display = "block";
  showStatus("search-status", "检索中…（多源并发 + 生成卡片，通常 30 秒到 3 分钟）", "info");
  const data = await api("/api/search", {
    method: "POST",
    body: {
      topic_id: topicId,
      limit: Number($("r-limit").value) || 15,
      sources,
      summarize: $("r-summarize").checked,
      rebuild,
    },
  });
  if (!data.ok) { showStatus("search-status", "失败：" + data.error, "err"); return; }
  pollJob(data.job.id, "search-log", (job) => {
    if (job.status === "error") { showStatus("search-status", "检索失败：" + job.error, "err"); return; }
    const res = job.result || {};
    S.results = res.papers || [];
    renderResults();
    const errs = Object.entries(res.errors || {});
    let msg = `共候选 ${res.candidates || 0} 篇，去重排序后给出 ${S.results.length} 篇推荐。`;
    if (errs.length) msg += " 失败的数据源：" + errs.map(([k, v]) => `${k}(${v})`).join("；");
    showStatus("search-status", msg, errs.length ? "info" : "ok");
    refresh();
  });
}

function renderResults() {
  const body = $("results-body");
  if (!S.results.length) {
    body.innerHTML = '<tr><td colspan="7" class="muted">没有结果。换个关键词或降低年份限制试试。</td></tr>';
    return;
  }
  body.innerHTML = S.results
    .map((p, idx) => {
      const link = p.url || (p.doi ? "https://doi.org/" + p.doi : (p.arxiv_id ? "https://arxiv.org/abs/" + p.arxiv_id : ""));
      const title = link ? `<a href="${esc(link)}" target="_blank" rel="noreferrer">${esc(p.title)}</a>` : esc(p.title);
      const meta = [
        p.year || "年份未知",
        p.citations != null ? "被引 " + p.citations : null,
        (p.sources || []).slice(0, 3).join("/"),
      ].filter(Boolean).join(" · ");
      const tier = p.extra && p.extra.venue_tier && p.extra.venue_tier !== "unknown" ? ` <span class="pill">${esc(p.extra.venue_tier)}</span>` : "";
      const hl = (p.highlights || []).map((h) => `<li>${esc(h)}</li>`).join("");
      return `<tr>
        <td><input type="checkbox" class="pick" data-key="${esc(p.key)}" style="width:auto"></td>
        <td class="title">${title}<div class="mini">${esc(meta)}</div></td>
        <td>${esc(p.venue || "未标注")}${tier}</td>
        <td>${esc(p.summary || "—")}</td>
        <td><ul class="hl">${hl || "<li>—</li>"}</ul></td>
        <td>${esc(p.reason || "—")}</td>
        <td class="score">${Math.round((p.score || 0) * 100)}</td>
      </tr>`;
    })
    .join("");
  body.querySelectorAll(".pick").forEach((cb) => cb.addEventListener("change", updateSelCount));
  updateSelCount();
}

function updateSelCount() {
  const n = document.querySelectorAll("#results-body .pick:checked").length;
  $("sel-count").textContent = `已选 ${n} 篇`;
}

function pickedKeys() {
  return Array.from(document.querySelectorAll("#results-body .pick:checked")).map((cb) => cb.dataset.key);
}

async function addToZotero() {
  const keys = pickedKeys();
  if (!keys.length) { showStatus("zotero-status", "先勾选要入库的论文。", "err"); return; }
  showStatus("zotero-status", `正在写入 Zotero（${keys.length} 篇）…`, "info");
  const data = await api("/api/zotero/add", {
    method: "POST",
    body: {
      keys,
      collection: $("z-collection").value,
      tags: $("z-tags").value.split(",").map((s) => s.trim()).filter(Boolean),
      topic_id: Number($("r-topic").value) || null,
    },
  });
  if (!data.ok) { showStatus("zotero-status", "失败：" + data.error, "err"); return; }
  const r = data.result || {};
  const parts = [`成功 ${r.added || 0} 篇`];
  if (r.duplicates) parts.push(`已存在跳过 ${r.duplicates} 篇`);
  if (r.failed) parts.push(`失败 ${r.failed} 篇`);
  if (r.error) parts.push("错误：" + r.error);
  showStatus("zotero-status", parts.join(" · "), r.error || r.failed ? "info" : "ok");
}

/* ------------------------------------------------------------------ */
/* 每日简报                                                            */
/* ------------------------------------------------------------------ */
function renderDigestPanel(data) {
  const d = data.digest || {};
  const sched = data.scheduler || {};
  $("sched-info").innerHTML = `
    <div class="k">定时状态</div><div>${sched.enabled ? '<span class="pill ok">已启用</span>' : '<span class="pill off">已暂停</span>'}</div>
    <div class="k">当前设定</div><div>${esc(d.time || "08:30")} · ${(d.days || []).map((x) => WEEKDAY_LABEL[x] || x).join(" ")} · 每次 ${d.top_n} 篇</div>
    <div class="k">下次运行</div><div>${esc(sched.next_run || "—")}</div>
    <div class="k">最近触发</div><div>${esc(sched.last_fired_date || "尚未触发")}</div>
    <div class="k">邮件通道</div><div>${data.mail?.enabled ? '<span class="pill ok">' + esc(data.mail.transport) + '</span> ' + esc((data.mail.to || []).join(", ")) : '<span class="pill off">未启用</span>'}</div>`;

  $("d-enabled").checked = d.enabled !== false;
  $("d-time").value = d.time || "08:30";
  $("d-topn").value = d.top_n || 5;
  $("d-lookback").value = d.lookback_days || 45;
  $("d-to").value = (data.mail?.to || []).join(", ");
  S.digestDays = new Set(d.days || WEEKDAYS);
  renderDayChips();

  const reports = data.config_masked ? null : null; // 报告列表单独拉
  api("/api/reports").then((r) => {
    if (!r.ok || !r.reports.length) { $("report-list").innerHTML = '<span class="muted">暂无报告。</span>'; return; }
    $("report-list").innerHTML = r.reports
      .map((x) => `<div><a href="${esc(x.url)}" target="_blank" rel="noreferrer">${esc(x.name)}</a></div>`)
      .join("");
  });
  const runs = data.runs || [];
  $("run-list").innerHTML = runs.length
    ? runs.map((r) => `<div>${esc(r.started_at)} · ${esc(r.kind)} · <b>${esc(r.status)}</b> ${r.error ? "· " + esc(r.error.slice(0, 120)) : ""}</div>`).join("")
    : '<span class="muted">暂无记录。</span>';
}

function renderDayChips() {
  $("d-days").innerHTML = WEEKDAYS
    .map((d) => `<span class="chip ${S.digestDays.has(d) ? "on" : ""}" data-day="${d}">${WEEKDAY_LABEL[d]}</span>`)
    .join("");
  $("d-days").querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const day = chip.dataset.day;
      if (S.digestDays.has(day)) S.digestDays.delete(day); else S.digestDays.add(day);
      chip.classList.toggle("on");
    });
  });
}

async function saveDigestSettings() {
  const patch = {
    digest: {
      enabled: $("d-enabled").checked,
      time: $("d-time").value || "08:30",
      top_n: Number($("d-topn").value) || 5,
      lookback_days: Number($("d-lookback").value) || 45,
      days: WEEKDAYS.filter((d) => S.digestDays.has(d)),
    },
    mail: { to: $("d-to").value.split(",").map((s) => s.trim()).filter(Boolean) },
  };
  const data = await api("/api/settings", { method: "POST", body: { patch } });
  if (!data.ok) { showStatus("digest-status", "保存失败：" + data.error, "err"); return; }
  showStatus("digest-status", "定时设置已保存，立即生效（无需重启）。", "ok");
  refresh();
}

async function runDigestNow(send) {
  if (send && !confirm("确认现在就发一封真实邮件？")) return;
  $("digest-log").style.display = "block";
  showStatus("digest-status", send ? "正在试跑并发送邮件…" : "正在试跑（只生成报告，不发信）…", "info");
  const data = await api("/api/digest/run", { method: "POST", body: { send } });
  if (!data.ok) { showStatus("digest-status", "失败：" + data.error, "err"); return; }
  pollJob(data.job.id, "digest-log", (job) => {
    if (job.status === "error") { showStatus("digest-status", "运行失败：" + job.error, "err"); return; }
    const r = job.result || {};
    const mails = (r.mail || []).map((m) => (m.ok ? "邮件已发送" : "邮件失败：" + (m.error || ""))).join("；");
    showStatus("digest-status", `本次新增 ${r.total_new || 0} 篇。${mails || "未发信。"}`, "ok");
    refresh();
  });
}

async function sendTestMail() {
  showStatus("digest-status", "正在发送测试邮件…", "info");
  const data = await api("/api/settings", { method: "POST", body: { patch: { mail: { dry_run: false } } } });
  if (!data.ok) { showStatus("digest-status", "失败：" + data.error, "err"); return; }
  const run = await api("/api/digest/run", { method: "POST", body: { send: true, topic_ids: [] } });
  if (!run.ok) { showStatus("digest-status", "失败：" + run.error, "err"); return; }
  showStatus("digest-status", "已触发一次真实发送（若当前没有新论文则不会发信）。", "info");
}

/* ------------------------------------------------------------------ */
/* 设置                                                                */
/* ------------------------------------------------------------------ */
function renderZoteroInfo(data) {
  const z = data.zotero || {};
  $("zotero-info").innerHTML = `
    <div class="k">状态</div><div>${z.has_key ? '<span class="pill ok">已配置 key</span>' : '<span class="pill off">缺少 key</span>'}</div>
    <div class="k">user_id</div><div>${esc(z.user_id || "（自动解析）")}</div>
    <div class="k">API</div><div>${esc(z.api_base || "")}</div>`;
}

async function loadCollections() {
  const data = await api("/api/zotero/collections");
  const sel = $("z-collection");
  if (!data.ok) {
    sel.innerHTML = '<option value="">（读取分类失败）</option>';
    showStatus("settings-status", "读取 Zotero 分类失败：" + data.error, "err");
    return;
  }
  sel.innerHTML = '<option value="">（Zotero 默认分类）</option>' +
    (data.collections || []).map((c) => `<option value="${esc(c.key)}">${esc(c.name)} (${c.count})</option>`).join("");
  showStatus("settings-status", `已拉取 ${(data.collections || []).length} 个分类。`, "ok");
}

function loadSettings() {
  if (!S.state) return;
  const c = S.state.config_masked || {};
  const llm = c.llm || {};
  const providers = Object.keys(llm.providers || {});
  const opts = providers.map((p) => `<option value="${p}">${p}</option>`).join("");
  $("s-llm-provider").innerHTML = opts;
  $("s-llm-fallback").innerHTML = '<option value="">（不使用）</option>' + opts;
  $("s-llm-provider").value = llm.provider || "";
  $("s-llm-fallback").value = llm.fallback_provider || "";
  $("s-llm-base").value = llm.base_url || "";
  $("s-llm-model").value = llm.model || "";
  $("s-llm-profile").checked = !!llm.enabled_for?.profile;
  $("s-llm-summary").checked = !!llm.enabled_for?.summary;

  const z = c.zotero || {};
  $("s-zotero-user").value = z.user_id || "";
  $("s-zotero-tags").value = (z.default_tags || []).join(", ");

  const m = c.mail || {};
  const smtp = m.smtp || {};
  $("s-mail-enabled").checked = !!m.enabled;
  $("s-mail-host").value = smtp.host || "";
  $("s-mail-port").value = smtp.port || 465;
  $("s-mail-sec").value = smtp.security || "ssl";
  $("s-mail-user").value = smtp.username || "";
  $("s-mail-to").value = (m.to || []).join(", ");

  const src = c.sources || {};
  $("s-per-source").value = src.per_source_limit || 20;
  renderSourceChips($("s-sources"), new Set(src.enabled || []), null);

  const w = (c.search || {}).weights || {};
  $("s-w-rel").value = w.relevance ?? 1;
  $("s-w-rec").value = w.recency ?? 0.35;
  $("s-w-cit").value = w.citations ?? 0.22;
  $("s-w-ven").value = w.venue ?? 0.18;
  $("s-year-lookback").value = (c.search || {}).year_lookback ?? 5;
}

async function saveSettings() {
  const enabledSources = Array.from($("s-sources").querySelectorAll(".chip.on")).map((c) => c.dataset.name);
  const patch = {
    llm: {
      provider: $("s-llm-provider").value,
      fallback_provider: $("s-llm-fallback").value,
      base_url: $("s-llm-base").value.trim(),
      model: $("s-llm-model").value.trim(),
      enabled_for: { profile: $("s-llm-profile").checked, summary: $("s-llm-summary").checked },
    },
    zotero: {
      user_id: $("s-zotero-user").value.trim(),
      default_tags: $("s-zotero-tags").value.split(",").map((s) => s.trim()).filter(Boolean),
    },
    mail: {
      enabled: $("s-mail-enabled").checked,
      smtp: {
        host: $("s-mail-host").value.trim(),
        port: Number($("s-mail-port").value) || 465,
        security: $("s-mail-sec").value,
        username: $("s-mail-user").value.trim(),
      },
      to: $("s-mail-to").value.split(",").map((s) => s.trim()).filter(Boolean),
    },
    sources: { enabled: enabledSources, per_source_limit: Number($("s-per-source").value) || 20 },
    search: {
      weights: {
        relevance: Number($("s-w-rel").value),
        recency: Number($("s-w-rec").value),
        citations: Number($("s-w-cit").value),
        venue: Number($("s-w-ven").value),
      },
      year_lookback: Number($("s-year-lookback").value) || 5,
    },
  };
  const secrets = {};
  if ($("s-llm-key").value.trim()) secrets.llm = { api_key: $("s-llm-key").value.trim() };
  if ($("s-zotero-key").value.trim()) secrets.zotero = { api_key: $("s-zotero-key").value.trim() };
  if ($("s-mail-pass").value) secrets.mail = { smtp: { password: $("s-mail-pass").value } };

  const data = await api("/api/settings", { method: "POST", body: { patch, secrets } });
  if (!data.ok) { showStatus("settings-status", "保存失败：" + data.error, "err"); return; }
  $("s-llm-key").value = "";
  $("s-zotero-key").value = "";
  $("s-mail-pass").value = "";
  showStatus("settings-status", "设置已保存并立即生效。", "ok");
  refresh();
  loadSettings();
}

async function testSources() {
  $("settings-log").style.display = "block";
  showStatus("settings-status", "正在逐个测试数据源…", "info");
  const data = await api("/api/sources/test", { method: "POST", body: {} });
  if (!data.ok) { showStatus("settings-status", "失败：" + data.error, "err"); return; }
  pollJob(data.job.id, "settings-log", (job) => {
    if (job.status === "error") { showStatus("settings-status", "测试失败：" + job.error, "err"); return; }
    const lines = Object.entries(job.result || {}).map(([name, r]) =>
      r.ok ? `✅ ${name}: ${r.count} 条 · ${(r.sample || []).join(" / ")}` : `❌ ${name}: ${r.error}`);
    showLog("settings-log", lines.join("\n"));
    showStatus("settings-status", "测试完成，详见下方日志。", "ok");
  });
}

/* ------------------------------------------------------------------ */
/* 事件绑定                                                            */
/* ------------------------------------------------------------------ */
$("btn-new").addEventListener("click", newTopic);
$("btn-save").addEventListener("click", () => saveTopic(false));
$("btn-build").addEventListener("click", buildProfile);
$("btn-delete").addEventListener("click", deleteTopic);
$("btn-save-search").addEventListener("click", async () => {
  const t = await saveTopic(false);
  if (t) { document.querySelector('nav button[data-tab="results"]').click(); runSearch(false); }
});
$("btn-search").addEventListener("click", () => runSearch(false));
$("btn-rebuild").addEventListener("click", () => runSearch(true));
$("btn-all").addEventListener("click", () => { document.querySelectorAll("#results-body .pick").forEach((cb) => (cb.checked = true)); updateSelCount(); });
$("btn-none").addEventListener("click", () => { document.querySelectorAll("#results-body .pick").forEach((cb) => (cb.checked = false)); updateSelCount(); });
$("check-all").addEventListener("change", (e) => {
  document.querySelectorAll("#results-body .pick").forEach((cb) => (cb.checked = e.target.checked));
  updateSelCount();
});
$("btn-add-zotero").addEventListener("click", addToZotero);
$("r-topic").addEventListener("change", (e) => selectTopic(Number(e.target.value)));
$("btn-save-digest").addEventListener("click", saveDigestSettings);
$("btn-digest-preview").addEventListener("click", () => runDigestNow(false));
$("btn-digest-send").addEventListener("click", () => runDigestNow(true));
$("btn-mail-test").addEventListener("click", sendTestMail);
$("btn-save-settings").addEventListener("click", saveSettings);
$("btn-test-sources").addEventListener("click", testSources);
$("btn-load-collections").addEventListener("click", loadCollections);

/* ------------------------------------------------------------------ */
/* 启动                                                                */
/* ------------------------------------------------------------------ */
(async function init() {
  await refresh();
  loadCollections().catch(() => {});
  if (S.topics.length) selectTopic(S.topics[0].id);
  setInterval(async () => {
    const d = await api("/api/state");
    if (d.ok) { S.state = d; renderDigestPanel(d); }
  }, 30000);
})();
