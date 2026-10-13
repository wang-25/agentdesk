/* ============================================================
   AgentDesk 前端公共工具（零依赖、无构建）
   ------------------------------------------------------------
   只放**各页都要用**的东西：转义、货币、请求封装、健康状态点。
   页面自己的逻辑留在页面里（就近可读，别把 5 个页面的逻辑堆成一个文件）。
   ============================================================ */
(function () {
  "use strict";

  window.AD = window.AD || {};

  /* ---------- 转义：所有插进 DOM 的外部文本都要过这里 ---------- */
  AD.esc = function (s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };

  /* ---------- 货币：按量级自适应精度（与后端 components.money 同一规则） ---------- */
  AD.money = function (v) {
    v = Number(v || 0);
    if (!isFinite(v)) return "—";
    if (v === 0) return "¥0";
    var a = Math.abs(v);
    if (a < 0.01) return "¥" + v.toFixed(4);
    if (a < 1000) return "¥" + v.toFixed(2);
    return "¥" + v.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
  };

  /* ---------- 耗时：数字与单位之间留空格 ---------- */
  AD.secs = function (ms) {
    ms = Number(ms || 0);
    if (!isFinite(ms)) return "—";
    return ms < 1000 ? Math.round(ms) + " ms" : (ms / 1000).toFixed(2) + " s";
  };

  /* ---------- 请求封装：统一错误处理，绝不把异常留给调用方猜 ---------- */
  AD.get = function (url) {
    return fetch(url, { headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
  };

  /* ---------- 健康状态点：把 /healthz 露到侧栏 ---------- */
  AD.watchHealth = function (intervalMs) {
    var dot = document.getElementById("healthDot");
    var text = document.getElementById("healthText");
    if (!dot || !text) return;
    function tick() {
      AD.get("/healthz").then(function (d) {
        var ok = d && d.ok;
        dot.className = "dot" + (ok ? "" : " err");
        var failing = (d && d.failing) || [];
        text.textContent = ok ? "正常" : ("异常：" + failing.join("、"));
        dot.title = ok ? "健康检查全部通过（点自检看明细）"
                       : ("未通过：" + failing.join("、"));
      }).catch(function () {
        dot.className = "dot err";
        text.textContent = "自检不可达";
        dot.title = "取不到 /healthz";
      });
    }
    tick();
    setInterval(tick, intervalMs || 60000);
  };

  /* ---------- 时间线（与后端 components.timeline 同一形态） ---------- */
  AD.NODE_LABEL = {
    supervisor: "调度决策", intent: "意图路由", knowledge: "知识检索",
    diagnose: "工具执行", reason: "纯推理分析", verify: "结果校验",
    remediate: "处置建议", finalize: "汇总输出"
  };

  AD.timeline = function (items) {
    items = items || [];
    var total = 0;
    items.forEach(function (it) { total += (it.elapsed_ms || 0); });
    if (!total) return '<div class="empty"><div>暂无计时数据</div></div>';
    var rows = items.map(function (it) {
      var ms = it.elapsed_ms || 0;
      var pct = Math.max(1, Math.round(ms * 100 / total));
      var nm = it.node || it.name || "?";
      return '<div class="row"><span class="nm">' +
        AD.esc(AD.NODE_LABEL[nm] || nm) + '</span>' +
        '<span class="tr"><span class="fl' + (it.ok === false ? " bad" : "") +
        '" style="width:' + pct + '%"></span></span>' +
        '<span class="ms">' + Math.round(ms) + 'ms</span></div>';
    }).join("");
    return '<div class="tl">' + rows + '<p class="hint">总耗时 ' +
      (total / 1000).toFixed(1) + 's</p></div>';
  };

  /* ---------- 条形图（fill 必须 display:block，见 app.css） ---------- */
  AD.bars = function (map, key, fmt) {
    fmt = fmt || AD.money;
    var keys = Object.keys(map || {});
    if (!keys.length) return '<div class="empty"><div>暂无数据</div></div>';
    var vals = keys.map(function (k) { return Number((map[k] || {})[key] || 0); });
    var top = Math.max.apply(null, vals) || 1;
    var order = keys.sort(function (a, b) {
      return Number((map[b] || {})[key] || 0) - Number((map[a] || {})[key] || 0);
    });
    return '<div class="bars">' + order.map(function (k) {
      var v = Number((map[k] || {})[key] || 0);
      var pct = Math.max(1, Math.round(v * 100 / top));
      return '<div class="bar"><span class="lbl" title="' + AD.esc(k) + '">' +
        AD.esc(k) + '</span><span class="track">' +
        '<span class="fill" style="width:' + pct + '%"></span></span>' +
        '<span class="val">' + AD.esc(fmt(v)) + '</span></div>';
    }).join("") + "</div>";
  };

  /* ---------- 空态 ---------- */
  AD.empty = function (text, hint) {
    return '<div class="empty"><div class="big">◌</div><div>' + AD.esc(text) +
      '</div>' + (hint ? '<p class="hint">' + AD.esc(hint) + '</p>' : "") + "</div>";
  };

  document.addEventListener("DOMContentLoaded", function () {
    if (document.getElementById("healthDot")) AD.watchHealth(60000);
  });
})();
