/* 对话积分浮窗 —— 注入到 WB 网关看板里。
   数据来自 /sessions（由 run_gateway.py 注册），样式跟随看板的 --bg / --fg 变量。 */
(function () {
  if (window.__wbSessionsReady) return;
  window.__wbSessionsReady = true;

  var REFRESH_MS = 60000;

  var css = ''
    + '#wbsc{position:fixed;right:18px;bottom:18px;width:352px;z-index:9999;'
    + 'font:12px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;'
    + 'background:var(--bg,#fff);color:var(--fg,#222);'
    + 'border:1px solid var(--line,#ddd);border-radius:12px;'
    + 'box-shadow:0 8px 28px rgba(0,0,0,.16);overflow:hidden}'
    + '#wbsc.min .bd{display:none}'
    + '#wbsc .hd{display:flex;align-items:center;gap:8px;padding:9px 12px;cursor:pointer;'
    + 'border-bottom:1px solid var(--line,#eee);font-weight:600;font-size:12.5px}'
    + '#wbsc.min .hd{border-bottom:none}'
    + '#wbsc .hd .sp{flex:1}'
    + '#wbsc .hd .tot{font-weight:400;color:var(--muted,#777);font-size:11.5px}'
    + '#wbsc .hd .tg{color:var(--muted,#999);font-size:14px;user-select:none}'
    + '#wbsc .bd{max-height:46vh;overflow:auto}'
    + '#wbsc table{width:100%;border-collapse:collapse}'
    + '#wbsc th{position:sticky;top:0;background:var(--bg,#fff);text-align:left;'
    + 'font-weight:500;color:var(--muted,#888);font-size:11px;padding:6px 10px;'
    + 'border-bottom:1px solid var(--line,#eee)}'
    + '#wbsc td{padding:6px 10px;border-bottom:1px solid var(--line,#f2f2f2);'
    + 'white-space:nowrap;font-variant-numeric:tabular-nums}'
    + '#wbsc tr.top td{font-weight:600}'
    + '#wbsc .cr{text-align:right}'
    + '#wbsc .ft{padding:7px 12px;color:var(--muted,#888);font-size:11px;'
    + 'display:flex;justify-content:space-between;align-items:center}'
    + '#wbsc button{font:inherit;font-size:11px;padding:3px 9px;border-radius:6px;'
    + 'border:1px solid var(--line,#ddd);background:transparent;color:inherit;cursor:pointer}'
    + '#wbsc button:hover{border-color:var(--fg,#888)}'
    + '#wbsc .empty{padding:16px 12px;color:var(--muted,#999);text-align:center}';

  var style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  var box = document.createElement('div');
  box.id = 'wbsc';
  box.innerHTML = ''
    + '<div class="hd"><span>对话积分</span><span class="tot"></span>'
    + '<span class="sp"></span><span class="tg">−</span></div>'
    + '<div class="bd"><div class="empty">加载中…</div></div>'
    + '<div class="ft"><span>按对话归集，积分来自上游</span>'
    + '<button type="button">刷新</button></div>';

  function ready(fn) {
    if (document.body) fn();
    else document.addEventListener('DOMContentLoaded', fn);
  }

  ready(function () {
    document.body.appendChild(box);

    var bd = box.querySelector('.bd');
    var tot = box.querySelector('.tot');
    var tg = box.querySelector('.tg');

    box.querySelector('.hd').addEventListener('click', function () {
      box.classList.toggle('min');
      tg.textContent = box.classList.contains('min') ? '+' : '−';
    });

    function fmt(n) {
      if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
      if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
      return String(n);
    }

    function fmtC(v) {
      if (!v) return '0';
      return (Math.round(v * 100) / 100).toString();
    }

    function esc(s) {
      return String(s).replace(/[&<>"]/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
      });
    }

    function draw(data) {
      var rows = (data && data.conversations) || [];
      if (!rows.length) {
        bd.innerHTML = '<div class="empty">还没有对话记录</div>';
        tot.textContent = '';
        return;
      }
      tot.textContent = '合计 ' + fmtC(data.total_credit) + ' 积分';
      var h = '<table><thead><tr><th>结束</th><th>请求</th><th class="cr">tokens</th>'
        + '<th class="cr">积分</th></tr></thead><tbody>';
      for (var i = 0; i < rows.length; i++) {
        var r = rows[i];
        h += '<tr' + (i === 0 ? ' class="top"' : '') + '>'
          + '<td>' + esc(r.when) + '</td>'
          + '<td>' + r.requests + '</td>'
          + '<td class="cr">' + fmt(r.tokens) + '</td>'
          + '<td class="cr">' + fmtC(r.credit) + '</td>'
          + '</tr>';
      }
      h += '</tbody></table>';
      bd.innerHTML = h;
    }

    function load() {
      fetch('/sessions', { headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
        .then(draw)
        .catch(function (e) {
          bd.innerHTML = '<div class="empty">读不到数据（' + e + '）</div>';
        });
    }

    box.querySelector('.ft button').addEventListener('click', function (ev) {
      ev.stopPropagation();
      load();
    });

    load();
    setInterval(load, REFRESH_MS);
  });
})();
