/* Мини-апп «Дневник КБЖУ». Без сборщика (§5.7): один файл, никаких зависимостей
   кроме telegram-web-app.js с telegram.org.

   Правило слоя: сервер считает, клиент рисует. Ни одного расчёта КБЖУ здесь
   быть не должно — это INV-1. */
(function () {
  'use strict';

  var tg = window.Telegram && window.Telegram.WebApp;
  var root = document.getElementById('screen');
  var nav = document.getElementById('tabs');
  var state = { tab: 'day', daysAgo: 0, open: {} };

  // Проверка идёт ДО регистрации слушателей: иначе на экране «открой из
  // Telegram» остаются живые обработчики, которые падают с невнятной ошибкой.
  if (!tg || !tg.initData) {
    root.innerHTML = '<div class="card state err"><b>Открой из Telegram</b>' +
      '<div class="mic" style="margin-top:6px">Аппка работает только внутри бота — ' +
      'снаружи подпись не выдать.</div></div>';
    return;
  }

  /* ── утилиты ─────────────────────────────────────────────────────────── */

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* Числа по-русски: запятая, а не точка. Целые — без хвоста «,0». */
  function n(v, digits) {
    if (v == null || isNaN(v)) return '—';
    var d = digits == null ? 0 : digits;
    return Number(v).toFixed(d).replace('.', ',').replace(/,0+$/, '');
  }

  function pct(part, whole) {
    if (!whole) return 0;
    return Math.max(0, Math.min(100, (part / whole) * 100));
  }

  function api(path) {
    if (!tg || !tg.initData) return Promise.reject(new Error('no-tg'));
    return fetch(path, { headers: { 'X-Telegram-Init-Data': tg.initData } })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          if (r.ok) return body;
          var err = (body.detail && body.detail.error) || {};
          var e = new Error(err.message || 'Сервер ответил ' + r.status);
          e.code = err.code;
          throw e;
        });
      });
  }

  function send(path, opts) {
    if (!tg || !tg.initData) return Promise.reject(new Error('no-tg'));
    var o = opts || {};
    o.method = o.method || 'POST';
    o.headers = o.headers || {};
    o.headers['X-Telegram-Init-Data'] = tg.initData;
    return fetch(path, o).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (r.ok) return body;
        var err = (body.detail && body.detail.error) || {};
        var e = new Error(err.message || 'Сервер ответил ' + r.status);
        e.code = err.code;
        throw e;
      });
    });
  }

  var toastTimer = null;
  function toast(text, bad) {
    var el = document.getElementById('toast');
    el.textContent = text;
    el.className = 'toast' + (bad ? ' bad' : '');
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 3500);
  }

  function show(html) { root.innerHTML = html; }

  function loading() {
    show('<div class="card">' +
         '<div class="skel" style="height:14px;width:45%"></div>' +
         '<div class="skel" style="height:34px;width:65%;margin-top:10px"></div>' +
         '<div class="skel" style="height:6px;margin-top:14px"></div>' +
         '</div>');
  }

  function failure(e) {
    var hint = e.code === 'init_data_expired'
      ? 'Сессия истекла. Закрой и открой аппку из бота.'
      : e.code === 'inactive'
        ? 'Доступ к аппке отключён.'
        : e.message === 'Failed to fetch'
          ? 'Нет связи. Проверь интернет и потяни вниз, чтобы обновить.'
          : e.message;
    show('<div class="card state err"><b>Не загрузилось</b>' +
         '<div class="mic" style="margin-top:6px">' + esc(hint) + '</div></div>');
  }

  /* ── верхняя панель Е (§6.2) ─────────────────────────────────────────── */

  function hero(d) {
    var t = d.totals, g = d.goal;
    var kcalLeft = t.kcal_left, protLeft = t.protein_left;
    var over = kcalLeft < 0;
    return '' +
      '<section class="card">' +
        '<div class="hero">' +
          '<div>' +
            '<div class="lbl">' + (over ? 'Перебор ккал' : 'Осталось ккал') + '</div>' +
            '<div class="num big" style="margin-top:4px' + (over ? ';color:var(--danger)' : '') + '">' +
              n(Math.abs(kcalLeft)) + '</div>' +
            '<div class="track"><i style="width:' + pct(t.kcal, g.kcal) + '%"></i></div>' +
            '<div class="mic" style="margin-top:6px">' + n(t.kcal) + ' из ' + n(g.kcal) + '</div>' +
          '</div>' +
          '<div>' +
            '<div class="lbl">Осталось белка</div>' +
            '<div style="display:flex;align-items:baseline;gap:4px;margin-top:4px">' +
              '<span class="num big" style="color:var(--warn)">' + n(Math.max(0, protLeft)) + '</span>' +
              '<span class="num unit">г</span></div>' +
            '<div class="track"><i style="width:' + pct(t.protein_g, g.protein_g) + '%;background:var(--prot)"></i></div>' +
            '<div class="mic" style="margin-top:6px;color:var(--warn)">' +
              n(t.protein_g) + ' из ' + n(g.protein_g) + ' г</div>' +
          '</div>' +
        '</div>' +
        '<div class="sep" style="margin:var(--s-4) 0 0"></div>' +
        '<div class="foot">' +
          foot('Жир', t.fat_g, g.fat_g, 'left') +
          foot('Углеводы', t.carb_g, g.carb_g, 'left') +
          foot('Приёмов', d.meals.length, null, 'right') +
        '</div>' +
      '</section>';
  }

  function foot(label, value, goal, align) {
    return '<div style="text-align:' + align + '">' +
      '<div class="mic">' + esc(label) + '</div>' +
      '<div style="margin-top:3px"><span class="num" style="font-size:var(--f-body);font-weight:600">' +
        n(value) + '</span>' +
      (goal ? '<span class="num den"> /' + n(goal) + '</span>' : '') + '</div></div>';
  }

  /* ── лента приёмов ───────────────────────────────────────────────────── */

  function feed(d) {
    if (!d.meals.length) {
      return '<section class="card" style="margin-top:var(--s-3)">' +
             '<div class="state">За этот день записей нет.</div></section>';
    }
    var rows = d.meals.map(function (m) {
      var opened = !!state.open[m.id];
      var mark = m.estimated ? ' <span style="color:var(--text-faint)">≈</span>' : '';
      var head =
        '<button class="meal" data-meal="' + m.id + '" aria-expanded="' + opened + '">' +
          '<span class="num t">' + esc(m.at) + '</span>' +
          '<span class="n">' + esc(m.raw_input || m.items.map(function (i) { return i.name; }).join(', ')) + mark + '</span>' +
          '<span class="num k">' + n(m.kcal) + '</span>' +
        '</button>';
      if (!opened) return head;
      var items = m.items.map(function (i) {
        return '<div class="row">' +
          '<span style="flex-grow:1">' + esc(i.name) +
            (i.is_estimate ? ' <span style="color:var(--text-faint)">≈</span>' : '') + '</span>' +
          '<span class="num g">' + n(i.grams) + ' г</span>' +
          '<span class="num" style="width:52px;text-align:right">' + n(i.kcal) + '</span>' +
        '</div>';
      }).join('');
      var macros = '<div class="row" style="border-top:1px solid var(--separator);margin-top:4px;padding-top:8px">' +
        '<span style="flex-grow:1">Б ' + n(m.protein_g) + ' · Ж ' + n(m.fat_g) + ' · У ' + n(m.carb_g) + '</span></div>';
      return head + '<div class="items">' + items + macros + '</div>';
    }).join('');
    return '<section class="card" style="margin-top:var(--s-3)">' +
           '<div class="cap" style="margin-bottom:var(--s-2)">Съедено</div>' + rows + '</section>';
  }

  /* ── экраны ──────────────────────────────────────────────────────────── */

  function title(d) {
    if (state.daysAgo === 0) return 'Сегодня';
    if (state.daysAgo === 1) return 'Вчера';
    var parts = String(d.day).split('-');
    var months = ['янв','фев','мар','апр','мая','июн','июл','авг','сен','окт','ноя','дек'];
    return Number(parts[2]) + ' ' + months[Number(parts[1]) - 1];
  }

  function renderDay() {
    loading();
    api('/api/day?days_ago=' + state.daysAgo)
      .then(function (d) {
        document.getElementById('title').textContent = title(d);
        document.getElementById('nav-day').classList.toggle('hidden', false);
        document.getElementById('fwd').disabled = state.daysAgo === 0;
        var extra = '';
        if (state.justLogged) extra = justCard(state.justLogged);
        else if (state.clarify) extra = clarifyCard(state.clarify);
        show(hero(d) + composer() + chips(d) + extra + feed(d));
        var inp = document.getElementById('say');
        if (inp && state.keepFocus) { inp.focus(); state.keepFocus = false; }
      })
      .catch(failure);
  }

  var SOON = {
    cat: ['Продукты', 'Каталог с поиском и правкой — следующий этап.'],
    tot: ['Итоги', 'Графики и сводки за период — следующий этап.'],
    ai:  ['AI', 'Разбор своих данных — последний этап.']
  };

  function render() {
    document.getElementById('nav-day').classList.toggle('hidden', state.tab !== 'day');
    if (state.tab === 'day') return renderDay();
    var s = SOON[state.tab];
    document.getElementById('title').textContent = s[0];
    show('<section class="card"><div class="state">' + esc(s[1]) + '</div></section>');
  }

  /* ── ввод ────────────────────────────────────────────────────────────── */

  function composer() {
    var rec = state.recording;
    return '<div class="composer">' +
      '<input id="say" type="text" inputmode="text" autocomplete="off" ' +
        'placeholder="Что съел? Или скажи голосом" value="' + esc(state.text || '') + '">' +
      '<button id="mic" class="' + (rec ? 'rec' : '') + '" ' +
        'aria-label="' + (rec ? 'Остановить запись' : 'Записать голосом') + '">' +
        (rec
          ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
            '<rect x="6" y="6" width="12" height="12" rx="2"/></svg>'
          : '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
            'stroke-width="2" stroke-linecap="round" aria-hidden="true">' +
            '<rect x="9" y="3" width="6" height="11" rx="3"/>' +
            '<path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>') +
      '</button></div>';
  }

  /* Частое — одна прокручиваемая строка, без «Ещё» (§6.3). Список приходит
     с сервера; пока эндпоинта нет, берём то, что уже съедено за день. */
  function chips(d) {
    var seen = {}, out = [];
    (d.meals || []).forEach(function (m) {
      m.items.forEach(function (i) {
        if (seen[i.name] || out.length >= 10) return;
        seen[i.name] = 1;
        out.push('<button class="chip" data-say="' + esc(i.name + ' ' + n(i.grams) + ' г') + '">' +
                 esc(i.name) + ' <span class="g">· ' + n(i.grams) + ' г</span></button>');
      });
    });
    return out.length ? '<div class="chips">' + out.join('') + '</div>' : '';
  }

  /* Пишем сразу, превью не показываем (решение владельца): записанное можно
     тут же поправить, а лишний клик стоит дороже ошибки. */
  function justCard(m) {
    var items = m.items.map(function (i) {
      return '<div class="row"><span style="flex-grow:1">' + esc(i.name) +
        (i.is_estimate ? ' <span style="color:var(--text-faint)">≈</span>' : '') + '</span>' +
        '<span class="num g">' + n(i.grams) + ' г</span>' +
        '<span class="num" style="width:52px;text-align:right">' + n(i.kcal) + '</span></div>';
    }).join('');
    return '<section class="card just" style="margin-top:var(--s-3)">' +
      '<div style="display:flex;align-items:baseline;gap:var(--s-2)">' +
        '<span class="ttl" style="flex-grow:1">Записал</span>' +
        '<span class="num" style="font-weight:600">' + n(m.kcal) + ' ккал</span></div>' +
      '<div class="items" style="padding-left:0;padding-bottom:0">' + items + '</div>' +
      '<div class="actions">' +
        '<button data-undo="' + m.id + '" class="danger">Отменить</button></div>' +
      '</section>';
  }

  function clarifyCard(c) {
    var opts = (c.options || []).map(function (o) {
      return '<button data-answer="' + esc(o) + '">' + esc(o) + '</button>';
    }).join('');
    return '<section class="card just" style="margin-top:var(--s-3)">' +
      '<div class="ttl">Уточни: ' + esc(c.item_name) + '</div>' +
      '<div class="opts">' + opts +
        '<button data-answer="" data-cancel="1" style="border-color:var(--surface-3);' +
        'background:var(--surface-2)">Отмена</button></div></section>';
  }

  function handle(res) {
    state.clarify = null;
    state.justLogged = null;
    if (res.kind === 'logged') {
      state.justLogged = res.meal;
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
    } else if (res.kind === 'clarify') {
      state.clarify = res;
    } else if (res.kind === 'weight') {
      var d = res.delta_kg;
      toast('Вес ' + n(res.weight_kg, 1) + ' кг' +
            (d ? ' · ' + (d > 0 ? '+' : '') + n(d, 1) + ' кг' : ''));
    } else if (res.kind === 'stats') {
      toast('Это вопрос, а не запись — цифры сверху обновлены.');
    } else {
      toast(res.message || 'Не понял, что записать.', true);
    }
    state.daysAgo = 0;
    renderDay();
  }

  function submitText() {
    var inp = document.getElementById('say');
    var text = (inp && inp.value || '').trim();
    if (!text) return;
    state.text = '';
    inp.value = '';
    inp.blur();
    loading();
    send('/api/meals', {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text })
    }).then(handle).catch(function (e) { toast(e.message, true); renderDay(); });
  }

  /* ── голос ───────────────────────────────────────────────────────────── */

  var recorder = null, chunks = [];

  function startRec() {
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      toast('Этот клиент не умеет записывать голос — набери текстом.', true);
      return;
    }
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = function (e) { if (e.data.size) chunks.push(e.data); };
      recorder.onstop = function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        state.recording = false;
        var blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
        if (!blob.size) { renderDay(); return; }
        loading();
        send('/api/meals/voice', {
          headers: { 'Content-Type': blob.type },
          body: blob
        }).then(handle).catch(function (e) { toast(e.message, true); renderDay(); });
      };
      recorder.start();
      state.recording = true;
      renderDay();
    }).catch(function () {
      toast('Не дали доступ к микрофону — набери текстом.', true);
    });
  }

  function stopRec() { if (recorder && recorder.state !== 'inactive') recorder.stop(); }

  /* ── события ─────────────────────────────────────────────────────────── */

  nav.addEventListener('click', function (e) {
    var b = e.target.closest('button[data-tab]');
    if (!b) return;
    state.tab = b.dataset.tab;
    state.daysAgo = 0;
    [].forEach.call(nav.querySelectorAll('button'), function (x) {
      if (x.dataset.tab === state.tab) x.setAttribute('aria-current', 'page');
      else x.removeAttribute('aria-current');
    });
    if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
    render();
  });

  root.addEventListener('click', function (e) {
    var b = e.target.closest('button');
    if (!b) return;

    if (b.dataset.meal) {
      state.open[b.dataset.meal] = !state.open[b.dataset.meal];
      return render();
    }
    if (b.id === 'mic') return state.recording ? stopRec() : startRec();
    if (b.dataset.say) {
      var inp = document.getElementById('say');
      inp.value = b.dataset.say;
      state.keepFocus = true;
      return submitText();
    }
    if (b.dataset.undo) {
      var id = b.dataset.undo;
      loading();
      return send('/api/meals/' + id, { method: 'DELETE' })
        .then(function () { state.justLogged = null; toast('Отменил.'); renderDay(); })
        .catch(function (err) { toast(err.message, true); renderDay(); });
    }
    if (b.hasAttribute('data-answer')) {
      if (b.dataset.cancel) { state.clarify = null; return renderDay(); }
      var c = state.clarify;
      loading();
      return send('/api/meals/clarify', {
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ draft_id: c.draft_id, answer: b.dataset.answer })
      }).then(handle).catch(function (err) { toast(err.message, true); renderDay(); });
    }
  });

  root.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && e.target.id === 'say') { e.preventDefault(); submitText(); }
  });

  root.addEventListener('input', function (e) {
    if (e.target.id === 'say') state.text = e.target.value;
  });

  document.getElementById('back').addEventListener('click', function () {
    state.daysAgo += 1; state.open = {}; renderDay();
  });
  document.getElementById('fwd').addEventListener('click', function () {
    if (state.daysAgo === 0) return;
    state.daysAgo -= 1; state.open = {}; renderDay();
  });

  /* ── старт ───────────────────────────────────────────────────────────── */

  tg.ready();
  tg.expand();
  if (tg.setHeaderColor) { try { tg.setHeaderColor('#000000'); } catch (e) { /* старый клиент */ } }
  render();
})();
