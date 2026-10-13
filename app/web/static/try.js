/* ============================================================
   提问台（/try）的前端逻辑 —— 零依赖、无构建、无 CDN
   ------------------------------------------------------------
   ★ 结果区不是"提交后长出来的"：四个分区（轨迹/结论/证据/成本）
     由服务端渲染好（含空态与骨架），这里只**原地换内容**。
     所以页面结构在任何时刻都是完整可预期的。
   ★ 所有外部文本（模型答案 / 工具输出 / 后端报错）进 DOM 前一律过 AD.esc()。
   ★ markdown 渲染是**先转义再套标签**的白名单式转换（见 AD.md）：
     旧页从 jsdelivr 加载 marked，断网/内网环境下整页就废了。
   ============================================================ */
(function () {
  "use strict";

  var AD = window.AD;
  var NL = String.fromCharCode(10);          /* 不写反斜杠字面量：三层转义最容易吃掉一层 */
  var KEY = "agentdesk_token";
  var cfg = { needsToken: false, streamEngine: "supervisor" };
  var t0 = 0, timer = null, ctrl = null, busy = false;

  /* ---------- 小工具 ---------- */
  function $(id) { return document.getElementById(id); }
  function esc(s) { return AD.esc(s); }
  function money(v) { return AD.money(v); }
  function now() { return new Date().toLocaleTimeString("zh-CN"); }
  function badge(text, tone) {
    return '<span class="badge ' + esc(tone) + '">' + esc(text) + "</span>";
  }
  function kv(pairs) {
    return '<dl class="kv">' + pairs.map(function (p) {
      return "<dt>" + esc(p[0]) + "</dt><dd>" + esc(p[1]) + "</dd>";
    }).join("") + "</dl>";
  }

  /* ---------- 四个分区的状态：空态 / 加载 / 错误 / 内容 ---------- */
  function setTrace(html) { $("traceBox").innerHTML = html; }
  function setConclusion(html) { $("conclusionBox").innerHTML = html; }
  function setEvidence(html) { $("evidenceBox").innerHTML = html; }
  function setCost(html) { $("costBox").innerHTML = html; }
  function setCount(text, tone) {
    var el = $("traceCount");
    el.className = "badge " + tone;
    el.textContent = text;
  }
  function skel(n, h) {
    var out = "";
    for (var i = 0; i < n; i++) {
      out += '<div class="sk" style="width:' + (100 - i * 8) + '%;height:' +
        (h || 12) + 'px"></div>';
    }
    return '<div class="stack">' + out + "</div>";
  }
  /* 统一的错误态：错误要落到它所属的分区里，不要整页替换 */
  function errHTML(text) {
    return '<div class="note err"><span class="ic">✕</span><span>' + esc(text) +
      "</span></div>";
  }

  /* ============================================================
     markdown → HTML（极小实现，白名单式）
     ------------------------------------------------------------
     【为什么不用 marked / 任何 CDN】
     项目宣称"零依赖、可离线/内网部署"。旧页 <script src="https://cdn.jsdelivr.net/...">
     意味着断网或内网环境下这一页是坏的 —— 一个运维工具不该有这种失败面。

     【安全模型：先转义，再套标签】
     ① 先 AD.esc() 把 & < > " ' 全部转义 —— 此后字符串里**不可能**再出现
        能被浏览器当成标签的字符；
     ② 之后只做「把少量已知模式换成固定标签」，标签由**本文件**给出，
        不含任何来自模型内容的字符（唯一的插值是已经转义过的文本或 URL）。
     所以模型输出永远进不了标签位置 —— 这比"事后过滤 <script>"可靠得多。
     ============================================================ */
  function inline(s) {
    /* 行内：代码 → 粗体 → 链接（顺序重要：先处理 code，避免它内部再被加标签） */
    s = s.replace(/`([^`]+)`/g, function (m, code) {
      return '<code class="mono">' + code + "</code>";
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function (m, text, href) {
      return '<a href="' + href + '" rel="noopener noreferrer" target="_blank">' +
        text + "</a>";
    });
    return s;
  }

  AD.md = function (text) {
    if (text === null || text === undefined || text === "") { return ""; }
    var src = AD.esc(String(text));           /* ① 先转义：之后只可能加标签 */
    return src.split(/\r?\n/).map(function (raw) {
      var line = inline(raw.trim());           /* ② 再套白名单标签 */
      if (!line) { return ""; }
      if (/^#{1,6}\s/.test(line)) {
        var level = line.match(/^#+/)[0].length;
        return "<h" + level + ">" + line.replace(/^#+\s*/, "") + "</h" + level + ">";
      }
      if (/^\s*[-*·]\s+/.test(line)) { return "<li>" + line.replace(/^\s*[-*·]\s+/, "") + "</li>"; }
      if (/^\s*\d+[.)]\s+/.test(line)) { return "<li>" + line.replace(/^\s*\d+[.)]\s+/, "") + "</li>"; }
      if (/^&gt;\s?/.test(line)) { return "<blockquote>" + line.replace(/^&gt;\s?/, "") + "</blockquote>"; }
      if (/^(---|\*\*\*)$/.test(line)) { return "<hr>"; }
      return "<p>" + line + "</p>";
    }).join("").replace(/(<li>[\s\S]*?<\/li>)+/g, function (m) {
      return "<ul>" + m + "</ul>";
    });
  };

  /* ============================================================
     渲染：结果四分区
     ============================================================ */
  function metricsOf(d) { return (d && d.metrics) || {}; }

  function renderCost(d, mode) {
    var m = metricsOf(d);
    var elapsed = m.elapsed_ms;
    if (!elapsed) { elapsed = Date.now() - t0; }
    var rows = [
      ["耗时", AD.secs(elapsed)],
      ["tokens", m.tokens === undefined ? "—" : String(m.tokens)]
    ];
    if (mode !== "rag") {
      rows.splice(1, 0, ["轮次", m.rounds === undefined ? "—" : String(m.rounds)]);
      rows.splice(2, 0, ["工具调用", m.tool_calls === undefined ? "—" : String(m.tool_calls)]);
    }
    var cache = (d && (d.cache_hit_tokens || d.cached_tokens)) || 0;
    if (cache) { rows.push(["命中缓存 tokens", String(cache)]); }
    var cost = (d && (d.cost_cny !== undefined ? d.cost_cny : d.cost)) || 0;
    rows.push(["本轮估算成本", cost ? money(cost) : "未返回金额"]);
    rows.push(["引擎", (d && d.engine) || mode]);
    return kv(rows) +
      '<p class="hint">成本按模型返回的 token 用量估算；金额只在接口返回时才显示 —— ' +
      '推测出来的数字比没有数字更糟。</p>';
  }

  function renderEvidence(d) {
    var html = "";
    var cites = (d && (d.citations || d.sources)) || [];
    if (cites.length) {
      html += '<div class="section"><div class="hint" style="margin:0 0 var(--s2)">' +
        "知识库引用（可翻回原文核对，防编造）</div>" +
        '<div class="tablewrap"><table><thead><tr><th>#</th><th>来源</th>' +
        "<th>标题</th><th>片段</th></tr></thead><tbody>" +
        cites.map(function (c, i) {
          return "<tr><td>" + esc(c.no || (i + 1)) + "</td><td><code>" +
            esc(c.source || "") + "</code></td><td>" + esc(c.title || "") +
            "</td><td>" + esc((c.preview || c.text || "").slice(0, 150)) + "</td></tr>";
        }).join("") + "</tbody></table></div></div>";
    }
    var trace = (d && d.trace) || [];
    if (trace.length) {
      html += '<div class="section"><div class="hint" style="margin:0 0 var(--s2)">' +
        "逐步轨迹（每步的判断、调用的工具与结果）</div>" +
        '<div class="tablewrap"><table><thead><tr><th>步</th><th>它的判断</th>' +
        "<th>工具</th><th>参数</th><th>结果</th></tr></thead><tbody>" +
        trace.map(function (s) {
          return "<tr><td>" + esc(s.step) + "</td><td>" +
            esc((s.thought || "").slice(0, 110)) + "</td><td><code>" +
            esc(s.tool || "") + "</code></td><td><code>" +
            esc(JSON.stringify(s.args || {})) + "</code></td><td>" +
            (s.ok ? "成功" : "失败") +
            (s.risk ? " · 风险 " + esc(s.risk) : "") + "</td></tr>";
        }).join("") + "</tbody></table></div></div>";
    }
    var orch = (d && d.orchestration) || {};
    var groups = [["已执行命令", orch.executed_commands],
                  ["被策略拦下", orch.denied_commands],
                  ["待人工审批", orch.pending_approvals]];
    var extra = groups.filter(function (g) { return (g[1] || []).length; });
    if (extra.length) {
      html += '<div class="section">' + extra.map(function (g) {
        return "<p class=\"hint\" style=\"margin:var(--s2) 0\">" + esc(g[0]) + "</p><ul>" +
          g[1].map(function (x) {
            return "<li><code class=\"mono\">" +
              esc(typeof x === "string" ? x : JSON.stringify(x)) + "</code></li>";
          }).join("") + "</ul>";
      }).join("") + "</div>";
    }
    if (!html) { return AD.empty("这次没有返回证据", "没有引用与轨迹：可能是纯推理回答"); }
    return html;
  }

  function renderConclusion(d) {
    var html = "";
    var orch = (d && d.orchestration) || {};
    var flags = [];
    if (orch.intent) { flags.push("意图 " + orch.intent); }
    if (orch.verdict) { flags.push("校验 " + orch.verdict); }
    if ((orch.pending_approvals || []).length) {
      flags.push("待审批 " + orch.pending_approvals.length + " 条");
    }
    if (flags.length) {
      html += '<div class="chips" style="margin:0 0 var(--s3)">' +
        flags.map(function (f) {
          return '<span class="badge ' +
            (/待审批/.test(f) ? "warn" : "info") + '">' + esc(f) + "</span>";
        }).join("") + "</div>";
    }
    html += '<div class="md"><pre>' + esc(d.answer || "（没有返回 answer 字段）") + "</pre></div>";
    if (orch.remediation) {
      html += '<div class="section"><p class="hint" style="margin:0 0 var(--s2)">' +
        "处置建议（写操作仍需人工批准）</p><div>" + AD.md(String(orch.remediation)) +
        "</div></div>";
    }
    return html;
  }

  function renderOk(d, mode) {
    setTrace(d.orchestration && d.orchestration.node_log
      ? AD.timeline(d.orchestration.node_log)
      : AD.empty("这次没有返回节点计时", "轨迹表在下面的「证据」里"));
    setConclusion(renderConclusion(d));
    setEvidence(renderEvidence(d));
    setCost(renderCost(d, mode));
  }

  function renderError(msg, hint) {
    var html = errHTML(msg) + (hint ? '<p class="hint">' + esc(hint) + "</p>" : "");
    setTrace(html);
    setConclusion(AD.empty("没有结论", msg));
    setEvidence(AD.empty("没有证据", "请求在取到结果前就结束了"));
    setCost(AD.empty("没有成本", "这一轮没有完成的调用"));
    setCount("失败", "err");
  }

  function setPending() {
    setTrace(skel(4, 12));
    setConclusion(skel(3, 14));
    setEvidence(skel(2, 12));
    setCost(skel(3, 12));
    setCount("进行中", "info");
  }

  /* ============================================================
     提交
     ============================================================ */
  function token() {
    var el = $("token");
    return el ? el.value.trim() : (localStorage.getItem(KEY) || "");
  }
  function headers() {
    var h = { "Content-Type": "application/json" };
    var t = token();
    if (t) { h["X-API-Key"] = t; }
    return h;
  }

  function checkToken(question) {
    if (cfg.needsToken && !token()) {
      renderError("请先填写访问令牌",
        "它在服务器 .env 的 AGENT_TOKEN 那一行；填一次即可，浏览器会记住");
      return false;
    }
    if (!question) {
      renderError("问题不能为空", "可以直接点上面的示例问题");
      return false;
    }
    return true;
  }

  function busyUI(on) {
    busy = on;
    $("go").disabled = on;
    $("stop").disabled = !on;
    $("go").innerHTML = on
      ? '<span class="spin"></span>处理中…'
      : "开始";
    $("q").readOnly = on;
  }

  function tick() {
    setCount("进行中 · 已等待 " + ((Date.now() - t0) / 1000).toFixed(1) + "s", "info");
  }

  function submit() {
    if (busy) { return; }
    var question = $("q").value.trim();
    var mode = $("mode").value;
    if (!checkToken(question)) { return; }

    var t = token();
    if (t) {
      localStorage.setItem(KEY, t);
      if ($("tokstate")) { $("tokstate").textContent = "已保存令牌（本机浏览器）。"; }
    }
    t0 = Date.now();
    setPending();
    busyUI(true);
    clearInterval(timer);
    timer = setInterval(tick, 500);
    ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;

    var job = (mode === cfg.streamEngine) ? stream(question, mode)
                                          : oneshot(question, mode);
    job.catch(function (e) {
      if (e && e.name === "AbortError") {
        setCount("已停止", "neutral");
        setTrace(errHTML("已手动停止") +
          '<p class="hint">已收到的步骤保留在下方「证据」里。</p>');
        return;
      }
      renderError("请求失败：" + (e && e.message ? e.message : "未知错误"),
        "确认服务在跑：uvicorn app.main:app --port 8000");
    }).then(function () {
      clearInterval(timer);
      busyUI(false);
      ctrl = null;
    });
  }

  /* ---------- 一次性请求（rag / langgraph / handwritten） ---------- */
  function oneshot(question, mode) {
    var path, body;
    if (mode === "rag") {
      path = "/rag/ask";
      body = { question: question, top_k: 5, mode: "hybrid" };
    } else {
      path = "/agent/ask";
      body = { question: question, engine: mode, include_trace: true };
    }
    return fetch(path, {
      method: "POST", headers: headers(), body: JSON.stringify(body),
      signal: ctrl ? ctrl.signal : undefined
    }).then(function (r) {
      return r.text().then(function (text) {
        return handleHTTP(r.status, text).then(function (d) {
          renderOk(d, mode);
          setCount("完成 · " + ((Date.now() - t0) / 1000).toFixed(1) + "s", "ok");
        });
      });
    });
  }

  function handleHTTP(status, text) {
    var data = null;
    try { data = JSON.parse(text); } catch (e) { /* 下面按状态码处理 */ }
    if (status === 401) {
      return Promise.reject(new Error(
        "令牌不对（HTTP 401）。检查它是否与服务器 .env 的 AGENT_TOKEN 完全一致"));
    }
    if (status === 429) {
      var d = (data && data.detail) ? String(data.detail) : "";
      return Promise.reject(new Error("触发限流（HTTP 429）。" + d +
        " 每 IP 每分钟 20 次、全站 60 次、每天 300 次"));
    }
    if (status < 200 || status >= 300) {
      return Promise.reject(new Error("HTTP " + status + "：" +
        String(text || "").slice(0, 300)));
    }
    if (data === null) {
      return Promise.reject(new Error("返回不是合法 JSON"));
    }
    return Promise.resolve(data);
  }

  /* ---------- SSE 流式（supervisor）：步骤实时点亮 ---------- */
  function stream(question, mode) {
    var steps = $("traceBox");
    steps.innerHTML = '<div class="steps"></div>';
    var box = steps.firstChild;
    var last = null, gotResult = false;

    function addStep(d) {
      if (last) { last.className = "step done"; }
      var row = document.createElement("div");
      row.className = "step";
      row.innerHTML = '<span class="dot"></span><b>' +
        esc(AD.NODE_LABEL[d.node] || d.node || "?") + "</b>" +
        (d.summary ? '<span class="sum">' + esc(d.summary) + "</span>" : "") +
        (d.elapsed_ms !== undefined ? '<span class="ms">' + esc(d.elapsed_ms) + "ms</span>" : "");
      box.appendChild(row);
      last = row;
      setCount("进行中 · " + box.children.length + " 步", "info");
    }

    return fetch("/agent/ask/stream", {
      method: "POST", headers: headers(),
      body: JSON.stringify({ question: question, engine: "supervisor" }),
      signal: ctrl ? ctrl.signal : undefined
    }).then(function (r) {
      if (r.status === 401) {
        return Promise.reject(new Error("令牌不对（HTTP 401）"));
      }
      if (r.status === 429) {
        return Promise.reject(new Error("触发限流（HTTP 429），等一下再试"));
      }
      if (!r.ok || !r.body) {
        return Promise.reject(new Error("流式接口不可用（HTTP " + r.status + "）"));
      }
      var reader = r.body.getReader();
      var dec = new TextDecoder();
      var buf = "";
      function pump() {
        return reader.read().then(function (c) {
          if (c.done) { return null; }
          buf += dec.decode(c.value, { stream: true });
          var i;
          while ((i = buf.indexOf(NL + NL)) >= 0) {
            var frame = buf.slice(0, i);
            buf = buf.slice(i + 2);
            var evName = "", dataRaw = "";
            frame.split(NL).forEach(function (ln) {
              if (ln.indexOf("event: ") === 0) { evName = ln.slice(7); }
              else if (ln.indexOf("data: ") === 0) { dataRaw = ln.slice(6); }
            });
            if (!dataRaw) { continue; }
            var d = null;
            try { d = JSON.parse(dataRaw); } catch (e) { continue; }
            if (evName === "step") {
              addStep(d);
            } else if (evName === "result") {
              if (last) { last.className = "step done"; }
              gotResult = true;
              renderOk(d, mode);
              setCount("完成 · " + ((Date.now() - t0) / 1000).toFixed(1) + "s", "ok");
            } else if (evName === "error") {
              gotResult = true;
              renderError("诊断中断：" + (d.error || "未知错误"), "服务端已记录这次失败");
            }
          }
          return pump();
        });
      }
      return pump().then(function () {
        /* 流结束却没等到 result = 被截断了。不能静默：空白和出错是两种状态 */
        if (!gotResult) {
          renderError("流在结果到达前结束（服务端可能中断）",
            "已收到的步骤见上方「执行轨迹」");
        }
      });
    });
  }

  /* ============================================================
     事件绑定
     ============================================================ */
  function bind() {
    var cfgEl = $("tryConfig");
    if (cfgEl) {
      try { cfg = JSON.parse(cfgEl.textContent) || cfg; } catch (e) { /* 用默认值 */ }
    }
    var saved = localStorage.getItem(KEY) || "";
    if ($("token")) {
      $("token").value = saved;
      if ($("tokstate")) {
        $("tokstate").textContent = saved
          ? "已保存令牌（本机浏览器），直接点「开始」即可。"
          : "还没有填写令牌。";
      }
    }
    $("go").addEventListener("click", submit);
    $("clear").addEventListener("click", function () {
      $("q").value = "";
      $("q").focus();
      setTrace(skel(4, 12));
      setConclusion(AD.empty("还没有结论",
        "提交一个问题后，这里是它给出的答案与校验结论"));
      setEvidence(AD.empty("还没有证据", "工具输出、知识库引用与逐步轨迹都会落在这里"));
      setCost(AD.empty("还没有成本", "本轮调用的轮次、工具次数、tokens 与耗时"));
      setCount("等待提问", "neutral");
    });
    $("stop").addEventListener("click", function () {
      if (ctrl) { ctrl.abort(); }
    });
    document.querySelectorAll(".chip[data-q]").forEach(function (el) {
      el.addEventListener("click", function () {
        $("q").value = el.getAttribute("data-q") || "";
        $("q").focus();
      });
    });
    $("q").addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { submit(); }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
