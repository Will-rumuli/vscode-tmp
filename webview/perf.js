/* 性能监控面板前端逻辑: 轮询宿主采集的性能快照, 用轻量 SVG 折线图实时展示
   帧率 / CPU 使用率 / CPU·GPU 频率 / 温度走势。

   数据流: 本页 postMessage 'poll' 请求下一次采样, 宿主采集完回 'data'/'error';
   收到响应后才排下一次 (串行), 避免慢请求堆积。所有图表为手写 SVG, 无外部依赖。

   悬停: 鼠标在图上移动时, 按横坐标反解出最近的采样点, 画出竖线 + 高亮圆点,
   并在光标旁弹出气泡显示该采样点的系统时间与各序列数值。 */
const vscode = acquireVsCodeApi();

function tr(key, vars) {
  let s = (window.QPI_I18N && window.QPI_I18N[key]) || key;
  if (vars) {
    for (const k in vars) { s = s.split('{' + k + '}').join(String(vars[k])); }
  }
  return s;
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const MAX_POINTS = 90;

/* 图表几何: 所有图共用, 悬停时反解坐标同样依赖, 故提到模块级 */
const CW = 640, CH = 160, CPL = 46, CPR = 12, CPT = 12, CPB = 22;
const CIW = CW - CPL - CPR, CIH = CH - CPT - CPB;

const errorEl = document.getElementById('error');
const statsEl = document.getElementById('stats');
const chartsEl = document.getElementById('charts');
const devHeadEl = document.getElementById('devHead');
const ivlEl = document.getElementById('ivl');
const pauseBtn = document.getElementById('pauseBtn');
const tipEl = document.getElementById('tip');

let history = [];
let running = true;
let intervalMs = 1000;
let timer = null;
let lastSerial = '';
let hoverCi = -1;        // 当前悬停的图下标
let hoverTs = null;      // 当前悬停采样点的时间戳 (重绘后据此恢复气泡)
let hoverX = 0, hoverY = 0;   // 最近的鼠标位置 (重绘后气泡仍定位在光标旁)

/* 图表定义: k = 历史点字段名 (见 flatten), color/labelKey 用于图例 */
const CHARTS = [
  {
    titleKey: 'perf.chart.fps', unit: 'fps', yMin: 0,
    series: [{ k: 'fps', labelKey: 'perf.series.fps', color: 'var(--vscode-charts-blue,#3794ff)' }],
  },
  {
    titleKey: 'perf.chart.cpu', unit: '%', fixed: [0, 100],
    series: [{ k: 'cpuUsage', labelKey: 'perf.series.cpuUsage', color: 'var(--vscode-charts-green,#89d185)' }],
  },
  {
    titleKey: 'perf.chart.freq', unit: 'MHz',
    series: [
      { k: 'cpuMHz', labelKey: 'perf.series.cpuMHz', color: 'var(--vscode-charts-blue,#3794ff)' },
      { k: 'gpuMHz', labelKey: 'perf.series.gpuMHz', color: 'var(--vscode-charts-purple,#b180d7)' },
    ],
  },
  {
    titleKey: 'perf.chart.temp', unit: '°C',
    series: [
      { k: 'cpuTemp', labelKey: 'perf.series.cpuTemp', color: 'var(--vscode-charts-red,#e06c75)' },
      { k: 'gpuTemp', labelKey: 'perf.series.gpuTemp', color: 'var(--vscode-charts-orange,#d18616)' },
      { k: 'ddrTemp', labelKey: 'perf.series.ddrTemp', color: 'var(--vscode-charts-yellow,#e5c07b)' },
    ],
  },
];

function num(v, digits) {
  if (v == null || isNaN(v)) { return null; }
  const d = digits == null ? (Math.abs(v) >= 100 ? 0 : 1) : digits;
  return Number(v).toFixed(d);
}

/** 把后端快照压平成图表用的历史点; 缺失字段用 null (折线断开, 显示「无数据」) */
function flatten(d) {
  const cpu = d.cpu || {}, gpu = d.gpu || {}, th = d.thermal || {};
  return {
    // 后端 ts 是 time.time() 的秒级浮点, 统一换算成毫秒 (与 Date.now() 兜底同单位)
    ts: d.ts != null ? d.ts * 1000 : Date.now(),
    fps: d.fps != null ? d.fps : null,
    cpuUsage: cpu.usagePct != null ? cpu.usagePct : null,
    cpuMHz: cpu.avgMHz != null ? cpu.avgMHz : null,
    gpuMHz: gpu.curMHz != null ? gpu.curMHz : null,
    gpuBusy: gpu.busyPct != null ? gpu.busyPct : null,
    maxTemp: th.maxTempC != null ? th.maxTempC : null,
    cpuTemp: th.cpuTempC != null ? th.cpuTempC : null,
    gpuTemp: th.gpuTempC != null ? th.gpuTempC : null,
    ddrTemp: th.ddrTempC != null ? th.ddrTempC : null,
  };
}

/** 某序列在本轮历史里是否出现过数据。
 *  从未出现过 = 该平台不提供这项参数 (如部分板卡的 GPU 频率 / DDR 温度),
 *  统计卡片、图表、图例、悬浮气泡里一律整项不展示, 而不是显示「无数据」占位。 */
function hasSeries(k) {
  for (let i = 0; i < history.length; i++) {
    if (history[i][k] != null) { return true; }
  }
  return false;
}

function statCard(label, val, unit, sub) {
  const body = val == null
    ? `<span class="hint">${tr('perf.noData')}</span>`
    : `${esc(val)}${unit ? `<span class="u">${esc(unit)}</span>` : ''}`;
  return `<div class="stat"><div class="lbl">${esc(label)}</div><div class="val">${body}</div>`
    + (sub ? `<div class="sub">${esc(sub)}</div>` : '') + '</div>';
}

function renderStats(d) {
  const cpu = d.cpu || {}, gpu = d.gpu || {}, th = d.thermal || {};
  const cards = [];
  // 取不到数据的参数整项不展示 (不留「无数据」卡片)
  if (hasSeries('fps')) {
    cards.push(statCard(tr('perf.stat.fps'), num(d.fps), 'fps', null));
  }
  if (hasSeries('cpuUsage')) {
    cards.push(statCard(tr('perf.stat.cpuUsage'), num(cpu.usagePct), '%',
      cpu.cores ? `${tr('perf.cores')}: ${cpu.cores}` : null));
  }
  if (hasSeries('cpuMHz')) {
    cards.push(statCard(tr('perf.stat.cpuFreq'), num(cpu.avgMHz), 'MHz',
      cpu.maxMHz != null ? `${tr('perf.maxFreq')}: ${num(cpu.maxMHz)} MHz` : null));
  }
  if (hasSeries('gpuMHz')) {
    cards.push(statCard(tr('perf.stat.gpuFreq'), num(gpu.curMHz), 'MHz',
      gpu.maxMHz != null ? `${tr('perf.maxFreq')}: ${num(gpu.maxMHz)} MHz` : null));
  }
  if (hasSeries('gpuBusy')) {
    cards.push(statCard(tr('perf.stat.gpuBusy'), num(gpu.busyPct), '%', null));
  }
  if (hasSeries('maxTemp')) {
    const tsub = [th.cpuTempC != null ? `CPU ${num(th.cpuTempC)}` : null,
                  th.gpuTempC != null ? `GPU ${num(th.gpuTempC)}` : null]
      .filter(Boolean).join(' / ');
    cards.push(statCard(tr('perf.stat.temp'), num(th.maxTempC), '°C', tsub || null));
  }
  const plat = d.platform || {};
  const ptxt = [plat.board, plat.soc].filter(Boolean).join(' · ');
  if (ptxt) {
    cards.push(statCard(tr('perf.stat.platform'), ptxt, '', plat.family || null));
  }
  if (cpu.load && cpu.load.length) {
    cards.push(statCard(tr('perf.stat.load'), cpu.load.map((x) => num(x, 2)).join(' / '),
      '', tr('perf.loadHint')));
  }
  statsEl.innerHTML = cards.join('');
}

/* ================= 横坐标定位 =================
 * 按真实采样时间戳定位 (采样间隔可切换), 时间戳不可用时退回索引等距。
 * 图轴不显示时间刻度, 具体时间由悬停气泡给出。 */
function useTimeAxis() {
  const n = history.length;
  return n > 1 && history[n - 1].ts > history[0].ts;
}
function xOf(i) {
  const n = history.length;
  if (n <= 1) { return CPL; }
  if (useTimeAxis()) {
    const t0 = history[0].ts, t1 = history[n - 1].ts;
    return CPL + (CIW * (history[i].ts - t0)) / (t1 - t0);
  }
  return CPL + (CIW * i) / (n - 1);
}
/** 画布 x 坐标反解成最接近的采样点下标 */
function nearestIndex(vx) {
  const n = history.length;
  if (!n) { return -1; }
  if (!useTimeAxis()) {
    const r = (vx - CPL) / CIW;
    return Math.max(0, Math.min(n - 1, Math.round(r * (n - 1))));
  }
  const t0 = history[0].ts, t1 = history[n - 1].ts;
  const t = t0 + ((vx - CPL) * (t1 - t0)) / CIW;
  let best = 0, bd = Infinity;
  for (let i = 0; i < n; i++) {
    const d = Math.abs(history[i].ts - t);
    if (d < bd) { bd = d; best = i; }
  }
  return best;
}
function indexOfTs(ts) {
  let best = -1, bd = Infinity;
  for (let i = 0; i < history.length; i++) {
    const d = Math.abs(history[i].ts - ts);
    if (d < bd) { bd = d; best = i; }
  }
  return best;
}

/* ================= 纵坐标 ================= */
function seriesPoints(def) {
  return def.series.map((s) => ({ s, pts: history.map((h) => h[s.k]) }));
}
function yRange(def) {
  const vals = [];
  seriesPoints(def).forEach(({ pts }) => pts.forEach((v) => { if (v != null) { vals.push(v); } }));
  let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
  if (def.fixed) {
    lo = def.fixed[0]; hi = def.fixed[1];
  } else {
    const span = (hi - lo) || Math.max(1, Math.abs(hi) * 0.1);
    lo -= span * 0.15; hi += span * 0.15;
    if (def.yMin != null && lo < def.yMin) { lo = def.yMin; }
  }
  return { lo, hi };
}
const yOf = (v, lo, hi) => CPT + CIH - ((v - lo) / (hi - lo)) * CIH;

/* ================= 图表绘制 ================= */
function chartHtml(def, ci) {
  // 只保留历史上有过数据的序列; 该图所有序列都取不到 -> 整张图不展示
  const series = seriesPoints(def).filter(({ s }) => hasSeries(s.k));
  if (!series.length) { return ''; }
  const head = `<div class="chart-head">${tr(def.titleKey)}`
    + (def.unit ? `<span class="unit">${esc(def.unit)}</span>` : '') + '</div>';
  const { lo, hi } = yRange(def);

  let grid = '';
  for (let g = 0; g <= 4; g++) {
    const v = lo + ((hi - lo) * g) / 4;
    const yy = yOf(v, lo, hi);
    grid += `<line class="grid" x1="${CPL}" y1="${yy.toFixed(1)}" x2="${CW - CPR}" y2="${yy.toFixed(1)}"/>`;
    grid += `<text class="axis" x="${CPL - 6}" y="${(yy + 3).toFixed(1)}" text-anchor="end">${num(v)}</text>`;
  }

  let paths = '';
  series.forEach(({ s, pts }) => {
    let d = '', pen = false;
    pts.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += `${pen ? ' L' : ' M'}${xOf(i).toFixed(1)} ${yOf(v, lo, hi).toFixed(1)}`;
      pen = true;
    });
    if (d) {
      // 颜色含 CSS 变量, 必须走 style 而不能用作 SVG 表现属性 (表现属性不支持 var())
      paths += `<path d="${d}" style="stroke:${s.color}" stroke-width="1.6" fill="none"`
        + ` vector-effect="non-scaling-stroke"/>`;
    }
    for (let i = pts.length - 1; i >= 0; i--) {
      if (pts[i] != null) {
        paths += `<circle cx="${xOf(i).toFixed(1)}" cy="${yOf(pts[i], lo, hi).toFixed(1)}" r="2.4"`
          + ` style="fill:${s.color}"/>`;
        break;
      }
    }
  });

  const legend = series.map(({ s, pts }) => {
    let cur = null;
    for (let i = pts.length - 1; i >= 0; i--) { if (pts[i] != null) { cur = pts[i]; break; } }
    const txt = cur == null ? tr('perf.noData') : `${num(cur)}${def.unit ? ' ' + def.unit : ''}`;
    return `<span class="lg"><i style="background:${s.color}"></i>${tr(s.labelKey)} <b>${esc(txt)}</b></span>`;
  }).join('');

  // hoverG: 悬停时的竖线 + 高亮圆点; hoverLayer: 覆盖绘图区的透明命中层
  return `<div class="chart">${head}`
    + `<svg class="svg" viewBox="0 0 ${CW} ${CH}" preserveAspectRatio="none">${grid}${paths}`
    + `<g class="hoverG" pointer-events="none"></g>`
    + `<rect class="hoverLayer" data-ci="${ci}" x="${CPL}" y="${CPT}" width="${CIW}" height="${CIH}"`
    + ` fill="none" pointer-events="all"/></svg>`
    + `<div class="legend">${legend}</div></div>`;
}

