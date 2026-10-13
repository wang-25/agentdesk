/* ============================================================
   AgentDesk 可观测看板（/dashboard）的页面逻辑
   ------------------------------------------------------------
   【这一版相比旧页修掉了什么】
   1. **空图表**：条形图与时间线只走 AD.bars / AD.timeline，
      它们生成的类名配的是 `.bar .fill{display:block}` / `.tl .fl{display:block}`。
      旧实现是行内 <span> 配 height:100% —— 行内元素的高度不生效，
      于是容器画出来了、数据条一条都看不见。本文件**不自己拼条形图**，
      也就不可能再踩一次。
   2. **口径桥接**：渲染完概览后，把 `/metrics/summary` 的 scope 写成一行
      「完整台账 N 条 = 扫描 X − 自检 Y」，并说明窗口内的运行次数是它的子集，
      所以两个数字不同不是矛盾。
   3. **对账有徽标**：偏差 1% 以内心 ✅，超过就 ⚠ 并给出百分比。
   4. 货币走 AD.money（0 显示 ¥0，不是 ¥0.0000），耗时走 AD.secs（"1.76 s"）。
   5. 所有来自接口的文本都过 AD.esc —— trace 名、问题、命令全是外部输入。

   页面逻辑留在页面文件里（app.js 只放各页共用的东西）。
   ============================================================ */
