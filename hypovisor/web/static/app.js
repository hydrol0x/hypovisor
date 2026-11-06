/* Hypovisor UI: thin page scripts over the JSON API. */
const HV = (() => {
  const $ = (s, r = document) => r.querySelector(s);
  const fmt = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v)) ? '–' : Number(v).toFixed(d);
  const api = async (url, opts = {}) => {
    const r = await fetch(url, opts);
    if (!r.ok) { let t = await r.text(); try { t = JSON.parse(t).detail || t; } catch {} throw new Error(t || r.statusText); }
    return r.json();
  };
  const json = (url, method, body) => api(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const localNow = () => { const d = new Date(); d.setSeconds(0, 0); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16); };
  const todayStr = () => localNow().slice(0, 10);
  const shiftDay = (s, n) => { const d = new Date(s + 'T12:00:00'); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10); };
  const pill = s => `<span class="pill ${s || ''}">${s || 'n/a'}</span>`;
  const hhmm = iso => iso ? iso.slice(11, 16) : '';
  const gridColor = '#262b36';
  Chart.defaults.color = '#8b93a7'; Chart.defaults.borderColor = gridColor;

  function glucoseColor(v) { return v < 70 ? '#ef6b73' : v > 180 ? '#f2c14e' : '#5ad1a8'; }

  function timelineChart(canvas, data, dayStr) {
    const t0 = new Date(dayStr + 'T00:00:00'), t1 = new Date(dayStr + 'T23:59:59');
    const g = data.glucose.filter(p => p.value !== null).map(p => ({ x: new Date(p.local), y: p.value }));
    const basal = data.basal.map(p => ({ x: new Date(p.local), y: p.rate }));
    const bol = data.boluses.map(b => ({ x: new Date(b.local), y: b.units, r: Math.min(14, 4 + b.units * 2), b }));
    const meals = data.meals.map(m => ({ x: new Date(m.local), y: 395, m }));
    const iob = (data.iob || []).map(p => ({ x: new Date(p.local), y: p.iob }));
    if (canvas._chart) canvas._chart.destroy();
    canvas._chart = new Chart(canvas, {
      type: 'line',
      data: { datasets: [
        { label: 'glucose', data: g, parsing: false, pointRadius: 1.5, pointBackgroundColor: ctx => glucoseColor(ctx.raw?.y ?? 100), borderColor: '#8fe3c4', borderWidth: 1.2, tension: .2, spanGaps: 15 * 60000, yAxisID: 'y' },
        { label: 'basal U/h', data: basal, parsing: false, type: 'line', stepped: true, pointRadius: 0, borderWidth: 0, backgroundColor: 'rgba(59,74,107,.5)', fill: 'origin', yAxisID: 'y2' },
        { label: 'IOB', data: iob, parsing: false, type: 'line', pointRadius: 0, borderColor: '#7aa2f7', borderDash: [4, 3], borderWidth: 1, yAxisID: 'y2' },
        { label: 'bolus', data: bol, parsing: false, type: 'bubble', backgroundColor: '#7aa2f7', yAxisID: 'y3' },
        { label: 'meal', data: meals, parsing: false, type: 'scatter', pointStyle: 'triangle', pointRadius: 9, backgroundColor: ctx => ({ green: '#5ad1a8', amber: '#f7b26a', red: '#ef6b73' }[ctx.raw?.m?.score] || '#f7b26a'), yAxisID: 'y' },
      ] },
      options: { animation: false, interaction: { mode: 'nearest', intersect: true }, maintainAspectRatio: true,
        scales: { x: { type: 'time', min: t0, max: t1, time: { unit: 'hour', displayFormats: { hour: 'HH' } }, grid: { color: gridColor } },
          y: { min: 40, max: 400, grid: { color: gridColor } }, y2: { position: 'right', min: 0, suggestedMax: 4, grid: { display: false }, title: { display: true, text: 'U/h · IOB' } }, y3: { display: false, min: 0, max: 20 } },
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => {
          const r = ctx.raw; if (r.b) return `${r.b.units} u ${r.b.kind} ${r.b.carbs ? r.b.carbs + 'g carbs' : ''} ${hhmm(r.b.local)}`;
          if (r.m) return `${r.m.description || 'meal'} ${r.m.carbs_g ?? '?'}g ${hhmm(r.m.local)}`; return `${ctx.dataset.label}: ${fmt(r.y, ctx.dataset.label === 'glucose' ? 0 : 2)}`; } } } },
        onClick: (e, els) => { const el = els.find(x => x.datasetIndex === 4); if (el) location.href = `/meals/${meals[el.index].m.id}`; } }
    });
  }

  function metricsRow(m) {
    if (!m || !m.n) return '<span class="muted">no CGM data</span>';
    return `<span>TIR <b>${fmt(m.tir)}%</b></span><span>Low <b>${fmt(m.tbr)}%</b></span><span>High <b>${fmt(m.tar)}%</b></span><span>Mean <b>${fmt(m.mean)}</b></span><span>CV <b>${fmt(m.cv)}%</b></span><span>GMI <b>${fmt(m.gmi, 1)}</b></span><span>n <b>${m.n}</b></span>`;
  }

  async function loadDay(day, canvas, onData) {
    const data = await api(`/api/timeline?day=${day}`);
    timelineChart(canvas, data, day);
    onData && onData(data);
    return data;
  }

  function dashboard() {
    let day = todayStr();
    const canvas = $('#timeline-chart');
    const render = async () => {
      $('#day-title').textContent = day === todayStr() ? 'Today' : day;
      const d = await loadDay(day, canvas, data => {
        $('#tir').textContent = data.metrics.n ? fmt(data.metrics.tir) + '%' : '–';
        $('#tir-sub').textContent = data.metrics.n ? `low ${fmt(data.metrics.tbr)}% · mean ${fmt(data.metrics.mean)}` : 'no CGM data';
        $('#insulin').textContent = fmt(data.basal_total_units + data.bolus_total_units, 1) + ' u';
        $('#insulin-sub').textContent = `bolus ${fmt(data.bolus_total_units, 1)} · basal ${fmt(data.basal_total_units, 1)}`;
        $('#meals').innerHTML = data.meals.length ? data.meals.map(m => `<div class="meal"><div class="noimg"></div><div><a href="/meals/${m.id}">${m.description || 'meal'}</a><div class="muted small">${hhmm(m.local)} · ${fmt(m.carbs_g)} g carbs</div></div>${pill(m.score)}</div>`).join('') : '<p class="muted">No meals logged. <a href="/meals/new">Log one</a>.</p>';
      });
    };
    $('#prev').onclick = e => { e.preventDefault(); day = shiftDay(day, -1); render(); };
    $('#next').onclick = e => { e.preventDefault(); day = shiftDay(day, 1); render(); };
    api('/api/status').then(s => {
      // open on the most recent day that has CGM data (imports usually end a day or more ago)
      if (s.last_glucose) { const last = new Date(s.last_glucose + 'Z'); const lastDay = new Date(last.getTime() - last.getTimezoneOffset() * 60000).toISOString().slice(0, 10); if (lastDay < day) day = lastDay; }
      render();
      $('#latest').textContent = s.latest ? fmt(s.latest.value) : '–';
      $('#latest-ts').textContent = s.latest ? new Date(s.latest.ts + 'Z').toLocaleString() : 'no data yet';
      $('#iob').textContent = fmt(s.iob_now, 2) + ' u';
    }).catch(() => render());
    api('/api/metrics/daily?days=14').then(d => {
      new Chart($('#daily-chart'), { type: 'bar', data: { labels: d.days.map(x => x.date.slice(5)), datasets: [
        { label: 'TIR %', data: d.days.map(x => x.tir), backgroundColor: '#5ad1a8' }, { label: 'Low %', data: d.days.map(x => x.tbr), backgroundColor: '#ef6b73' }, { label: 'High %', data: d.days.map(x => x.tar), backgroundColor: '#f2c14e' }] },
        options: { animation: false, scales: { x: { stacked: true }, y: { stacked: true, max: 100 } }, plugins: { legend: { position: 'bottom' } } } });
    });
  }

  function timeline() {
    const input = $('#day'); if (!input.value) input.value = todayStr();
    const canvas = $('#timeline-chart');
    const render = async () => {
      const day = input.value; $('#day-title').textContent = day;
      history.replaceState(null, '', `/timeline?day=${day}`);
      await loadDay(day, canvas, data => {
        $('#day-metrics').innerHTML = metricsRow(data.metrics) + `<span>Bolus <b>${fmt(data.bolus_total_units, 1)} u</b></span><span>Basal <b>${fmt(data.basal_total_units, 1)} u</b></span>`;
        $('#boluses').innerHTML = data.boluses.length ? `<table class="table"><tr><th>Time</th><th>Units</th><th>Kind</th><th>Carbs</th><th>BG</th></tr>${data.boluses.map(b => `<tr><td>${hhmm(b.local)}</td><td>${fmt(b.units, 2)}</td><td>${b.kind}${b.extended_min ? ' ' + b.extended_min + 'm' : ''} · ${b.reason}</td><td>${fmt(b.carbs)}</td><td>${fmt(b.bg)}</td></tr>`).join('')}</table>` : '<p class="muted">none</p>';
        $('#meals').innerHTML = [...data.meals.map(m => `<div><a href="/meals/${m.id}">${hhmm(m.local)} ${m.description || 'meal'}</a> ${fmt(m.carbs_g)} g ${pill(m.score)}</div>`), ...data.events.map(e => `<div class="muted">${hhmm(e.local)} ${e.kind} ${e.value ?? ''} ${e.notes || ''}</div>`)].join('') || '<p class="muted">none</p>';
      });
    };
    input.onchange = render;
    $('#prev').onclick = e => { e.preventDefault(); input.value = shiftDay(input.value, -1); render(); };
    $('#next').onclick = e => { e.preventDefault(); input.value = shiftDay(input.value, 1); render(); };
    $('#event-form').onsubmit = async e => { e.preventDefault(); const f = new FormData(e.target); await json('/api/events', 'POST', Object.fromEntries(f.entries())); render(); e.target.reset(); };
    render();
  }

  function mealList() {
    api('/api/meals?limit=200').then(ms => {
      $('#meal-list').innerHTML = ms.length ? ms.map(m => `<div class="meal">${m.photo_url ? `<img src="${m.photo_url}">` : '<div class="noimg"></div>'}<div><a href="/meals/${m.id}">${m.description || 'meal'}</a><div class="muted small">${m.local.replace('T', ' ').slice(0, 16)} · ${fmt(m.carbs_g)} g carbs${m.carbs_low_g !== m.carbs_high_g ? ` (${fmt(m.carbs_low_g)}–${fmt(m.carbs_high_g)})` : ''} · ${m.status}${m.response && m.response.bolus_units ? ` · ${m.response.bolus_units} u` : ''}</div></div>${pill(m.response?.score)}</div>`).join('') : '<p class="muted">No meals yet.</p>';
    });
  }

  function itemsEditor(container, items, kb) {
    const row = (it, i) => `<div class="item" data-i="${i}">
      <input name="name" value="${(it.name || '').replace(/"/g, '&quot;')}" placeholder="item">
      <input name="portion_desc" value="${(it.portion_desc || '').replace(/"/g, '&quot;')}" placeholder="portion">
      <input name="carbs_g" type="number" step="0.5" value="${it.carbs_g ?? ''}" placeholder="carbs">
      <input name="fat_g" type="number" step="0.5" value="${it.fat_g ?? ''}" placeholder="fat">
      <input name="protein_g" type="number" step="0.5" value="${it.protein_g ?? ''}" placeholder="protein">
      <input name="fiber_g" type="number" step="0.5" value="${it.fiber_g ?? ''}" placeholder="fiber">
      <button type="button" class="rm" title="remove">✕</button>
      <div class="range" style="grid-column:1/-1">${it.carbs_low_g != null && it.carbs_low_g !== it.carbs_high_g ? `model range ${fmt(it.carbs_low_g)}–${fmt(it.carbs_high_g)} g · confidence ${fmt((it.confidence || 0) * 100)}%` : ''}
        ${(kb && kb[it.name] || []).filter(k => k.id !== it.food_id).slice(0, 2).map(k => ` · <a href="#" class="use-kb" data-i="${i}" data-id="${k.id}" data-carbs="${k.carbs_g}" data-fat="${k.fat_g ?? ''}" data-protein="${k.protein_g ?? ''}" data-serving="${k.serving_desc}">use saved “${k.name}” (${fmt(k.carbs_g)} g, ${k.provenance || ''})</a>`).join('')}
        ${it.food_name ? ` · linked to <b>${it.food_name}</b>` : ''}</div></div>`;
    container.innerHTML = `<div class="items-edit"><div class="item head"><span>Item</span><span>Portion</span><span>Carbs</span><span>Fat</span><span>Protein</span><span>Fiber</span><span></span></div>${items.map(row).join('')}</div><button type="button" id="add-item">+ item</button>`;
    container._items = items.map(it => ({ ...it }));
    container.onclick = e => {
      if (e.target.id === 'add-item') { container._items.push({ name: '' }); itemsEditor(container, container._items, kb); }
      if (e.target.classList.contains('rm')) { const i = +e.target.closest('.item').dataset.i; readItems(container); container._items.splice(i, 1); itemsEditor(container, container._items, kb); }
      if (e.target.classList.contains('use-kb')) { e.preventDefault(); const d = e.target.dataset; readItems(container); const it = container._items[+d.i]; it.food_id = +d.id; it.carbs_g = +d.carbs; if (d.fat) it.fat_g = +d.fat; if (d.protein) it.protein_g = +d.protein; it.portion_desc = d.serving; it.user_corrected = true; itemsEditor(container, container._items, kb); }
    };
  }
  function readItems(container) {
    container.querySelectorAll('.item[data-i]').forEach(el => {
      const i = +el.dataset.i, it = container._items[i]; const before = JSON.stringify([it.name, it.carbs_g, it.fat_g, it.protein_g, it.fiber_g, it.portion_desc]);
      el.querySelectorAll('input').forEach(inp => { const v = inp.value; it[inp.name] = inp.type === 'number' ? (v === '' ? null : +v) : v; });
      if (JSON.stringify([it.name, it.carbs_g, it.fat_g, it.protein_g, it.fiber_g, it.portion_desc]) !== before) it.user_corrected = true;
    });
    return container._items.filter(it => it.name);
  }

  function mealNew() {
    $('#local_ts').value = localNow();
    const photo = $('#photo'), preview = $('#preview');
    photo.onchange = () => { const f = photo.files[0]; if (!f) return; preview.src = URL.createObjectURL(f); preview.hidden = false; $('#photo-label').textContent = f.name; };
    $('#estimate-form').onsubmit = async e => {
      e.preventDefault(); const btn = $('#estimate-btn'); btn.disabled = true; btn.textContent = 'Estimating…';
      try {
        const fd = new FormData(e.target); if (!photo.files[0]) fd.delete('photo');
        const m = await api('/api/meals/estimate', { method: 'POST', body: fd });
        renderEstimate(m);
      } catch (err) { alert(err.message); } finally { btn.disabled = false; btn.textContent = 'Estimate'; }
    };
    function renderEstimate(m) {
      const box = $('#estimate-result'); box.hidden = false;
      const est = m.estimate || {};
      box.innerHTML = `<h3>${est.meal_name || m.description || 'Meal'}</h3>
        ${m.error ? `<div class="banner">Model estimate unavailable: ${m.error}. Enter values manually.</div>` : ''}
        <div class="metrics-row"><span>Carbs <b>${fmt(m.carbs_g)} g</b> <span class="range">(${fmt(m.carbs_low_g)}–${fmt(m.carbs_high_g)})</span></span><span>Fat <b>${fmt(m.fat_g)} g</b></span><span>Protein <b>${fmt(m.protein_g)} g</b></span><span>FPU <b>${fmt(m.fpu, 1)}</b></span><span>Confidence <b>${fmt((est.overall_confidence || 0) * 100)}%</b></span></div>
        ${est.assumptions?.length ? `<p class="small muted">Assumed: ${est.assumptions.join('; ')}</p>` : ''}
        ${est.questions?.length ? `<p class="small">Would help: ${est.questions.join(' ')}</p>` : ''}
        <div id="items"></div>
        <div class="row"><input id="notes" placeholder="notes (exercise, stress, site day…)" style="flex:1"><button id="confirm">Confirm meal</button></div>
        <p class="small muted">Insulin context: bolus IOB ${fmt(m.insulin_context?.iob, 2)} u now.</p>`;
      itemsEditor($('#items'), m.items.length ? m.items : [{ name: m.description || '', carbs_g: m.carbs_g }], m.kb_matches || {});
      $('#confirm').onclick = async () => {
        const items = readItems($('#items')).map(it => ({ ...it, user_corrected: !!it.user_corrected }));
        const r = await json(`/api/meals/${m.id}`, 'PUT', { items, notes: $('#notes').value, confirm: true, local_ts: $('#local_ts').value });
        location.href = `/meals/${r.id}`;
      };
    }
  }

  function responseChart(canvas, curves, baseline) {
    const ds = curves.map((c, i) => ({ label: c.label, data: c.points.map(p => ({ x: p.minutes, y: p.value })), parsing: false, borderColor: c.color || '#8fe3c4', borderWidth: c.width || 1.5, pointRadius: 0, tension: .2 }));
    const bands = { id: 'bands', beforeDraw(chart) { const { ctx, chartArea: a, scales: { y } } = chart; ctx.save(); ctx.fillStyle = 'rgba(90,209,168,.07)'; ctx.fillRect(a.left, y.getPixelForValue(180), a.width, y.getPixelForValue(70) - y.getPixelForValue(180)); ctx.restore(); } };
    if (canvas._chart) canvas._chart.destroy();
    canvas._chart = new Chart(canvas, { type: 'line', data: { datasets: ds }, plugins: [bands], options: { animation: false, scales: { x: { type: 'linear', min: -30, max: 360, title: { display: true, text: 'minutes after meal' }, ticks: { stepSize: 60 } }, y: { min: 40, max: 400 } }, plugins: { legend: { display: curves.length > 1, position: 'bottom' } } } });
  }

  function mealDetail() {
    const id = $('#meal').dataset.id;
    const load = async () => {
      const m = await api(`/api/meals/${id}`); const r = m.response || {};
      $('#title').textContent = `${m.description || 'Meal'} · ${m.local.replace('T', ' ').slice(0, 16)}`;
      $('#score').className = 'pill ' + (r.score || ''); $('#score').textContent = r.score ? `${r.score}: ${r.score_reason}` : (r.score_reason || 'no response yet');
      if (m.photo_url) { $('#photo').src = m.photo_url; $('#photo').hidden = false; }
      $('#macros').innerHTML = `<span>Carbs <b>${fmt(m.carbs_g)} g</b> <span class="range">${m.carbs_low_g !== m.carbs_high_g ? `(${fmt(m.carbs_low_g)}–${fmt(m.carbs_high_g)})` : ''}</span></span><span>Fat <b>${fmt(m.fat_g)} g</b></span><span>Protein <b>${fmt(m.protein_g)} g</b></span><span>Fiber <b>${fmt(m.fiber_g)} g</b></span><span>FPU <b>${fmt(m.fpu, 1)}</b></span><span>values: <b>${m.values_source}</b></span>${m.notes ? `<span>notes: <b>${m.notes}</b></span>` : ''}`;
      itemsEditor($('#items'), m.items, m.kb_matches); $('#items').insertAdjacentHTML('beforeend', ' <button type="button" id="save-items">Save corrections</button>');
      $('#save-items').onclick = async () => { const items = readItems($('#items')); await json(`/api/meals/${id}`, 'PUT', { items, confirm: true }); load(); };
      $('#insulin').innerHTML = kv({ 'Bolus for meal': r.bolus_units != null ? `${r.bolus_units} u` : 'none found', 'Pre-bolus': r.prebolus_min != null ? `${fmt(r.prebolus_min)} min` : '–', 'Carbs entered on pump': fmt(r.carbs_entered_on_pump), 'Bolus IOB at meal': fmt(r.iob_at_meal, 2) + ' u', 'Basal at meal': r.basal_rate_at_meal != null ? `${fmt(r.basal_rate_at_meal, 2)} U/h` : '–', 'All bolus insulin −1h..+6h': fmt(r.insulin_6h, 2) + ' u', 'Units per 10 g': r.bolus_units && m.carbs_g ? fmt(10 * r.bolus_units / m.carbs_g, 2) : '–' });
      $('#response').innerHTML = kv({ 'Baseline': `${fmt(r.baseline_mgdl)} (${r.baseline_trend > 0 ? '+' : ''}${fmt(r.baseline_trend, 1)}/min)`, 'Peak': `${fmt(r.peak_mgdl)} at ${fmt(r.time_to_peak_min)} min`, 'Δ 1h / 2h / 3h / 4h': `${fmt(r.delta_1h)} / ${fmt(r.delta_2h)} / ${fmt(r.delta_3h)} / ${fmt(r.delta_4h)}`, 'Min': fmt(r.min_mgdl), 'TIR 0–2h / 2–6h / 0–6h': `${fmt(r.tir_0_2h)}% / ${fmt(r.tir_2_6h)}% / ${fmt(r.tir_0_6h)}%`, 'iAUC 2h / 4h': `${fmt(r.iauc_2h)} / ${fmt(r.iauc_4h)}`, 'Low event': r.low_event ? 'yes' : 'no', 'Late rise (2–5h)': r.late_rise ? 'yes' : 'no', 'CGM coverage': fmt((r.coverage || 0) * 100) + '%' });
      responseChart($('#response-chart'), [{ label: 'this meal', points: m.curve || [] }], r.baseline_mgdl);
    };
    $('#recompute').onclick = async () => { await api(`/api/meals/${id}/recompute`, { method: 'POST' }); load(); };
    $('#delete').onclick = async () => { if (confirm('Delete this meal?')) { await api(`/api/meals/${id}`, { method: 'DELETE' }); location.href = '/meals'; } };
    load();
  }
  const kv = o => `<dl class="kv">${Object.entries(o).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>`;

  function foods() {
    const tbody = $('#food-table tbody');
    const render = async (q) => {
      const [foods, lb] = await Promise.all([api('/api/foods' + (q ? `?q=${encodeURIComponent(q)}` : '')), api('/api/foods/leaderboard')]);
      const L = Object.fromEntries(lb.map(x => [x.id, x]));
      tbody.innerHTML = foods.map(f => { const l = L[f.id] || {}; return `<tr><td><a href="/foods/${f.id}">${f.name}</a></td><td>${f.serving_desc}</td><td>${fmt(f.carbs_g)}</td><td>${fmt(f.fat_g)}</td><td>${fmt(f.protein_g)}</td><td class="muted">${f.provenance}</td><td>${f.times_eaten}</td><td>${l.pct_green != null ? l.pct_green + '%' : '–'}</td><td>${fmt(l.median_peak)}</td></tr>`; }).join('') || '<tr><td colspan="9" class="muted">No foods yet. They are created when you confirm meals.</td></tr>';
    };
    $('#food-search').oninput = e => render(e.target.value);
    $('#food-form').onsubmit = async e => { e.preventDefault(); const o = Object.fromEntries(new FormData(e.target).entries()); for (const k of ['carbs_g', 'fat_g', 'protein_g', 'fiber_g']) o[k] = o[k] === '' ? null : +o[k]; await json('/api/foods', 'POST', o); e.target.reset(); render(); };
    render();
  }

  function foodDetail() {
    const id = $('#food').dataset.id;
    const load = async () => {
      const f = await api(`/api/foods/${id}`); const o = f.outcomes || {};
      $('#title').textContent = f.name;
      $('#summary').textContent = `${f.serving_desc}: ${fmt(f.carbs_g)} g carbs · ${f.provenance} · eaten ${f.times_eaten}× · ${o.n_scored || 0} scored · ${o.pct_green != null ? o.pct_green + '% green' : ''} ${o.median_peak ? '· median peak ' + fmt(o.median_peak) : ''} ${o.late_rise_rate ? '· late rise ' + o.late_rise_rate + '%' : ''}`;
      const form = $('#food-edit'); for (const el of form.elements) if (el.name) el.value = f[el.name] ?? '';
      form.onsubmit = async e => { e.preventDefault(); const b = Object.fromEntries(new FormData(form).entries()); for (const k of ['carbs_g', 'fat_g', 'protein_g', 'fiber_g']) b[k] = b[k] === '' ? null : +b[k]; await json(`/api/foods/${id}`, 'PUT', b); load(); };
      $('#bolus-table tbody').innerHTML = (o.bolus_history || []).map(g => `<tr><td>${g.bolus}</td><td>${g.prebolus}</td><td>${g.n}</td><td>${g.green}</td><td>${g.amber}</td><td>${g.red}</td><td>${g.lows}</td><td>${fmt(g.median_peak)}</td><td>${fmt(g.median_delta_2h)}</td><td>${fmt(g.median_iob, 1)}</td></tr>`).join('') || '<tr><td colspan="10" class="muted">no scored meals yet</td></tr>';
      $('#meal-table tbody').innerHTML = (o.meals || []).map(m => `<tr><td><a href="/meals/${m.meal_id}">${m.local}</a></td><td>${fmt(m.carbs_g)}</td><td>${fmt(m.bolus_units, 1)}</td><td>${fmt(m.prebolus_min)}</td><td>${fmt(m.iob_at_meal, 1)}</td><td>${fmt(m.baseline)}</td><td>${fmt(m.peak)}</td><td>${fmt(m.delta_2h)}</td><td>${fmt(m.tir_0_6h)}</td><td>${pill(m.score)}</td></tr>`).join('');
      const curves = await Promise.all((o.meals || []).slice(0, 12).map(async m => { const d = await api(`/api/meals/${m.meal_id}`); return { label: `${m.local.slice(0, 10)} ${m.bolus_units ?? '?'}u`, points: d.curve, color: { green: '#5ad1a8', amber: '#f7b26a', red: '#ef6b73' }[m.score] || '#8b93a7' }; }));
      responseChart($('#overlay-chart'), curves);
    };
    load();
  }

  function insights() {
    const sel = $('#days');
    const render = async () => {
      const d = await api(`/api/insights/overview?days=${sel.value}`);
      $('#overall').innerHTML = metricsRow(d.overall);
      const h = d.hourly; const mk = (k) => h.map(x => x[k] ?? null);
      newChart('#hourly-chart', { type: 'line', data: { labels: h.map(x => x.hour), datasets: [
        { label: 'p95', data: mk('p95'), borderWidth: 0, backgroundColor: 'rgba(143,227,196,.12)', fill: '+1', pointRadius: 0 }, { label: 'p75', data: mk('p75'), borderWidth: 0, backgroundColor: 'rgba(143,227,196,.25)', fill: '+1', pointRadius: 0 },
        { label: 'median', data: mk('p50'), borderColor: '#5ad1a8', borderWidth: 2, pointRadius: 0 }, { label: 'p25', data: mk('p25'), borderWidth: 0, backgroundColor: 'rgba(143,227,196,.25)', fill: '+1', pointRadius: 0 }, { label: 'p5', data: mk('p5'), borderWidth: 0, pointRadius: 0 }] },
        options: { animation: false, scales: { y: { min: 40, max: 300 } }, plugins: { legend: { display: false } } } });
      $('#overnight').textContent = d.overnight.nights ? `Over ${d.overnight.nights} nights: median change 00–03h ${fmt(d.overnight.median_change_00_03)} mg/dL, 03–07h ${fmt(d.overnight.median_change_03_07)} mg/dL; rise >30 on ${fmt(d.overnight.dawn_rise_rate)}% of nights.` : '';
      newChart('#daily-chart', { type: 'bar', data: { labels: d.daily.map(x => x.date.slice(5)), datasets: [{ label: 'TIR', data: d.daily.map(x => x.tir), backgroundColor: '#5ad1a8' }, { label: 'Low', data: d.daily.map(x => x.tbr), backgroundColor: '#ef6b73' }, { label: 'High', data: d.daily.map(x => x.tar), backgroundColor: '#f2c14e' }] }, options: { animation: false, scales: { x: { stacked: true }, y: { stacked: true, max: 100 } }, plugins: { legend: { position: 'bottom' } } } });
      $('#slot-table tbody').innerHTML = d.meal_scores_by_slot.map(s => `<tr><td>${s.bucket}</td><td>${s.n}</td><td>${s.pct_green ?? '–'}</td><td>${s.pct_red ?? '–'}</td></tr>`).join('');
      $('#dow-table tbody').innerHTML = d.day_of_week.map(s => `<tr><td>${s.day}</td><td>${fmt(s.mean)}</td><td>${fmt(s.tir)}%</td><td>${fmt(s.tbr)}%</td></tr>`).join('');
      $('#food-table tbody').innerHTML = d.food_leaderboard.map(f => `<tr><td><a href="/foods/${f.id}">${f.name}</a></td><td>${f.n}</td><td>${f.green}</td><td>${f.amber}</td><td>${f.red}</td><td>${fmt(f.median_peak)}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">no confirmed meals yet</td></tr>';
      $('#isf-table tbody').innerHTML = d.sensitivity.map(s => `<tr><td>${s.bucket}</td><td>${s.n}</td><td>${s.observed_isf != null ? fmt(s.observed_isf) + ' mg/dL/u' : '–'}</td><td>${fmt(s.pump_isf)}</td></tr>`).join('');
      $('#icr-table tbody').innerHTML = d.carb_ratio.map(s => `<tr><td>${s.bucket}</td><td>${s.n}</td><td>${s.n_well_covered}</td><td>${s.observed_icr != null ? '1:' + fmt(s.observed_icr, 1) : '–'}</td><td>${s.pump_icr_used != null ? '1:' + fmt(s.pump_icr_used, 1) : '–'}</td><td>${s.under_dosed}</td><td>${s.over_dosed}</td></tr>`).join('');
      newChart('#drift-chart', { type: 'bar', data: { labels: d.basal_drift.map(x => x.hour), datasets: [{ label: 'median drift mg/dL per h', data: d.basal_drift.map(x => x.median_drift), backgroundColor: d.basal_drift.map(x => (x.median_drift || 0) > 0 ? '#f2c14e' : '#7aa2f7') }] }, options: { animation: false, plugins: { legend: { display: false }, tooltip: { callbacks: { afterLabel: ctx => `n=${d.basal_drift[ctx.dataIndex].n}` } } } } });
      const lc = d.site_lifecycle.length ? d.site_lifecycle : d.sensor_lifecycle;
      newChart('#lifecycle-chart', { type: 'line', data: { labels: lc.map(x => 'day ' + x.day), datasets: [{ label: 'TIR % by days since site change', data: lc.map(x => x.tir ?? null), borderColor: '#5ad1a8' }] }, options: { animation: false, scales: { y: { min: 0, max: 100 } }, plugins: { legend: { display: true } } } });
      if (!lc.length) $('#lifecycle-chart').insertAdjacentHTML('afterend', '<p class="muted small">Log site changes / sensor starts (Timeline → add event) to see TIR by device age.</p>');
    };
    sel.onchange = render; render();
  }
  function newChart(sel, cfg) { const c = $(sel); if (c._chart) c._chart.destroy(); c._chart = new Chart(c, cfg); }

  function importPage() {
    document.querySelectorAll('form.upload').forEach(f => {
      f.onsubmit = async e => { e.preventDefault(); const btn = f.querySelector('button'); btn.disabled = true; let out = f.querySelector('.result'); if (!out) { out = document.createElement('div'); out.className = 'result'; f.appendChild(out); }
        try { const r = await api(f.dataset.url, { method: 'POST', body: new FormData(f) }); const s = r.result; out.textContent = `Imported: ${s.glucose_added} glucose, ${s.bolus_added} boluses, ${s.basal_added} basal doses, ${s.events_added} events (skipped ${s.glucose_skipped + s.bolus_skipped + s.basal_skipped} duplicates). Range ${s.first_ts || '?'} → ${s.last_ts || '?'}. ${s.warnings.join(' ')}`; loadLog(); }
        catch (err) { out.textContent = 'Error: ' + err.message; } finally { btn.disabled = false; } };
    });
    const loadLog = () => api('/api/import/log').then(rows => { $('#import-log').innerHTML = rows.length ? `<table class="table"><tr><th>When</th><th>Source</th><th>File</th><th>Added</th></tr>${rows.map(r => `<tr><td>${r.ts.slice(0, 16).replace('T', ' ')}</td><td>${r.source}</td><td>${r.filename || ''}</td><td>${r.summary ? `${r.summary.glucose_added} g · ${r.summary.bolus_added} b · ${r.summary.basal_added} basal` : ''}</td></tr>`).join('')}</table>` : '<p class="muted">nothing imported yet</p>'; });
    loadLog();
    $('#ns-sync').onclick = async () => { $('#sync-result').textContent = 'syncing…'; try { const r = await api('/api/sync/nightscout', { method: 'POST' }); $('#sync-result').textContent = JSON.stringify(r.result); } catch (e) { $('#sync-result').textContent = e.message; } };
  }

  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/static/sw.js').catch(() => {});
  return { dashboard, timeline, mealList, mealNew, mealDetail, foods, foodDetail, insights, importPage };
})();
