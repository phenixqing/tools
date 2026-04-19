/* ── Options Portfolio Analyzer ─────────────────────────────────────────── */

const _DAYS = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
function fmtDate(dateStr) {
  const d = new Date(dateStr + "T00:00:00");
  return `${dateStr} ${_DAYS[d.getDay()]}`;
}

const fmt = {
  usd:  v => v == null ? "—" : new Intl.NumberFormat("en-US",{style:"currency",currency:"USD",maximumFractionDigits:0}).format(v),
  usd2: v => v == null ? "—" : new Intl.NumberFormat("en-US",{style:"currency",currency:"USD",minimumFractionDigits:2,maximumFractionDigits:2}).format(v),
  pct:  v => v == null ? "—" : (v>=0?"+":"")+v.toFixed(2)+"%",
  num:  (v,d=4) => v==null?"—":v.toFixed(d),
  sign: v => v>=0?"pos":"neg",
};

/* ── Plotly theme ────────────────────────────────────────── */
const PL = {
  paper_bgcolor:"transparent", plot_bgcolor:"transparent",
  font:{ color:"#e2e8f0", family:"Inter,system-ui,sans-serif", size:11 },
  xaxis:{ gridcolor:"#2a2f50", zerolinecolor:"#3d4470", tickfont:{size:10} },
  yaxis:{ gridcolor:"#2a2f50", zerolinecolor:"#3d4470", tickfont:{size:10} },
  legend:{ bgcolor:"rgba(26,29,46,.9)", bordercolor:"#2e3250", borderwidth:1 },
};
const CFG = { responsive:true, displayModeBar:false };
const zeroLine = () => ({ type:"line", x0:0, x1:1, xref:"paper", y0:0, y1:0,
  line:{ color:"#4a5180", width:1.5, dash:"dot" } });

/* ── State ───────────────────────────────────────────────── */
let scenarioData       = null;
let selectedKey        = "aggregate";
let priceType          = "option_pnl";
let comparisonRows     = [];        // [{date, price}]
let currentFile        = null;
let includedSymbols    = new Set();
let currentPortfolioValue = 0;      // for "Total Assets" in comparison
// hover-panel data (updated each renderScenario call)
let _hoverMatrix = null, _hoverPortMatrix = null, _hoverDates = null, _hoverXLabels = null;
let _hoverCallMatrix = null, _hoverPutMatrix = null;
let _hoverCallCost = 0, _hoverPutCost = 0;
// live price
let _liveEnabled = false;
let _livePrice   = null;
let _priceSSE    = null;   // EventSource instance

/* ── Fetch ───────────────────────────────────────────────── */
async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) { const t = await r.text(); throw new Error(t || `${r.status}`); }
  return r.json();
}

/* ────────────────────────────────────────────────────────── */
/*  DATA SOURCE BAR (radio-style exclusive selection)         */
/* ────────────────────────────────────────────────────────── */
let _activeSource = "fidelity";   // "fidelity" | "upload"

function _setDsStatus(text, cls = "") {
  const el = document.getElementById("ds-status");
  if (!el) return;
  el.textContent = text;
  el.className   = "ds-status" + (cls ? " " + cls : "");
  el.title       = text;
}

function _setDsActive(source) {
  _activeSource = source;
  document.getElementById("ds-fidelity")?.classList.toggle("active", source === "fidelity");
  document.getElementById("ds-upload")?.classList.toggle("active", source === "upload");
}

async function _doUpload(file) {
  if (!file) return;
  _setDsStatus("⏳ Uploading…", "running");
  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await fetchJSON("/api/upload", { method: "POST", body: fd });
    currentFile = res.filename;
    _setDsStatus(`✓ ${res.filename}`, "done");
    document.getElementById("holdings-file-tag").textContent = res.filename;
    comparisonRows = [];
    await reloadAll();
  } catch(e) {
    _setDsStatus(`✗ ${e.message}`, "error");
  }
}