(function () {
  "use strict";

  var DEFS = {};      /* 后端 DEFS 的副本，见 loadDefs()；key 与后端一致 */

  var TOKEN_KEY = "agentdesk_token";
  var AUTO_MS = 30000;
  var LIMIT_KEY = "agentdesk_dash_limit";

  var NEEDS_TOKEN = false;
  var TOKEN = "";
  var LIMIT = 50;
  var timer = null;
  var busy = false;

  /* ---------- 小工具：没加载出 app.js 时也给出可读的降级 ---------- */
  var e = function (v) {
    if (window.AD && AD.esc) return AD.esc(v);
    return String(v === null || v === undefined ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };
  var MONEY = function (v) {
    if (window.AD && AD.money) return AD.money(v);
    var n = Number(v || 0);
    return n === 0 ? "¥0" : "¥" + n.toFixed(4);
  };
  var SECS = function (ms) {
    if (window.AD && AD.secs) return AD.secs(ms);
    ms = Number(ms || 0);
    return ms < 1000 ? Math.round(ms) + " ms" : (ms / 1000).toFixed(2) + " s";
  };
  var EMPTY = function (text, hint) {
    if (window.AD && AD.empty) return AD.empty(text, hint);
    return '<div class="empty"><div class="big">◌</div><div>' + e(text) +
      "</div>" + (hint ? '<p class="hint">' + e(hint) + "</p>" : "") + "</div>";
  };
  function byId(id) { return document.getElementById(id); }
  function put(id, html) { var n = byId(id); if (n) n.innerHTML = html; }
  function num(v, d) { var n = Number(v); return isFinite(n) ? n : (d || 0); }
  function pct(v, digits) { return (num(v) * 100).toFixed(digits === undefined ? 2 : digits); }

  /* ---------- 指标卡：与后端 components.kpi 同形 ---------- */
  function tile(label, value, opts) {
    opts = opts || {};
    var cls = "kpi" + (opts.tone ? " " + opts.tone : "");
    return '<div class="' + cls + '">' +
      '<div class="k">' + e(label) + info(opts.defKey) + "</div>" +
      '<div class="v">' + e(value) +
      (opts.unit ? "<small>" + e(opts.unit) + "</small>" : "") + "</div>" +
      (opts.note ? '<div class="x">' + e(opts.note) + "</div>" : "") +
      "</div>";
  }

  /* ---------- 接口 ---------- */
  function headers() {
    var h = { "Accept": "application/json" };
    if (NEEDS_TOKEN && TOKEN) h["Authorization"] = "Bearer " + TOKEN;
    return h;
  }
  function getJson(path) {
    return fetch(path, { headers: headers(), cache: "no-store" })
      .then(function (r) {
        if (r.status === 401) throw new Error("需要令牌（HTTP 401）—— 请在上方填入");
        if (!r.ok) throw new Error(path + " → HTTP " + r.status);
        return r.json();
      });
  }
  function fail(where, err) {
    put(where, '<div class="note err"><span class="ic">✕</span><span>' +
      e(where) + " 取数失败：" + e(err.message) +
      "。这是接口出错，不是「没有数据」—— 两者必须能分辨。</span></div>");
  }
  function showTopError(msg) {
    put("err", '<div class="note err"><span class="ic">✕</span><span>' + e(msg) +
      "</span></div>");
  }

  /* ---------- 概览 ---------- */
  function renderSummary(d) {
    var sc = d.scope || {};
    var el = d.elapsed_ms || {};
    var tk = (d.tokens || {}).total_tokens;
    if (tk === undefined || tk === null) tk = (d.tokens || {}).total || 0;
    var runs = num(d.runs);
    var sample = num(el.sample);

    var cards = [
      tile("运行次数", runs, { tone: "brand", defKey: "runs" }),
      tile("不同问题", num(d.distinct_questions), { defKey: "distinct" }),
      tile("错误", num(d.errors), {
        tone: num(d.errors) ? "err" : "ok", defKey: "errors"
      }),
      tile("总成本", MONEY(d.cost_cny), { defKey: "cost" }),
      tile("token", num(tk).toLocaleString("zh-CN"), { defKey: "tokens" }),
      tile("缓存命中率", pct(d.prompt_cache_hit_rate, 1), { unit: "%", defKey: "cache" }),
      tile("P50 延迟", num(el.p50), { unit: "s", defKey: "p50", note: "样本 " + sample + " 次" }),
      tile("P95 延迟", num(el.p95), { unit: "s", defKey: "p95", note: "样本 " + sample + " 次" }),
      tile("批处理", num(d.batch_runs), { defKey: "batch", note: "计入成本 · 不计入延迟" }),
      tile("统计窗口", "最近 " + num(sc.window_requested, LIMIT) + " 次", { defKey: "window" })
    ];

    if (!runs) {
      put("summaryCards", "");
      put("bridge", "");
      put("summaryEmpty", EMPTY("这个窗口里还没有真实运行记录",
        "先跑一次 /try 或 POST /agent/ask，再回来看板"));
      put("chartAgent", EMPTY("暂无数据"));
      put("chartType", EMPTY("暂无数据"));
      put("breakdownTables", "");
      return;
    }
    put("summaryEmpty", "");
    put("summaryCards", cards.join(""));
    put("bridge", bridgeHTML(d, sc));
  }

  /* ★ 口径桥接：把"看起来互相打架"的三个数字一次说清。
     数字全部来自接口自己的 scope，页面上**不自己算、不自己编**。 */
  function bridgeHTML(d, sc) {
    var parts = [];
    var scanned = num(sc.records_scanned);
    var selfTest = num(sc.selftest_excluded);
    var live = num(sc.live_available, num(d.runs));
    if (scanned > 0) {
      parts.push("<b>完整台账 " + live + " 条 = 扫描 " + scanned +
        " 条 − 自检 " + selfTest + " 条</b>（自检记录只用来验证「记录得对不对」，" +
        "不代表系统运行状况，所以不计入任何计数与成本）");
    }
    parts.push("<b>上面的「运行次数 " + num(d.runs) + "」是台账里的窗口子集</b>：" +
      "min(" + live + ", 窗口 " + num(sc.window_requested, LIMIT) + ") —— " +
      "所以它和完整台账条数不同是设计如此，不是口径打架");
    if (sc.note) parts.push(e(sc.note));
    return parts.map(function (p) { return '<div class="hint">' + p + "</div>"; }).join("");
  }

  /* ---------- 归因表 + 条形图 ---------- */
  function tableHTML(headers, rows) {
    var hi = headers.map(function (h) { return "<th>" + e(h) + "</th>"; }).join("");
    var bi = rows.map(function (r) {
      return "<tr>" + r.map(function (c, i) {
        return i === 0 ? "<td>" + c + "</td>" : '<td class="r">' + c + "</td>";
      }).join("") + "</tr>";
    }).join("");
    return '<div class="tablewrap"><table><thead><tr>' + hi +
      "</tr></thead><tbody>" + bi + "</tbody></table></div>";
  }

  function renderBreakdown(d) {
    var name = d.by_span_name || {};
    var type = d.by_span_type || {};
    put("breakdownTables",
      '<div class="grid c2">' +
      bucketCard("按 Agent（钱花在谁身上）", "Agent", name, "by_name") +
      bucketCard("按动作（钱花在什么事上）", "动作类型", type, "by_type") + "</div>");
    put("chartAgent", window.AD && AD.bars ? AD.bars(name, "cost_cny", MONEY)
                                           : EMPTY("暂无数据"));
    put("chartType", window.AD && AD.bars ? AD.bars(type, "cost_cny", MONEY)
                                          : EMPTY("暂无数据"));
  }

  function bucketCard(title, firstCol, map, defKey) {
    var keys = Object.keys(map || {});
    if (!keys.length) return shCard(title, EMPTY("暂无数据"), defKey);
    keys.sort(function (a, b) {
      return num((map[b] || {}).cost_cny) - num((map[a] || {}).cost_cny);
    });
    var rows = keys.map(function (k) {
      var b = map[k] || {};
      return "<tr><td>" + e(k) + '</td><td class="r">' + num(b.calls) +
        '</td><td class="r">' + num(b.tokens).toLocaleString("zh-CN") +
        '</td><td class="r">' + MONEY(b.cost_cny) +
        '</td><td class="r">' + SECS(b.elapsed_ms) + "</td></tr>";
    }).join("");
    return shCard(title, tableHTML([firstCol, "次数", "token", "成本", "耗时"], []) +
      '<div class="tablewrap"><table><tbody>' + rows + "</tbody></table></div>", defKey);
  }

  /* 卡片外壳（与后端 components.card 同形） */
  function shCard(title, bodyHtml, defKey) {
    return '<div class="card tight"><div class="card-head"><span class="t">' + e(title) +
      '</span><span class="d">合计等于总账 · 与另一维不可相加</span>' +
      '<span class="actions">' + info(defKey) + "</span></div>" + bodyHtml + "</div>";
  }

  /* ---------- 对账 ---------- */
  function gapBadge(gap) {
    var g = num(gap);
    if (Math.abs(g) <= 0.01) return '<span class="badge ok">✅ 一致</span>';
    return '<span class="badge warn">⚠ 偏差 ' + Math.abs(g * 100).toFixed(2) + "%</span>";
  }

  function renderReconcile(d) {
    var rc = d.reconcile || {};
    if (!num(d.runs) || rc.trace_total_cny === undefined) {
      put("reconcileBadges", "");
      put("reconcileNote", EMPTY("这个窗口里还没有可对账的花费",
        "对账要等至少一条真实运行"));
      put("reconcileTable", "");
      return;
    }
    var typeOk = Math.abs(num(rc.by_type_gap)) <= 0.01;
    var nameOk = Math.abs(num(rc.by_name_gap)) <= 0.01;
    put("reconcileBadges",
      '<div class="btnrow">' + gapBadge(rc.by_type_gap) +
      '<span class="hint" style="margin:0">按动作合计 vs 总账</span>' +
      gapBadge(rc.by_name_gap) +
      '<span class="hint" style="margin:0">按 Agent 合计 vs 总账</span>' +
      '<span class="badge ' + (typeOk && nameOk ? "ok" : "warn") + '">' +
      (typeOk && nameOk ? "✅ 对账一致（阈值 1%）" : "⚠ 有维度偏离超过 1%") +
      "</span></div>");

    var rows = [
      "<tr><td>总账（trace 合计）</td><td class=\"r\">" + MONEY(rc.trace_total_cny) +
        '</td><td class="r">基准</td><td class="r">' +
        '<span class="badge neutral">基准</span></td></tr>',
      "<tr><td>按动作合计</td><td class=\"r\">" + MONEY(rc.by_type_total_cny) +
        '</td><td class="r">' + pct(rc.by_type_gap) + '%</td><td class="r">' +
        gapBadge(rc.by_type_gap) + "</td></tr>",
      "<tr><td>按 Agent 合计</td><td class=\"r\">" + MONEY(rc.by_name_total_cny) +
        '</td><td class="r">' + pct(rc.by_name_gap) + '%</td><td class="r">' +
        gapBadge(rc.by_name_gap) + "</td></tr>"
    ].join("");
    put("reconcileTable", '<div class="tablewrap"><table><thead><tr>' +
      "<th>口径</th><th>金额</th><th>与总账偏差</th><th>判定</th></tr></thead><tbody>" +
      rows + "</tbody></table></div>");

    var notes = [];
    if (rc.warning) notes.push('<div class="note warn"><span class="ic">⚠</span><span>' +
      e(rc.warning) + "</span></div>");
    if (d.unpriced_models && d.unpriced_models.length) {
      notes.push('<div class="note warn"><span class="ic">⚠</span><span>未定价模型：' +
        e(d.unpriced_models.join("、")) +
        " —— 这些金额是按兜底价估的，不是真实单价。</span></div>");
    }
    notes.push('<div class="hint">' + e(rc.note || "") +
      " · 无 span 的 trace：" + num(rc.traces_without_spans) +
      " 条（残差的已知来源：它们的钱只进总账，进不了两个维度）</div>");
    put("reconcileNote", notes.join(""));
  }

  /* ---------- 最近运行 + 逐步轨迹 ---------- */
  function traceCard(t) {
    var ok = t.status === "ok";
    var id = String(t.trace_id || "");
    return '<div class="card tight trace-row" data-trace="' + e(id) + '" role="button"' +
      ' tabindex="0" aria-expanded="false" style="cursor:pointer">' +
      '<div class="btnrow" style="gap:8px">' +
      '<b>' + e(t.name || "(未命名)") + "</b>" +
      '<span class="badge ' + (ok ? "ok" : "err") + '">' + e(t.status || "?") + "</span>" +
      '<code class="mono">' + e(id) + "</code>" +
      '<span class="hint" style="margin:0">' + SECS(t.elapsed_ms) + " · " +
      MONEY(t.cost_cny) + " · " + num(t.span_count) + " span</span>" +
      '<span class="hint" style="margin:0 0 0 auto">' +
      e(t.started_at || "") + " · 点开看轨迹</span>" +
      "</div>" +
      (t.question ? '<div class="hint" style="margin:6px 0 0">问：' + e(t.question) +
        "</div>" : "") +
      '<div class="trace-detail" hidden></div></div>';
  }

  function renderTraces(items) {
    if (!items.length) {
      put("traces", EMPTY("还没有运行记录", "先跑一次 /try 或 POST /agent/ask"));
      return;
    }
    put("traces", items.map(traceCard).join(""));
    Array.prototype.forEach.call(
      document.querySelectorAll("#traces .trace-row"), function (node) {
        node.addEventListener("click", function () { toggleTrace(node); });
        node.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggleTrace(node); }
        });
      });
  }

  function toggleTrace(node) {
    var box = node.querySelector(".trace-detail");
    if (!box || !box.hidden) {
      if (box) { box.hidden = true; node.setAttribute("aria-expanded", "false"); }
      return;
    }
    box.hidden = false;
    node.setAttribute("aria-expanded", "true");
    box.innerHTML = '<div class="hint">正在取 ' + e(node.dataset.trace) + " 的 span…</div>";
    getJson("/traces/" + encodeURIComponent(node.dataset.trace)).then(function (d) {
      var spans = (d.spans || []).map(function (s) {
        return { name: s.name, elapsed_ms: num(s.elapsed_ms), ok: s.status === "ok" };
      });
      if (!spans.length) {
        box.innerHTML = EMPTY("这条 trace 没有 span",
          "常见于流式接口：它把成本记进总账，但过程没有埋点");
      } else if (window.AD && AD.timeline) {
        box.innerHTML = AD.timeline(spans);
      } else {
        box.innerHTML = EMPTY("时间线组件未加载（/static/app.js）");
      }
    }).catch(function (err) {
      box.innerHTML = '<div class="note err"><span class="ic">✕</span><span>' +
        e(err.message) + "</span></div>";
    });
  }

  /* ---------- 待审批（只读展示） ---------- */
  function renderApprovals(items) {
    if (!items.length) {
      put("approvalsBox", EMPTY("没有待确认的写操作", "有高危写操作时会出现在这里"));
      return;
    }
    var rows = items.map(function (a) {
      return "<tr><td>" + e(a.id) + "</td><td><code class=\"mono\">" +
        e(a.command) + "</code></td><td>" + e(a.risk || "—") +
        '</td><td class="r">' + e(a.expires_at || "—") + "</td></tr>";
    }).join("");
    put("approvalsBox", '<div class="tablewrap"><table><thead><tr>' +
      "<th>编号</th><th>命令</th><th>风险</th><th>到期</th></tr></thead><tbody>" +
      rows + "</tbody></table></div>");
  }

  /* ---------- 编排 ---------- */
  function loadAll() {
    if (busy) return Promise.resolve();
    busy = true;
    var stamp = byId("stamp");
    if (stamp) stamp.textContent = "载入中…";
    function done() {
      busy = false;
      if (stamp) stamp.textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN");
      applyDefTitles();
    }
    return Promise.all([
      getJson("/metrics/summary?limit=" + LIMIT).then(function (d) {
        renderSummary(d); renderBreakdown(d); renderReconcile(d);
      }).catch(function (err) {
        fail("summary", err); fail("breakdown", err); fail("reconcile", err);
      }),
      getJson("/traces?limit=" + Math.min(LIMIT, 50)).then(function (d) {
        renderTraces(d.items || []);
      }).catch(function (err) { fail("traces", err); }),
      getJson("/approvals?status=pending&limit=10").then(function (d) {
        renderApprovals(d.items || []);
      }).catch(function (err) { fail("approvals", err); })
    ]).then(done, done);
  }

  function toggleAuto() {
    var box = byId("auto");
    if (!box) return;
    if (timer) { clearInterval(timer); timer = null; }
    if (box.checked) timer = setInterval(loadAll, AUTO_MS);
  }

  /* ---------- 口径 ⓘ ----------
     定义只有后端一处（app/web/pages/dashboard.py 的 DEFS），
     服务端把整张表塞进 <script type="application/json" id="pageDefs">，
     这里读出来当 title 用 —— 页面上不会出现「提示和文档说的不一样」。 */
  function info(defKey) {
    if (!defKey) return "";
    var text = DEFS[defKey] || "";
    return '<i class="info"' + (text ? ' title="' + e(text) + '"' : "") + ">i</i>";
  }

  function applyDefTitles() {
    var tips = document.querySelectorAll(".info[data-tip]");
    for (var k = 0; k < tips.length; k++) {
      tips[k].setAttribute("title", tips[k].getAttribute("data-tip"));
    }
  }

  function loadDefs() {
    var node = byId("pageDefs");
    if (!node) return;
    try {
      DEFS = JSON.parse(node.textContent || "{}") || {};
    } catch (err) {
      DEFS = {};               // 解析失败就退化成"没有提示"，页面照常可用
    }
  }

  function boot() {
    loadDefs();
    var state = byId("pageState");
    NEEDS_TOKEN = !!(state && state.dataset.needsToken === "true");
    TOKEN = localStorage.getItem(TOKEN_KEY) || "";
    var saved = parseInt(localStorage.getItem(LIMIT_KEY) || "", 10);
    if (saved > 0) LIMIT = saved;

    var sel = byId("limitSel");
    if (sel) {
      sel.value = String(LIMIT);
      sel.addEventListener("change", function () {
        LIMIT = parseInt(sel.value, 10) || 50;
        localStorage.setItem(LIMIT_KEY, String(LIMIT));
        loadAll();
      });
    }
    if (NEEDS_TOKEN) {
      var bar = byId("tokbar");
      var inp = byId("token");
      if (bar) bar.hidden = false;
      if (inp) inp.value = TOKEN;
      var save = byId("tokSave");
      if (save) save.addEventListener("click", function () {
        TOKEN = (inp && inp.value || "").trim();
        localStorage.setItem(TOKEN_KEY, TOKEN);
        loadAll();
      });
    }
    var rb = byId("refreshBtn");
    if (rb) rb.addEventListener("click", function () { loadAll(); });
    var ab = byId("auto");
    if (ab) ab.addEventListener("change", toggleAuto);

    applyDefTitles();
    loadAll();
  }

  // ★ 全局错误必须**可见**：旧看板第一版栽在这里 ——
  //   一处语法错误让整段脚本没执行，五块数据区全空、连一条报错都没有，
  //   看起来像"没数据"，实际是"代码根本没跑"。
  //   空白和出错必须是两种可见的状态。
  window.addEventListener("error", function (ev) {
    showTopError("页面脚本出错：" + (ev.message || "未知") +
      "（第 " + (ev.lineno || "?") + " 行）—— 数据区因此可能是空的");
  });

  document.addEventListener("DOMContentLoaded", boot);
})();