function renderCharts() {
  chartsEl.innerHTML = CHARTS.map((def, ci) => chartHtml(def, ci)).join('');
  // 采样会周期性重绘, 若鼠标仍停在图上则恢复气泡, 避免每秒闪没
  if (hoverCi >= 0 && hoverTs != null) {
    const layer = chartsEl.querySelector(`.hoverLayer[data-ci="${hoverCi}"]`);
    const i = indexOfTs(hoverTs);
    if (layer && i >= 0) {
      hoverTs = history[i].ts;
      drawGuide(layer.ownerSVGElement, hoverCi, i);
      showTip(hoverCi, i, hoverX, hoverY);
    } else {
      hideHover();
    }
  }
}

/* ================= 悬停气泡 ================= */
function clockOf(ts) {
  const d = new Date(ts);
  const p = (x) => String(x).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function drawGuide(svg, ci, i) {
  const g = svg && svg.querySelector('.hoverG');
  if (!g) { return; }
  const def = CHARTS[ci];
  const { lo, hi } = yRange(def);
  const xx = xOf(i);
  let out = `<line x1="${xx.toFixed(1)}" y1="${CPT}" x2="${xx.toFixed(1)}" y2="${CH - CPB}"/>`;
  def.series.forEach((s) => {
    const v = history[i][s.k];
    if (v == null) { return; }
    out += `<circle cx="${xx.toFixed(1)}" cy="${yOf(v, lo, hi).toFixed(1)}" r="3.6"`
      + ` style="fill:var(--bg);stroke:${s.color}"/>`;
  });
  g.innerHTML = out;
}

function showTip(ci, i, cx, cy) {
  const def = CHARTS[ci];
  const h = history[i];
  if (!tipEl || !def || !h) { return; }
  const rows = def.series.filter((s) => hasSeries(s.k)).map((s) => {
    const v = h[s.k];
    const txt = v == null ? tr('perf.noData') : `${num(v)}${def.unit ? ' ' + def.unit : ''}`;
    return `<div class="tr"><i style="background:${s.color}"></i>${esc(tr(s.labelKey))}`
      + `<b>${esc(txt)}</b></div>`;
  }).join('');
  tipEl.innerHTML = `<div class="tt">${esc(tr(def.titleKey))} · ${clockOf(h.ts)}</div>${rows}`;
  tipEl.classList.remove('hidden');
  const tw = tipEl.offsetWidth, th = tipEl.offsetHeight;
  tipEl.style.left = `${Math.max(8, Math.min(cx + 14, window.innerWidth - tw - 8))}px`;
  tipEl.style.top = `${Math.max(8, Math.min(cy + 14, window.innerHeight - th - 8))}px`;
}

function hideHover() {
  hoverCi = -1;
  hoverTs = null;
  if (tipEl) { tipEl.classList.add('hidden'); }
}

function onHoverMove(e) {
  const target = e.target;
  const layer = target && target.closest ? target.closest('.hoverLayer') : null;
  if (!layer) { hideHover(); return; }
  const svg = layer.ownerSVGElement;
  const box = svg && svg.getBoundingClientRect();
  if (!box || !box.width) { return; }
  // preserveAspectRatio="none" 下宽高各自独立缩放, 直接用 svg 外框线性映射回 viewBox
  const vx = ((e.clientX - box.left) / box.width) * CW;
  const i = nearestIndex(vx);
  if (i < 0) { return; }
  hoverCi = Number(layer.dataset.ci) || 0;
  hoverTs = history[i].ts;
  hoverX = e.clientX;
  hoverY = e.clientY;
  drawGuide(svg, hoverCi, i);
  showTip(hoverCi, i, hoverX, hoverY);
}

chartsEl.addEventListener('mousemove', onHoverMove);
chartsEl.addEventListener('mouseleave', hideHover);

/* ================= 设备状态 / 轮询 ================= */
function renderDevHead(d) {
  if (!d || !d.device) { devHeadEl.innerHTML = ''; return; }
  const ok = String(d.device.state).toLowerCase() === 'device';
  devHeadEl.innerHTML = `<span class="dot ${ok ? '' : 'off'}"></span>`
    + `<span>${ok ? tr('di.devOnline') : tr('di.devOffline')}</span>`
    + ` · <span class="mono">${esc(d.device.serial)}</span>`;
}

function schedule(delay) {
  clearTimeout(timer);
  if (!running) { return; }
  timer = setTimeout(() => {
    if (running) { vscode.postMessage({ type: 'poll' }); }
  }, delay == null ? intervalMs : delay);
}

pauseBtn.addEventListener('click', () => {
  running = !running;
  pauseBtn.textContent = running ? tr('perf.pause') : tr('perf.resume');
  if (running) { schedule(0); } else { clearTimeout(timer); }
});

ivlEl.addEventListener('change', () => {
  intervalMs = Number(ivlEl.value) || 1000;
});

window.addEventListener('message', (e) => {
  const m = e.data;
  if (m.type === 'data') {
    const d = m.payload || {};
    if (!d.ok) {
      errorEl.style.display = 'block';
      errorEl.textContent = d.error || tr('perf.readFail');
      renderDevHead(null);
      schedule(intervalMs);           // 设备掉线时持续重试, 恢复后自动续采
      return;
    }
    errorEl.style.display = 'none';
    const serial = (d.device && d.device.serial) || '';
    if (serial && serial !== lastSerial) {   // 换设备: 清空历史, 避免曲线串台
      lastSerial = serial;
      history = [];
      hideHover();
    }
    history.push(flatten(d));
    if (history.length > MAX_POINTS) { history = history.slice(-MAX_POINTS); }
    renderDevHead(d);
    renderStats(d);
    renderCharts();
    schedule();
  } else if (m.type === 'error') {
    errorEl.style.display = 'block';
    errorEl.textContent = tr('perf.readFail') + ': ' + m.payload;
    renderDevHead(null);
    schedule(Math.max(intervalMs, 2000));
  }
});

renderStats({});
renderCharts();
vscode.postMessage({ type: 'poll' });