function setupDataSource() {
  const input    = document.getElementById("csv-file-input");
  const fidBtn   = document.getElementById("ds-fidelity");
  const upBtn    = document.getElementById("ds-upload");

  // Fidelity button: select it (if not active) OR trigger sync (if already active)
  fidBtn?.addEventListener("click", async () => {
    if (_activeSource !== "fidelity") {
      _setDsActive("fidelity");
      _setDsStatus("");
      return;
    }
    // Already selected — trigger sync
    _triggerFidelitySync();
  });

  // Upload button: select it and open file picker immediately
  upBtn?.addEventListener("click", () => {
    _setDsActive("upload");
    _setDsStatus("");
    input.value = "";   // allow re-picking same file
    input.click();
  });

  input.addEventListener("change", () => _doUpload(input.files[0]));

  // Drag & drop anywhere in the portfolio tab
  const zone = document.getElementById("upload-zone");
  if (zone) {
    zone.addEventListener("dragover",  e => { e.preventDefault(); zone.classList.add("drag-over"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
    zone.addEventListener("drop", e => {
      e.preventDefault();
      zone.classList.remove("drag-over");
      _setDsActive("upload");
      _doUpload(e.dataTransfer.files[0]);
    });
  }
}

/* ────────────────────────────────────────────────────────── */
/*  FIDELITY SYNC                                             */
/* ────────────────────────────────────────────────────────── */
let _syncPollTimer = null;

function _fmtAgo(secs) {
  if (secs < 60)   return `${Math.round(secs)}s ago`;
  if (secs < 3600) return `${Math.round(secs/60)}m ago`;
  return `${Math.round(secs/3600)}h ago`;
}

async function _checkSyncStatus(pollOnRunning = false) {
  try {
    const s   = await fetchJSON("/api/sync/status");
    const btn = document.getElementById("ds-fidelity");

    if (!s.creds_configured && btn) {
      btn.title = "Set FIDELITY_USERNAME + FIDELITY_PASSWORD env vars to enable";
    }

    if (s.status === "running") {
      btn?.classList.add("ds-running");
      // Only update status text when Fidelity is the active source
      if (_activeSource === "fidelity") {
        _setDsStatus(s.message || "Syncing…", "running");
      }
      if (pollOnRunning && !_syncPollTimer) {
        _syncPollTimer = setInterval(() => _checkSyncStatus(true), 3000);
      }
    } else {
      btn?.classList.remove("ds-running");
      clearInterval(_syncPollTimer); _syncPollTimer = null;

      if (_activeSource === "fidelity") {
        if (s.status === "done") {
          const ago = s.age_secs != null ? ` (${_fmtAgo(s.age_secs)})` : "";
          _setDsStatus(`✓ Synced${ago}`, "done");
          // Auto-reload data if the sync just completed (within 5 s)
          if (s.age_secs != null && s.age_secs < 5) { await reloadAll(); }
        } else if (s.status === "error") {
          _setDsStatus(`✗ ${s.last_error?.split("\n")[0] || "Error"}`, "error");
        } else {
          _setDsStatus(s.creds_configured ? "" : "⚠ creds not set");
        }
      }
    }
    return s;
  } catch(_) { return null; }
}

async function _triggerFidelitySync() {
  const btn = document.getElementById("ds-fidelity");
  btn?.classList.add("ds-running");
  _setDsStatus("Starting…", "running");
  try {
    const res = await fetchJSON("/api/sync/fidelity", { method: "POST" });
    if (res.status === "already_running") {
      _setDsStatus("Already running…", "running");
    }
    _checkSyncStatus(true);
    _syncPollTimer = _syncPollTimer || setInterval(() => _checkSyncStatus(true), 3000);
  } catch(e) {
    _setDsStatus(`✗ ${e.message}`, "error");
    btn?.classList.remove("ds-running");
  }
}

/* ────────────────────────────────────────────────────────── */
/*  SUMMARY CARDS                                             */
/* ────────────────────────────────────────────────────────── */
async function loadSummary() {
  const d = await fetchJSON("/api/portfolio/summary");
  currentPortfolioValue = d.total_value;
  document.getElementById("ref-date-badge").textContent = `📅 ${d.reference_date}`;
  document.getElementById("card-total").textContent = fmt.usd(d.total_value);
  document.getElementById("card-avgo").textContent  = fmt.usd2(d.underlying_price);
  document.getElementById("card-opt").textContent   = fmt.usd2(d.options_value);
  document.getElementById("card-stk").textContent   = fmt.usd(d.stocks_value);
  document.getElementById("card-cash").textContent  = fmt.usd(d.cash_value);
  const pnlEl = document.getElementById("card-pnl");
  pnlEl.textContent = fmt.usd2(d.options_unrealized_pnl);
  pnlEl.className = "value " + fmt.sign(d.options_unrealized_pnl);
}

/* ────────────────────────────────────────────────────────── */
/*  HOLDINGS                                                  */
/* ────────────────────────────────────────────────────────── */
async function loadHoldings() {
  const [h, opts] = await Promise.all([
    fetchJSON("/api/holdings"),
    fetchJSON("/api/options"),
  ]);

  // Stock cards
  const grid = document.getElementById("stock-grid");
  grid.innerHTML = "";
  for (const s of h.stocks) {
    grid.insertAdjacentHTML("beforeend", `
      <div class="stock-card">
        <div class="s-sym">${s.symbol}</div>
        <div class="s-desc" title="${s.description}">${s.description}</div>
        <div class="s-row">
          <span>${Number(s.quantity).toLocaleString()} shares</span>
          <span class="s-val">${fmt.usd2(s.market_price)}</span>
        </div>
        <div class="s-row">
          <span style="color:var(--muted);font-size:.7rem">${s.account}</span>
          <span class="s-val">${fmt.usd(s.current_value)}</span>
        </div>
      </div>`);
  }

  // Shares card
  const avgoShares = h.stocks.find(s=>s.symbol==="AVGO");
  document.getElementById("card-shares").textContent =
    avgoShares ? Number(avgoShares.quantity).toLocaleString() : "—";

  // Options table — also initialise includedSymbols
  includedSymbols = new Set(opts.map(o => o.symbol.trim()));

  const tbody = document.getElementById("opt-tbody");
  tbody.innerHTML = "";
  for (const o of opts) {
    const g   = o.greeks;
    const dte = o.days_to_expiry;
    const sym = o.symbol.trim();
    const gl  = o.gain_loss;
    const glp = o.gain_loss_pct;
    const cb  = o.cost_basis;
    const cbAvg = (cb != null && o.quantity) ? cb / Math.abs(o.quantity) / 100 : null;
    tbody.insertAdjacentHTML("beforeend", `
      <tr data-sym="${sym}">
        <td style="text-align:center"><input type="checkbox" class="opt-chk" data-sym="${sym}" checked/></td>
        <td><b>${o.description}</b></td>
        <td><span class="acc-tag">${o.account}</span></td>
        <td><span class="tag tag-${o.option_type}">${o.option_type.toUpperCase()}</span><span class="tag tag-short">SHORT</span></td>
        <td>$${o.strike.toFixed(2)}</td>
        <td>${o.expiry}</td>
        <td class="${dte<=3?"neg":""}">${dte}d</td>
        <td>${o.quantity}</td>
        <td>$${fmt.num(o.market_price,2)}</td>
        <td class="${fmt.sign(o.current_value)}">${fmt.usd2(o.current_value)}</td>
        <td>${cbAvg != null ? "$"+fmt.num(cbAvg,2) : "—"}</td>
        <td>${cb != null ? fmt.usd2(cb) : "—"}</td>
        <td class="${gl!=null?fmt.sign(gl):""}">${gl!=null?fmt.usd2(gl):"—"}</td>
        <td class="${glp!=null?fmt.sign(glp):""}">${glp!=null?fmt.pct(glp):"—"}</td>
        <td>${fmt.pct(o.implied_vol_pct)}</td>
        <td class="${fmt.sign(g.delta)}">${fmt.num(g.delta,4)}</td>
        <td>${fmt.num(g.gamma,5)}</td>
        <td class="${fmt.sign(g.theta)}">${fmt.usd2(g.theta)}</td>
        <td>${fmt.usd2(g.vega)}</td>
        <td>${fmt.usd2(g.rho)}</td>
      </tr>`);
  }

  // Checkbox toggle logic
  tbody.querySelectorAll(".opt-chk").forEach(chk => {
    chk.addEventListener("change", () => {
      const sym = chk.dataset.sym;
      chk.checked ? includedSymbols.add(sym) : includedSymbols.delete(sym);
      chk.closest("tr").style.opacity = chk.checked ? "1" : "0.4";
      if (scenarioData) { renderScenario(); renderComparisonTable(); }
    });
  });

  // "Check all" header checkbox
  document.getElementById("chk-all").addEventListener("change", function() {
    tbody.querySelectorAll(".opt-chk").forEach(chk => {
      chk.checked = this.checked;
      const sym = chk.dataset.sym;
      this.checked ? includedSymbols.add(sym) : includedSymbols.delete(sym);
      chk.closest("tr").style.opacity = this.checked ? "1" : "0.4";
    });
    if (scenarioData) { renderScenario(); renderComparisonTable(); }
  });

  document.getElementById("holdings-spinner").style.display  = "none";
  document.getElementById("holdings-content").style.display  = "block";
}

/* ────────────────────────────────────────────────────────── */
/*  SCENARIOS                                                 */
/* ────────────────────────────────────────────────────────── */
async function loadScenarios(steps=21) {
  document.getElementById("sc-spinner").style.display       = "block";
  document.getElementById("scenario-content").style.display = "none";

  scenarioData = await fetchJSON(`/api/options/scenarios?price_steps=${steps}`);

  // Update date input bounds in comparison tool
  const allDates = scenarioData.aggregate.dates;
  const cmpDate  = document.getElementById("cmp-date");
  cmpDate.min = allDates[0];
  cmpDate.max = allDates[allDates.length - 1];
  if (!cmpDate.value) cmpDate.value = allDates[Math.floor(allDates.length / 2)];

  // Update price input placeholder
  const cmpPrice = document.getElementById("cmp-price");
  if (!cmpPrice.value) cmpPrice.value = scenarioData.underlying_price.toFixed(2);

  // Populate position dropdown
  const sel = document.getElementById("opt-select");
  while (sel.options.length > 1) sel.remove(1);
  for (const o of scenarioData.options) {
    const opt = document.createElement("option");
    opt.value = o.symbol.trim();
    opt.textContent = `${o.description}  (qty ${o.quantity})`;
    sel.appendChild(opt);
  }

  document.getElementById("comparison-section").style.display = "block";
  renderScenario();
  renderComparisonTable();   // refresh if already has rows
}

/* ── Recompute aggregate from includedSymbols ────────────── */
function recomputeAggregate() {
  const sd       = scenarioData;
  const allDates = sd.aggregate.dates;
  const n        = sd.price_range.length;
  const agg      = allDates.map(() => new Array(n).fill(0));

  for (const o of sd.options) {
    if (!includedSymbols.has(o.symbol.trim())) continue;
    const dateMap = {};
    o.dates.forEach((d, i) => dateMap[d] = i);
    const last = o.pnl_matrix[o.pnl_matrix.length - 1];
    for (let ai = 0; ai < allDates.length; ai++) {
      const src = allDates[ai] in dateMap ? o.pnl_matrix[dateMap[allDates[ai]]] : last;
      for (let pi = 0; pi < n; pi++) agg[ai][pi] += src[pi];
    }
  }

  const port = agg.map(row => row.map((v, pi) => v + sd.stock_delta[pi]));
  return { agg, port };
}

/* ── Active P&L matrix ───────────────────────────────────── */
function getMatrix(src) {
  const isAgg = selectedKey === "aggregate";
  if (!isAgg) return src.pnl_matrix;
  const { agg, port } = recomputeAggregate();
  return priceType === "portfolio_pnl" ? port : agg;
}

/* ── Render charts ───────────────────────────────────────── */
function renderScenario() {
  if (!scenarioData) return;

  const sd     = scenarioData;
  const isAgg  = selectedKey === "aggregate";
  const pctLbls= sd.pct_labels;
  const prices = sd.price_range;
  const n      = prices.length;
  const S_curr = sd.underlying_price;

  let src;
  if (isAgg) {
    src = { dates: sd.aggregate.dates, pnl_matrix: getMatrix(null) };
    document.getElementById("greeks-display").style.display = "none";
  } else {
    src = sd.options.find(o => o.symbol.trim() === selectedKey);
    if (!src) return;
    document.getElementById("greeks-display").style.display = "grid";
    const g0 = src.greeks_over_time[0];
    document.getElementById("g-delta").textContent = fmt.num(g0.delta,4);
    document.getElementById("g-gamma").textContent = fmt.num(g0.gamma,6);
    document.getElementById("g-theta").textContent = fmt.usd2(g0.theta);
    document.getElementById("g-vega").textContent  = fmt.usd2(g0.vega);
    document.getElementById("g-iv").textContent    = fmt.pct(src.implied_vol_pct);
  }

  const dates  = src.dates;
  const matrix = src.pnl_matrix;
  const yLbl   = priceType==="portfolio_pnl"&&isAgg ? "Portfolio P&L ($)" : "Option P&L ($)";

  // Combined labels: "+5.0%  $390" used on all chart axes
  const xLabels = pctLbls.map((pct, i) => `${pct}  $${prices[i].toFixed(2)}`);

  // Store for hover panel
  _hoverMatrix  = matrix;
  _hoverDates   = dates;
  _hoverXLabels = xLabels;
  // Always store portfolio P&L and call/put splits for hover columns
  if (isAgg) {
    const { port } = recomputeAggregate();
    _hoverPortMatrix = port;

    const sd2 = scenarioData;
    const n2  = sd2.price_range.length;
    const callAgg = sd2.aggregate.dates.map(() => new Array(n2).fill(0));
    const putAgg  = sd2.aggregate.dates.map(() => new Array(n2).fill(0));
    let callCost = 0, putCost = 0;

    for (const o of sd2.options) {
      if (!includedSymbols.has(o.symbol.trim())) continue;
      const isCall = o.option_type === "call";
      if (isCall) callCost += Math.abs(o.cost_basis || 0);
      else        putCost  += Math.abs(o.cost_basis || 0);
      const dateMap = {};
      o.dates.forEach((d, i) => dateMap[d] = i);
      const last = o.pnl_matrix[o.pnl_matrix.length - 1];
      const target = isCall ? callAgg : putAgg;
      for (let ai = 0; ai < sd2.aggregate.dates.length; ai++) {
        const dl  = sd2.aggregate.dates[ai];
        const src = dl in dateMap ? o.pnl_matrix[dateMap[dl]] : last;
        for (let pi = 0; pi < n2; pi++) target[ai][pi] += src[pi];
      }
    }
    _hoverCallMatrix = callAgg;
    _hoverPutMatrix  = putAgg;
    _hoverCallCost   = callCost;
    _hoverPutCost    = putCost;
  } else {
    _hoverPortMatrix = null;
    _hoverCallMatrix = null;
    _hoverPutMatrix  = null;
  }

  /* ── 1. Price curves by date ─────────────────────────────── */
  const curveIdxs  = Array.from({length:9}, (_,i) => Math.round(i*(n-1)/8));
  const COLORS = ["#ef4444","#f97316","#eab308","#a3e635","#94a3b8","#34d399","#22d3ee","#60a5fa","#818cf8"];

  const curveTraces = curveIdxs.map((pi, ci) => ({
    type:"scatter", mode:"lines",
    name: xLabels[pi],
    x: dates,
    y: matrix.map(row => row[pi]),
    line:{ color:COLORS[ci], width: pi===Math.floor(n/2) ? 2.5 : 1.8 },
    hovertemplate:`<b>${xLabels[pi]}</b><br>%{x}<br>P&L: <b>$%{y:,.0f}</b><extra></extra>`,
  }));

  // Anchor markers — one dot per date at y=0, easy click targets
  const anchorTrace = {
    type:"scatter", mode:"markers+text",
    name:"__anchors__", showlegend:false,
    x: dates, y: new Array(dates.length).fill(0),
    yaxis:"y2",
    marker:{ size:14, color:"rgba(99,102,241,0.2)", line:{color:"#6366f1",width:1.5}, symbol:"circle" },
    text: dates.map(d => d.slice(5)),
    textposition:"top center",
    textfont:{ size:8.5, color:"#8892a4" },
    hoverinfo:"skip",
  };

  document.getElementById("curves-title").textContent = isAgg
    ? `${priceType==="portfolio_pnl"?"Portfolio":"Options Aggregate"} P&L by Date`
    : `${src.description} — P&L by Date`;

  const xaxisCurves = dates.length < 10
    ? { ...PL.xaxis, title:{text:"Date",standoff:8}, tickvals: dates, ticktext: dates.map(() => ""), showticklabels: true }
    : { ...PL.xaxis, title:{text:"Date",standoff:8} };

  Plotly.react("price-curves", [...curveTraces, anchorTrace], {
    ...PL,
    margin:{t:12,b:55,l:72,r:20},
    xaxis: xaxisCurves,
    yaxis:{...PL.yaxis, title:{text:yLbl,standoff:8}, tickformat:",.0f"},
    yaxis2:{ overlaying:"y", side:"right", range:[0,10], fixedrange:true,
              showgrid:false, showticklabels:false, zeroline:false },
    shapes:[zeroLine()],
    legend:{...PL.legend, orientation:"v", x:1.01, y:1, font:{size:9.5}},
    hovermode:"x",
  }, CFG);

  // Initialize hover live card hint
  const liveCardInit = document.getElementById("hover-live-card");
  if (liveCardInit && !liveCardInit._initialized) {
    liveCardInit._initialized = true;
    liveCardInit.innerHTML = '<span class="hp-hint" style="margin:auto">↗ Hover or click a date to compare all price scenarios</span>';
  }

  // Shared function to render Scenario Preview for a given date index
  function renderPreviewForDate(dIdx, pinned) {
    const date = _hoverDates[dIdx];
    const row  = _hoverMatrix[dIdx];
    const liveCard = document.getElementById("hover-live-card");
    if (!liveCard) return;
    const portRow  = _hoverPortMatrix ? _hoverPortMatrix[dIdx] : null;
    const callRow  = _hoverCallMatrix ? _hoverCallMatrix[dIdx] : null;
    const putRow   = _hoverPutMatrix  ? _hoverPutMatrix[dIdx]  : null;
    const hasSplit = callRow && putRow;
    const hasPort  = !!portRow;
    liveCard.innerHTML = `
      <div class="hp-date">📅 ${fmtDate(date)}${pinned?' <span class="hp-pinned">📌 pinned</span>':''}</div>
      <table class="hp-table">
        <thead><tr>
          <th>Change</th><th>AVGO Price</th><th>Option P&L</th>
          ${hasSplit ? "<th>Call P&L</th><th>Put P&L</th>" : ""}
          ${hasPort  ? "<th>Total Assets</th><th>Assets Δ%</th>" : ""}
        </tr></thead>
        <tbody>${row.map((pnl, pi) => {
          const parts = (_hoverXLabels[pi]||"").split("  ");
          const pnlPct = _hoverCallCost + _hoverPutCost
            ? (pnl / (_hoverCallCost + _hoverPutCost) * 100) : null;
          let splitCols = "";
          if (hasSplit) {
            const cp = callRow[pi]; const pp = putRow[pi];
            const cpPct = _hoverCallCost ? (cp / _hoverCallCost * 100) : null;
            const ppPct = _hoverPutCost  ? (pp / _hoverPutCost  * 100) : null;
            splitCols = `
              <td class="${cp>=0?"hp-pos":"hp-neg"}">${fmt.usd2(cp)}<span class="hp-pct">${cpPct!=null?" "+fmt.pct(cpPct):""}</span></td>
              <td class="${pp>=0?"hp-pos":"hp-neg"}">${fmt.usd2(pp)}<span class="hp-pct">${ppPct!=null?" "+fmt.pct(ppPct):""}</span></td>`;
          }
          let portCol = "";
          if (hasPort) {
            const portPnl   = portRow[pi];
            const total     = currentPortfolioValue + portPnl;
            const changePct = currentPortfolioValue ? (portPnl / Math.abs(currentPortfolioValue) * 100) : null;
            portCol = `<td class="${portPnl>=0?"hp-pos":"hp-neg"}">${fmt.usd(total)}</td>
                       <td class="${portPnl>=0?"hp-pos":"hp-neg"}">${changePct!=null?fmt.pct(changePct):"—"}</td>`;
          }
          return `<tr>
            <td>${parts[0]||""}</td>
            <td>${parts[1]||""}</td>
            <td class="${pnl>=0?"hp-pos":"hp-neg"}">${fmt.usd2(pnl)}<span class="hp-pct">${pnlPct!=null?" "+fmt.pct(pnlPct):""}</span></td>
            ${splitCols}${portCol}
          </tr>`;
        }).join("")}</tbody>
      </table>`;
  }

  // Set up hover / click on chart (once per element)
  const curveEl = document.getElementById("price-curves");
  if (curveEl && !curveEl._hpReady) {
    curveEl._hpReady = true;
    let _pinnedDIdx = null;

    curveEl.on("plotly_click", data => {
      if (!_hoverMatrix || !_hoverDates) return;
      const date = data.points[0].x;
      const dIdx = _hoverDates.indexOf(date);
      if (dIdx < 0) return;
      if (_pinnedDIdx === dIdx) {
        _pinnedDIdx = null;  // click again to unpin
        const liveCard = document.getElementById("hover-live-card");
        if (liveCard) liveCard.innerHTML = '<span class="hp-hint" style="margin:auto">↗ Hover or click a date to compare all price scenarios</span>';
      } else {
        _pinnedDIdx = dIdx;
        renderPreviewForDate(dIdx, true);
      }
    });

    curveEl.on("plotly_hover", data => {
      if (!_hoverMatrix || !_hoverDates || _pinnedDIdx !== null) return;
      const date = data.points[0].x;
      const dIdx = _hoverDates.indexOf(date);
      if (dIdx < 0) return;
      renderPreviewForDate(dIdx, false);
    });

    curveEl.on("plotly_unhover", () => {
      if (_pinnedDIdx !== null) return;
      const liveCard = document.getElementById("hover-live-card");
      if (liveCard) liveCard.innerHTML = '<span class="hp-hint" style="margin:auto">↗ Hover or click a date to compare all price scenarios</span>';
    });
  }

  /* ── 2. Heatmap ──────────────────────────────────────────── */
  const flat   = matrix.flat();
  const absMax = Math.max(Math.abs(Math.min(...flat)), Math.abs(Math.max(...flat)));
  const zB     = absMax < 200 ? 200 : absMax;

  Plotly.react("heatmap", [{
    type:"heatmap",
    x: xLabels, y: dates, z: matrix,
    zmin:-zB, zmax:zB,
    colorscale:[
      [0,"#991b1b"],[0.35,"#ef4444"],[0.47,"#fca5a5"],
      [0.5,"#1e2235"],
      [0.53,"#86efac"],[0.65,"#22c55e"],[1,"#14532d"],
    ],
    colorbar:{
      title:{text:"P&L ($)",font:{color:"#8892a4",size:10}},
      tickfont:{color:"#8892a4",size:9}, thickness:11, len:.8, tickformat:",.0f",
    },
    hovertemplate:"<b>%{x}</b><br>Date: %{y}<br>P&L: <b>$%{z:,.0f}</b><extra></extra>",
    xgap:1, ygap:1,
  }], {
    ...PL,
    margin:{t:12,b:60,l:80,r:60},
    xaxis:{...PL.xaxis, title:{text:"AVGO Price Change",standoff:8}, tickangle:-40},
    yaxis:{...PL.yaxis, title:{text:"Date",standoff:8}, autorange:"reversed"},
  }, CFG);

  /* ── 3. Payoff at expiry ─────────────────────────────────── */
  const lastRow = matrix[matrix.length - 1];
  const midIdx  = Math.round((n-1)/2);

  Plotly.react("payoff", [{
    type:"bar", x:xLabels, y:lastRow,
    marker:{ color:lastRow.map(v=>v>=0?"#22c55e":"#ef4444"),
             line:{color:lastRow.map(v=>v>=0?"#16a34a":"#dc2626"),width:.5} },
    hovertemplate:"<b>%{x}</b><br>P&L at expiry: <b>$%{y:,.0f}</b><extra></extra>",
  }], {
    ...PL,
    margin:{t:12,b:70,l:78,r:20},
    xaxis:{...PL.xaxis, title:{text:"AVGO Price Change",standoff:8}, tickangle:-35},
    yaxis:{...PL.yaxis, title:{text:yLbl,standoff:8}, tickformat:",.0f"},
    shapes:[zeroLine(), {
      type:"line",
      x0:xLabels[midIdx], x1:xLabels[midIdx], y0:0, y1:1, yref:"paper",
      line:{color:"#f59e0b",width:2,dash:"dash"},
    }],
    annotations:[{
      x:xLabels[midIdx], y:1.03, yref:"paper",
      text:"Current", showarrow:false,
      font:{color:"#f59e0b",size:9.5}, xanchor:"center",
    }],
  }, CFG);

  document.getElementById("sc-spinner").style.display       = "none";
  document.getElementById("scenario-content").style.display = "block";
}

/* ────────────────────────────────────────────────────────── */
/*  COMPARISON TABLE                                          */
/* ────────────────────────────────────────────────────────── */

// Find nearest index in a sorted array
function nearestIdx(arr, val) {
  return arr.reduce((best,v,i) => Math.abs(v-val) < Math.abs(arr[best]-val) ? i : best, 0);
}

async function lookupPnL(date, price) {
  if (!scenarioData) return null;
  const sd = scenarioData;

  // Find nearest date in aggregate list
  const ts   = new Date(date).getTime();
  const dIdx = sd.aggregate.dates.reduce((best, d, i) =>
    Math.abs(new Date(d) - ts) < Math.abs(new Date(sd.aggregate.dates[best]) - ts) ? i : best, 0);
  const actualDate = sd.aggregate.dates[dIdx];

  // Exact BS computation for any arbitrary price
  const res = await fetchJSON(`/api/options/pnl_point?price=${price}&target_date=${actualDate}`);

  // Map opt_details to per-option structure with included flag
  let aggPnl = 0;
  const optDetails = sd.options.map((o, oi) => {
    const d       = res.opt_details[oi];
    const included = includedSymbols.has(o.symbol.trim());
    if (included) aggPnl += d.pnl;
    return { pnl: d.pnl, scenPrice: d.scen_price, scenValue: d.scen_value, included };
  });

  const portPnl = aggPnl + res.stock_delta;

  return {
    actualDate,
    actualPrice: price,
    pctChange:   ((price - sd.underlying_price) / sd.underlying_price) * 100,
    aggPnl,
    portPnl,
    optDetails,
  };
}

async function renderComparisonTable() {
  if (!scenarioData) return;

  const cards = document.getElementById("cmp-cards");
  const empty = document.getElementById("cmp-empty");

  if (comparisonRows.length === 0) {
    cards.innerHTML    = "";
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";
  cards.innerHTML = "";

  const opts = scenarioData.options;

  for (let ri = 0; ri < comparisonRows.length; ri++) {
    const row = comparisonRows[ri];
    const lk  = await lookupPnL(row.date, row.price);
    if (!lk) continue;

    const pctSign = lk.pctChange >= 0 ? "pos" : "neg";

    // Derive % figures for header
    const sd        = scenarioData;
    const inclCostBasis = sd.options
      .filter(o => includedSymbols.has(o.symbol.trim()))
      .reduce((s, o) => s + Math.abs(o.cost_basis || 0), 0);
    const aggPnlPct  = inclCostBasis ? (lk.aggPnl / inclCostBasis * 100) : null;
    const portPnlPct = currentPortfolioValue ? (lk.portPnl / Math.abs(currentPortfolioValue) * 100) : null;
    const totalAssets = currentPortfolioValue + lk.portPnl;
    const totalAssetsPct = currentPortfolioValue ? (lk.portPnl / Math.abs(currentPortfolioValue) * 100) : null;

    // One table row per option
    const optRows = opts.map((o, oi) => {
      const d      = lk.optDetails[oi];
      const incl   = includedSymbols.has(o.symbol.trim());
      const cb     = o.cost_basis;
      const cbAvg  = (cb != null && o.quantity) ? cb / Math.abs(o.quantity) / 100 : null;
      const pnlPct = cb ? (d.pnl / Math.abs(cb) * 100) : null;
      const rowStyle = incl ? "" : 'style="opacity:.35"';
      return `<tr ${rowStyle}>
        <td><b>${o.description}</b></td>
        <td><span class="acc-tag">${o.account}</span></td>
        <td>
          <span class="tag tag-${o.option_type}">${o.option_type.toUpperCase()}</span>
          <span class="tag tag-short">SHORT</span>
        </td>
        <td>$${o.strike.toFixed(2)}</td>
        <td>${o.expiry}</td>
        <td>${o.days_to_expiry}d</td>
        <td>${o.quantity}</td>
        <td>${cbAvg != null ? "$"+fmt.num(cbAvg,2) : "—"}</td>
        <td>${cb != null ? fmt.usd2(cb) : "—"}</td>
        <td>$${fmt.num(d.scenPrice, 2)}</td>
        <td class="${fmt.sign(d.scenValue)}">${fmt.usd2(d.scenValue)}</td>
        <td class="${d.pnl>=0?"pnl-pos":"pnl-neg"}">${fmt.usd2(d.pnl)}</td>
        <td class="${d.pnl>=0?"pnl-pos":"pnl-neg"}" style="font-size:.75rem">${pnlPct!=null?fmt.pct(pnlPct):"—"}</td>
      </tr>`;
    }).join("");

    const allDates = scenarioData.aggregate.dates;
    const card = document.createElement("div");
    card.className = "cmp-card";
    card.innerHTML = `
      <div class="cmp-card-header">
        <div class="cmp-headline">
          <span>📅 ${fmtDate(lk.actualDate)}</span>
          <span class="cmp-sep">·</span>
          <span>AVGO ${fmt.usd2(lk.actualPrice)}</span>
          <span class="cmp-pct-badge ${pctSign}">${fmt.pct(lk.pctChange)}</span>
        </div>
        <div class="cmp-edit-bar">
          <input type="date" class="cmp-input cmp-edit-date" value="${row.date}" min="${allDates[0]}" max="${allDates[allDates.length-1]}"/>
          <input type="number" class="cmp-input cmp-edit-price" value="${row.price}" step="0.5"/>
          <button class="cmp-update-btn btn-accent" data-idx="${ri}">↻ Update</button>
        </div>
        <div class="cmp-totals">
          <span>Option P&L:
            <b class="${fmt.sign(lk.aggPnl)}">${fmt.usd2(lk.aggPnl)}</b>
            <span class="cmp-pct-inline ${fmt.sign(lk.aggPnl)}">${aggPnlPct!=null?fmt.pct(aggPnlPct):""}</span>
          </span>
          <span>Portfolio P&L:
            <b class="${fmt.sign(lk.portPnl)}">${fmt.usd2(lk.portPnl)}</b>
            <span class="cmp-pct-inline ${fmt.sign(lk.portPnl)}">${portPnlPct!=null?fmt.pct(portPnlPct):""}</span>
          </span>
          <span>Total Assets:
            <b class="${fmt.sign(lk.portPnl)}">${fmt.usd(totalAssets)}</b>
            <span class="cmp-pct-inline ${fmt.sign(lk.portPnl)}">${totalAssetsPct!=null?fmt.pct(totalAssetsPct):""}</span>
          </span>
        </div>
        <button class="cmp-del-card" data-idx="${ri}">✕ Remove</button>
      </div>
      <div class="tbl-wrap">
        <table>
          <thead><tr>
            <th>Description</th><th>Acct</th><th>Type</th><th>Strike</th>
            <th>Expiry</th><th>DTE</th><th>Qty</th>
            <th>Cost/Avg</th><th>Cost Total</th>
            <th>Scen Price</th><th>Scen Value</th><th>P&amp;L vs Today</th><th>P&amp;L %</th>
          </tr></thead>
          <tbody>
            ${optRows}
            <tr class="cmp-total-row">
              <td colspan="11"><b>Included Options Total</b></td>
              <td class="${lk.aggPnl>=0?"pnl-pos":"pnl-neg"}">${fmt.usd2(lk.aggPnl)}</td>
              <td class="${lk.aggPnl>=0?"pnl-pos":"pnl-neg"}" style="font-size:.75rem">${aggPnlPct!=null?fmt.pct(aggPnlPct):"—"}</td>
            </tr>
          </tbody>
        </table>
      </div>`;
    cards.appendChild(card);
  }

  cards.querySelectorAll(".cmp-del-card").forEach(btn => {
    btn.addEventListener("click", () => {
      comparisonRows.splice(parseInt(btn.dataset.idx), 1);
      renderComparisonTable();
    });
  });

  cards.querySelectorAll(".cmp-update-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const idx   = parseInt(btn.dataset.idx);
      const card  = btn.closest(".cmp-card");
      const date  = card.querySelector(".cmp-edit-date").value;
      const price = parseFloat(card.querySelector(".cmp-edit-price").value);
      if (!date || isNaN(price) || price <= 0) return;
      comparisonRows[idx] = { date, price };
      renderComparisonTable();
    });
  });
}

/* ── % live preview while typing price ──────────────────── */
function updateCmpPctLive() {
  const price = parseFloat(document.getElementById("cmp-price").value);
  const pctEl = document.getElementById("cmp-pct-live");
  if (!scenarioData || isNaN(price)) { pctEl.textContent = "—"; pctEl.className = "cmp-pct"; return; }
  const pct = (price - scenarioData.underlying_price) / scenarioData.underlying_price * 100;
  pctEl.textContent = fmt.pct(pct);
  pctEl.className = "cmp-pct " + fmt.sign(pct);
}

/* ────────────────────────────────────────────────────────── */
/*  EVENTS                                                    */
/* ────────────────────────────────────────────────────────── */
document.getElementById("opt-select").addEventListener("change", e => {
  selectedKey = e.target.value; renderScenario();
});

document.getElementById("price-type-toggle").addEventListener("click", e => {
  const btn = e.target.closest(".toggle-btn");
  if (!btn) return;
  priceType = btn.dataset.val;
  document.querySelectorAll(".toggle-btn").forEach(b => b.classList.toggle("active", b===btn));
  renderScenario();
  renderComparisonTable();
});

const stepsSlider = document.getElementById("steps-slider");
stepsSlider.addEventListener("input", () => {
  document.getElementById("steps-label").textContent = stepsSlider.value;
});
document.getElementById("refresh-btn").addEventListener("click", () =>
  loadScenarios(parseInt(stepsSlider.value)));

document.getElementById("cmp-price").addEventListener("input", updateCmpPctLive);

document.getElementById("add-comparison-btn").addEventListener("click", () => {
  const date  = document.getElementById("cmp-date").value;
  const price = parseFloat(document.getElementById("cmp-price").value);
  if (!date || isNaN(price) || price <= 0) {
    alert("Please enter a valid date and price.");
    return;
  }
  comparisonRows.push({ date, price });
  renderComparisonTable();
});

document.getElementById("clear-comparison-btn").addEventListener("click", () => {
  comparisonRows = [];
  renderComparisonTable();
});

// Language toggle for AI analysis
document.addEventListener("click", e => {
  const btn = e.target.closest("#ai-lang-toggle .toggle-btn");
  if (!btn) return;
  document.querySelectorAll("#ai-lang-toggle .toggle-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
});

/* ── AI cache helpers ────────────────────────────────────── */
function _renderAI(text) {
  const output = document.getElementById("ai-output");
  if (!output) return;
  output.className = "ai-output";
  output.innerHTML = text
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/\n/g, '<br>');
}

function _updateCacheBadge(status, ageSecs) {
  const badge = document.getElementById("ai-cache-badge");
  if (!badge) return;
  if (status === "done") {
    const mins = ageSecs != null ? Math.round(ageSecs / 60) : null;
    badge.textContent = mins != null ? `缓存 ${mins} 分钟前` : "已缓存";
    badge.className = "ai-cache-badge done";
    badge.style.display = "inline";
  } else if (status === "running") {
    badge.textContent = "后台分析中…";
    badge.className = "ai-cache-badge running";
    badge.style.display = "inline";
  } else {
    badge.style.display = "none";
  }
}

async function loadAICache() {
  try {
    const c = await fetchJSON("/api/ai/cache");
    _updateCacheBadge(c.status, c.age_secs);
    if (c.status === "done" && c.result) {
      _renderAI(c.result);
    } else if (c.status === "running") {
      const output = document.getElementById("ai-output");
      const lang = document.querySelector("#ai-lang-toggle .toggle-btn.active")?.dataset.lang || "zh";
      if (output && !output.textContent.trim()) {
        output.className = "ai-output loading";
        output.innerHTML = (lang === "zh" ? "后台分析进行中，请稍候…" : "Background analysis running…") + ' <span class="ai-cursor"></span>';
      }
    }
  } catch(_) {}
}

document.addEventListener("click", async e => {
  if (!e.target.closest("#ai-analyze-btn")) return;
  const output = document.getElementById("ai-output");
  const btn    = document.getElementById("ai-analyze-btn");
  if (!output || !btn) return;

  const langBtn = document.querySelector("#ai-lang-toggle .toggle-btn.active");
  const lang = langBtn ? langBtn.dataset.lang : "zh";

  // Check cache first — if fresh (< 30 min) and same lang, show it
  try {
    const cache = await fetchJSON("/api/ai/cache");
    if (cache.status === "done" && cache.result && cache.age_secs != null && cache.age_secs < 1800) {
      _renderAI(cache.result);
      _updateCacheBadge("done", cache.age_secs);
      return;
    }
    if (cache.status === "running") {
      output.className = "ai-output loading";
      output.innerHTML = (lang === "zh" ? "后台分析进行中，请稍候…" : "Background analysis running…") + ' <span class="ai-cursor"></span>';
      _updateCacheBadge("running", null);
      return;
    }
  } catch(_) {}

  const symbols = Array.from(includedSymbols);
  if (!symbols.length) { output.innerHTML = '<span class="hp-hint">No options selected — check at least one position in the Holdings table.</span>'; return; }

  btn.disabled = true;
  btn.textContent = "⏳ Analyzing…";
  output.className = "ai-output loading";
  output.innerHTML = (lang === "zh" ? "正在生成分析…" : "Generating analysis…") + ' <span class="ai-cursor"></span>';

  try {
    const res = await fetch("/api/ai/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selected_symbols: symbols, avgo_price: scenarioData?.underlying_price, lang }),
    });
    if (!res.ok) throw new Error(await res.text());

    output.className = "ai-output";
    output.innerHTML = "";
    let raw = "";
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      raw += decoder.decode(value, { stream: true });
      output.innerHTML = raw
        .replace(/^## (.+)$/gm, '<h2>$1</h2>')
        .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
        .replace(/\n/g, '<br>');
      output.scrollTop = output.scrollHeight;
    }
  } catch(e) {
    output.className = "ai-output";
    output.innerHTML = `<span style="color:var(--red)">✗ Error: ${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "✦ Analyze Selected";
  }
});

/* ── Live price toggle ───────────────────────────────────── */
let _lastScenarioRefresh = 0;

function _updateLiveDisplay(price, chg) {
  const display = document.getElementById("live-price-display");
  if (!display) return;
  display.style.display = "inline";
  display.className = "live-price-val" + (chg != null && chg < 0 ? " down" : "");
  display.textContent = `$${price.toFixed(2)}` + (chg != null ? ` (${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%)` : "");
  // Update AVGO price summary card directly
  const avgoCard = document.getElementById("card-avgo");
  if (avgoCard) avgoCard.textContent = fmt.usd2(price);
}

function _startPriceSSE() {
  if (_priceSSE) { _priceSSE.close(); _priceSSE = null; }
  _priceSSE = new EventSource("/api/price/stream");
  _priceSSE.onmessage = e => {
    const data = JSON.parse(e.data);
    if (!data.live) return;
    _livePrice = data.price;
    _updateLiveDisplay(data.price, data.change_pct);
    // Throttle: refresh scenarios + holdings at most once per 10s when price shifts
    const now = Date.now();
    if (scenarioData && Math.abs(data.price - (scenarioData.underlying_price || 0)) > 0.5
        && now - _lastScenarioRefresh > 10000) {
      _lastScenarioRefresh = now;
      loadScenarios(parseInt(stepsSlider?.value || 21));
      loadSummary();
      loadHoldings();
    }
  };
  _priceSSE.onerror = () => { /* auto-retry by browser */ };
}

function _stopPriceSSE() {
  if (_priceSSE) { _priceSSE.close(); _priceSSE = null; }
  const display = document.getElementById("live-price-display");
  if (display) display.style.display = "none";
}

document.addEventListener("change", async e => {
  if (e.target.id !== "live-price-toggle") return;
  _liveEnabled = e.target.checked;
  try {
    const r = await fetch(`/api/price/live?enabled=${_liveEnabled}`, { method: "POST" });
    const d = await r.json();
    if (_liveEnabled) {
      _startPriceSSE();
      // Wait briefly for the first price to arrive before full reload
      await new Promise(res => setTimeout(res, 2500));
      await reloadAll();
    } else {
      _stopPriceSSE();
      await reloadAll();
    }
  } catch(err) {
    console.error("Live price toggle error:", err);
  }
});

/* ────────────────────────────────────────────────────────── */
/*  TABS                                                      */
/* ────────────────────────────────────────────────────────── */
let _logsInterval = null;

function _startLogsPolling() {
  if (_logsInterval) return;
  _logsInterval = setInterval(() => {
    if (_activeLogTab === "debug") loadDebugLogs(true);
    else                           loadLogs(true);
  }, 5000);
}
function _stopLogsPolling() {
  if (_logsInterval) { clearInterval(_logsInterval); _logsInterval = null; }
}

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    const tab = btn.dataset.tab;
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.toggle("active", p.id === `tab-${tab}`));
    if (tab === "settings") { loadSettings(); _stopLogsPolling(); }
    else if (tab === "logs") { _switchLogTab(_activeLogTab); _startLogsPolling(); }
    else { _stopLogsPolling(); }
  });
});

/* ────────────────────────────────────────────────────────── */
/*  SETTINGS TAB                                              */
/* ────────────────────────────────────────────────────────── */
async function loadSettings() {
  try {
    const s = await fetchJSON("/api/settings");
    document.getElementById("setting-warning-pct").value = s.warning_pct;
    document.getElementById("setting-alert-pct").value   = s.alert_pct;
    document.getElementById("setting-avgo-pct").value    = s.avgo_change_pct;
  } catch(_) {}
  await loadOngoingSync();
}

/* ────────────────────────────────────────────────────────── */
/*  ONGOING SYNC                                              */
/* ────────────────────────────────────────────────────────── */
function _setIntervalDisplay(secs) {
  const input = document.getElementById("ongoing-interval");
  const unit  = document.getElementById("ongoing-unit");
  if (!input || !unit) return;
  if (secs % 60 === 0) {
    input.value = secs / 60;
    unit.value  = "min";
  } else {
    input.value = secs;
    unit.value  = "sec";
  }
}

function _getIntervalSecs() {
  const val  = parseInt(document.getElementById("ongoing-interval")?.value) || 5;
  const unit = document.getElementById("ongoing-unit")?.value || "min";
  return unit === "sec" ? Math.max(30, val) : Math.max(1, val) * 60;
}

async function loadOngoingSync() {
  try {
    const s = await fetchJSON("/api/sync/ongoing");
    document.getElementById("ongoing-sync-toggle").checked    = !!s.enabled;
    document.getElementById("allow-anytime-toggle").checked   = !!s.allow_anytime;
    _setIntervalDisplay(s.interval_secs ?? 300);
    _renderOngoingStatus(s);
  } catch(_) {}
}

function _renderOngoingStatus(s) {
  const badge = document.getElementById("ongoing-status-badge");
  const lastEl = document.getElementById("ongoing-last-sync");
  const mktEl  = document.getElementById("ongoing-market-hours");
  if (!badge) return;

  if (s.status === "running") {
    badge.textContent = "● Syncing…";
    badge.className = "ongoing-badge running";
  } else if (s.status === "done") {
    badge.textContent = "✓ Active";
    badge.className = "ongoing-badge done";
  } else if (s.status === "error") {
    badge.textContent = "✗ Error";
    badge.title = s.last_error || "";
    badge.className = "ongoing-badge error";
  } else if (s.enabled) {
    badge.textContent = "⏳ Waiting…";
    badge.className = "ongoing-badge waiting";
  } else {
    badge.textContent = "Off";
    badge.className = "ongoing-badge off";
  }

  lastEl.textContent = s.last_sync
    ? `Last: ${_fmtAgo(s.age_secs)}`
    : "";

  if (s.enabled) {
    mktEl.textContent = s.in_market_hours
      ? "📈 Market hours (running)"
      : "🕐 Outside market hours (paused)";
    mktEl.className = s.in_market_hours ? "ongoing-market-hours active" : "ongoing-market-hours muted";
  } else {
    mktEl.textContent = "Mon–Fri 06:00–13:00 PT";
    mktEl.className = "ongoing-market-hours muted";
  }
}

document.getElementById("sync-now-btn")?.addEventListener("click", async () => {
  const btn = document.getElementById("sync-now-btn");
  const msg = document.getElementById("sync-now-msg");
  btn.disabled = true;
  btn.textContent = "⏳ Syncing…";
  msg.textContent = "";
  msg.classList.remove("visible");
  try {
    const res = await fetchJSON("/api/sync/now", { method: "POST" });
    msg.textContent = `✓ Saved ${res.filename}`;
    msg.style.color = "var(--green)";
    msg.classList.add("visible");
    // Reload holdings with the new data
    await reloadAll();
  } catch(e) {
    msg.textContent = `✗ ${e.message.split("\n")[0]}`;
    msg.style.color = "var(--red)";
    msg.classList.add("visible");
  } finally {
    btn.disabled = false;
    btn.textContent = "↻ Sync Now";
    setTimeout(() => msg.classList.remove("visible"), 5000);
  }
});

document.getElementById("save-ongoing-btn")?.addEventListener("click", async () => {
  const enabled       = document.getElementById("ongoing-sync-toggle").checked;
  const allow_anytime = document.getElementById("allow-anytime-toggle").checked;
  const msg = document.getElementById("ongoing-save-msg");
  try {
    const s = await fetchJSON("/api/sync/ongoing", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, interval_secs: _getIntervalSecs(), allow_anytime }),
    });
    _renderOngoingStatus(s);
    msg.textContent = "✓ Saved";
    msg.classList.add("visible");
    setTimeout(() => msg.classList.remove("visible"), 2500);
  } catch(e) {
    msg.textContent = `✗ ${e.message}`;
    msg.style.color = "var(--red)";
    msg.classList.add("visible");
  }
});

document.getElementById("save-settings-btn").addEventListener("click", async () => {
  const body = {
    warning_pct:    parseFloat(document.getElementById("setting-warning-pct").value),
    alert_pct:      parseFloat(document.getElementById("setting-alert-pct").value),
    avgo_change_pct: parseFloat(document.getElementById("setting-avgo-pct").value),
  };
  const msg = document.getElementById("settings-save-msg");
  try {
    await fetchJSON("/api/settings", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    msg.textContent = "✓ Saved";
    msg.classList.add("visible");
    setTimeout(() => msg.classList.remove("visible"), 2500);
  } catch(e) {
    msg.textContent = `✗ ${e.message}`;
    msg.style.color = "var(--red)";
    msg.classList.add("visible");
  }
});

document.getElementById("test-notif-btn").addEventListener("click", async () => {
  const btn = document.getElementById("test-notif-btn");
  const res = document.getElementById("test-notif-result");
  btn.disabled = true;
  btn.textContent = "⏳ Sending…";
  res.style.display = "none";
  try {
    const d = await fetchJSON("/api/notifications/test", { method: "POST" });
    const chRows = d.channels.map(ch => {
      const ok = ch.status === "ok";
      return `<div class="${ok ? "ch-ok" : "ch-err"}">${ok ? "✓" : "✗"} ${ch.name} — ${ch.detail}</div>`;
    }).join("");
    res.innerHTML = `<div>${chRows}</div><div class="notif-preview">${d.message.replace(/\*/g, "").replace(/`/g, "")}</div>`;
    res.style.display = "block";
  } catch(e) {
    res.innerHTML = `<div class="ch-err">✗ Error: ${e.message}</div>`;
    res.style.display = "block";
  } finally {
    btn.disabled = false;
    btn.textContent = "📬 Send Test Notification";
  }
});

/* ────────────────────────────────────────────────────────── */
/*  LOGS TAB                                                  */
/* ────────────────────────────────────────────────────────── */
const _LOG_LABELS = {
  csv_load: "CSV 加载", alert: "告警", warning: "预警",
  test: "测试", setting: "设置", error: "错误", sync: "同步",
};
const _LOG_ICONS = {
  csv_load: "📂", alert: "🚨", warning: "⚠️", test: "📬",
  setting: "⚙️", error: "❌", sync: "🔄",
};

function _fmtLogTime(isoStr) {
  try {
    const d = new Date(isoStr);
    return d.toLocaleString("zh-CN", { month:"2-digit", day:"2-digit",
      hour:"2-digit", minute:"2-digit", second:"2-digit", hour12: false });
  } catch(_) { return isoStr; }
}

async function loadLogs(silent = false) {
  const list = document.getElementById("logs-list");
  if (!silent) list.innerHTML = '<div class="logs-empty">Loading…</div>';
  try {
    const logs = await fetchJSON("/api/logs?limit=200");
    if (!logs.length) { list.innerHTML = '<div class="logs-empty">No events yet.</div>'; return; }
    list.innerHTML = logs.map(e => {
      const type = e.type || "info";
      return `<div class="log-entry">
        <div class="log-time">${_fmtLogTime(e.time)}</div>
        <div class="log-badge ${type}">${_LOG_ICONS[type]||"ℹ️"} ${_LOG_LABELS[type]||type}</div>
        <div class="log-msg">${e.message}</div>
      </div>`;
    }).join("");
  } catch(e) {
    if (!silent)
      list.innerHTML = `<div class="logs-empty" style="color:var(--red)">Failed to load logs: ${e.message}</div>`;
  }
}

/* ── Debug log ─────────────────────────────────────────────── */
const _DL_LEVEL_RE = /\[(\w+)\s*\]/;

async function loadDebugLogs(silent = false) {
  const list = document.getElementById("debug-logs-list");
  const meta = document.getElementById("debug-log-meta");
  if (!silent) list.innerHTML = '<div class="logs-empty">Loading…</div>';
  try {
    const d = await fetchJSON("/api/logs/debug?lines=600");
    if (meta) {
      const kb = (d.size_bytes / 1024).toFixed(1);
      meta.textContent = `${d.total_lines} lines · ${kb} KB`;
    }
    if (!d.lines.length) { list.innerHTML = '<div class="logs-empty">Debug log is empty.</div>'; return; }
    list.innerHTML = d.lines.slice().reverse().map(line => {
      // Parse: "2026-04-19 10:30:15.123 [INFO ] message..."
      const ts  = line.slice(0, 23);
      const m   = line.match(_DL_LEVEL_RE);
      const lvl = m ? m[1].trim() : "INFO";
      const msg = line.slice(line.indexOf("]") + 1).trim();
      return `<div class="debug-log-entry">
        <span class="dl-time">${ts}</span>
        <span class="dl-level ${lvl}">${lvl}</span>
        <span class="dl-msg">${msg.replace(/</g,"&lt;").replace(/>/g,"&gt;")}</span>
      </div>`;
    }).join("");
  } catch(e) {
    if (!silent)
      list.innerHTML = `<div class="logs-empty" style="color:var(--red)">Failed to load debug log: ${e.message}</div>`;
  }
}

/* ── Log sub-tab switching ─────────────────────────────────── */
let _activeLogTab = "events";

function _switchLogTab(tab) {
  _activeLogTab = tab;
  document.querySelectorAll(".log-tab-btn").forEach(b =>
    b.classList.toggle("active", b.id === `log-tab-${tab}`)
  );
  document.querySelectorAll(".log-panel").forEach(p =>
    p.classList.toggle("active", p.id === `log-panel-${tab}`)
  );
  if (tab === "events") loadLogs(false);
  else                  loadDebugLogs(false);
}

document.getElementById("log-tab-events")?.addEventListener("click", () => _switchLogTab("events"));
document.getElementById("log-tab-debug")?.addEventListener("click",  () => _switchLogTab("debug"));

document.getElementById("refresh-logs-btn").addEventListener("click", () => {
  if (_activeLogTab === "debug") loadDebugLogs(false);
  else                           loadLogs(false);
});

/* ────────────────────────────────────────────────────────── */
/*  BOOT                                                      */
/* ────────────────────────────────────────────────────────── */
async function reloadAll() {
  document.getElementById("holdings-spinner").style.display = "block";
  document.getElementById("holdings-content").style.display = "none";
  await Promise.all([loadSummary(), loadHoldings()]);
  await loadScenarios(parseInt(stepsSlider.value) || 21);
  // Defer AI cache render: give Plotly's async DOM operations time to settle
  setTimeout(loadAICache, 600);
}

(async () => {
  setupDataSource();
  // Check sync status on load — if a startup sync is running, start polling
  const s = await _checkSyncStatus(false);
  if (s?.status === "running") {
    _syncPollTimer = _syncPollTimer || setInterval(() => _checkSyncStatus(true), 3000);
  }
  try {
    await reloadAll();
  } catch(err) {
    console.error(err);
    document.body.insertAdjacentHTML("afterbegin",
      `<div style="background:#ef4444;color:#fff;padding:9px 22px;font-size:.8rem">
        ⚠ Backend not reachable. Run: <code>cd options-portfolio && ./start.sh</code>
      </div>`);
  }
})();
