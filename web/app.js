/* Мини-апп «Дневник КБЖУ». Без сборщика (§5.7): один файл, никаких зависимостей
   кроме telegram-web-app.js с telegram.org.

   Правило слоя: сервер считает, клиент рисует. Ни одного расчёта КБЖУ здесь
   быть не должно — это INV-1. */
(function () {
  'use strict';

  var tg = window.Telegram && window.Telegram.WebApp;
  var root = document.getElementById('screen');
  var nav = document.getElementById('tabs');
  var state = { tab: 'day', daysAgo: 0, open: {},
                cat: { q: '', offset: 0, view: 'list', product: null, dup: null } };

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
      var acts = '<div class="actions" style="margin-top:var(--s-2)">' +
        '<button data-repeat="' + m.id + '">Повторить</button>' +
        '<button data-drop="' + m.id + '" class="danger">Удалить</button></div>';
      return head + '<div class="items">' + items + macros + acts + '</div>';
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
        show(hero(d) + composer() + chips() + extra + feed(d));
        var inp = document.getElementById('say');
        if (inp && state.keepFocus) { inp.focus(); state.keepFocus = false; }
      })
      .catch(failure);
  }

  /* ── Продукты ────────────────────────────────────────────────────────── */

  var PAGE = 20;

  function renderCatalog() {
    var c = state.cat;
    if (c.view === 'product') return renderProduct();
    if (c.view === 'dups') return renderDups();
    document.getElementById('title').textContent = 'Продукты';
    loading();
    api('/api/products?limit=' + PAGE + '&offset=' + c.offset +
        (c.q ? '&q=' + encodeURIComponent(c.q) : ''))
      .then(function (d) {
        var rows = d.items.map(function (p) {
          var est = p.source === 'llm'
            ? '<span class="badge">оценка</span>'
            : (p.source === 'label' ? '<span class="badge">с этикетки</span>' : '');
          return '<button class="item" data-product="' + p.id + '">' +
            '<span class="nm">' + esc(p.name) + est + '</span>' +
            '<span class="num kk">' + n(p.kcal_per_100g) + ' ккал</span></button>';
        }).join('') || '<div class="state">Ничего не нашлось.</div>';
        var from = d.total ? c.offset + 1 : 0;
        var to = Math.min(c.offset + PAGE, d.total);
        show(
          '<div class="search">' +
            '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" ' +
              'stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/>' +
              '<path d="M20 20l-3.5-3.5" stroke-linecap="round"/></svg>' +
            '<input id="q" type="search" placeholder="Найти продукт" value="' + esc(c.q) + '">' +
          '</div>' +
          '<section class="card"><div class="plist">' + rows + '</div></section>' +
          '<div class="cap" style="text-align:center;margin-top:var(--s-3)">' +
            (d.total ? from + '–' + to + ' из ' + d.total : 'пусто') + '</div>' +
          '<div class="pager">' +
            '<button id="prev"' + (c.offset ? '' : ' disabled') + '>Назад</button>' +
            '<button id="dups">Найти дубли</button>' +
            '<button id="next"' + (to < d.total ? '' : ' disabled') + '>Вперёд</button>' +
          '</div>');
        var q = document.getElementById('q');
        if (state.keepFocus) { q.focus(); q.setSelectionRange(q.value.length, q.value.length);
                               state.keepFocus = false; }
      })
      .catch(failure);
  }

  function renderProduct() {
    loading();
    api('/api/products/' + state.cat.product).then(function (d) {
      var p = d.product;
      document.getElementById('title').textContent = 'Продукт';
      var why = { curated: 'выверено вручную', label: 'с этикетки',
                  manual: 'исправлено тобой', llm: 'оценка модели' }[p.source] || p.source;
      show('<section class="card">' +
        '<input id="pname" value="' + esc(p.name) + '" ' +
          'style="width:100%;background:var(--surface-2);border:1px solid var(--surface-3);' +
          'border-radius:var(--r-sm);color:var(--text);font:inherit;font-size:var(--f-body);' +
          'padding:11px;outline:none">' +
        '<div class="mic" style="margin-top:var(--s-2)">' + esc(why) +
          ' · съедено ' + n(p.uses) + ' раз</div>' +
        '<div style="margin-top:var(--s-4)">' +
          field('pk', 'Ккал / 100 г', p.kcal_per_100g) +
          field('pp', 'Белок, г', p.protein_per_100g) +
          field('pf', 'Жир, г', p.fat_per_100g) +
          field('pc', 'Углеводы, г', p.carb_per_100g) +
        '</div>' +
        '<button class="primary" id="save">Сохранить</button>' +
        '<div class="mic" style="margin-top:var(--s-3);line-height:1.5">' +
          'Правка меняет будущие записи. Уже съеденное не двинется — в истории ' +
          'хранится снимок на момент записи.</div>' +
        '</section>' +
        '<div class="pager"><button id="toList">К списку</button></div>');
    }).catch(failure);
  }

  function field(id, label, value) {
    return '<div class="field"><label for="' + id + '">' + esc(label) + '</label>' +
      '<input id="' + id + '" class="num" type="number" step="0.1" inputmode="decimal" ' +
      'value="' + esc(value) + '"></div>';
  }

  function renderDups() {
    loading();
    api('/api/products/duplicates/groups').then(function (d) {
      document.getElementById('title').textContent = 'Дубли';
      if (!d.groups.length) {
        show('<section class="card"><div class="state">Дублей не нашлось.</div></section>' +
             '<div class="pager"><button id="toList">К списку</button></div>');
        return;
      }
      state.cat.dup = state.cat.dup || {};
      var blocks = d.groups.map(function (g, gi) {
        var chosen = state.cat.dup[gi] == null ? g.suggested_winner : state.cat.dup[gi];
        var rows = g.items.map(function (p) {
          return '<div class="pick' + (p.id === chosen ? ' on' : '') +
            '" data-group="' + gi + '" data-pick="' + p.id + '">' +
            '<span class="dot"></span>' +
            '<span style="flex-grow:1;min-width:0"><span style="font-size:var(--f-body-s)">' +
              esc(p.name) + '</span>' +
              '<div class="mic">' + n(p.kcal_per_100g) + ' ккал · ' + esc(p.source) + '</div></span>' +
            '</div>';
        }).join('');
        var warn = g.safe ? '' :
          '<div class="mic" style="color:var(--warn);margin-top:var(--s-2);line-height:1.5">' +
          'Плотности разошлись на ' + n(g.spread * 100) + '%. Слияние заметно двинет ' +
          'будущие записи — проверь, точно ли это одна еда.</div>';
        return '<section class="card" style="margin-top:var(--s-3)">' +
          '<div class="cap" style="margin-bottom:var(--s-2)">Оставить одну запись</div>' +
          rows + warn +
          '<button class="primary" data-merge="' + gi + '">Слить в выбранную</button></section>';
      }).join('');
      show(blocks + '<div class="pager"><button id="toList">К списку</button></div>');
      state.cat._groups = d.groups;
    }).catch(failure);
  }

  var SOON = {
    tot: ['Итоги', 'Графики и сводки за период — следующий этап.'],
    ai:  ['AI', 'Разбор своих данных — последний этап.']
  };

  function render() {
    document.getElementById('nav-day').classList.toggle('hidden', state.tab !== 'day');
    if (state.tab === 'day') return renderDay();
    if (state.tab === 'cat') return renderCatalog();
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

  /* Частое — одна прокручиваемая строка, без «Ещё» (§6.3). Список считает
     сервер по медиане граммовки за 30 дней. */
  function chips() {
    var xs = state.frequent || [];
    if (!xs.length) return '';
    return '<div class="chips">' + xs.map(function (i) {
      return '<button class="chip" data-say="' + esc(i.name + ' ' + n(i.grams) + ' г') + '">' +
        esc(i.name) + ' <span class="g">· ' + n(i.grams) + ' г</span></button>';
    }).join('') + '</div>';
  }

  function loadFrequent() {
    /* Тихо: пустые чипы — не ошибка, ради них не стоит ломать экран. */
    api('/api/frequent?limit=10').then(function (d) {
      var before = (state.frequent || []).length;
      state.frequent = d.items;
      if (!before && d.items.length && state.tab === 'day') renderDay();
    }).catch(function () {});
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
    if (b.dataset.repeat) {
      loading();
      return send('/api/meals/' + b.dataset.repeat + '/repeat')
        .then(handle).catch(function (err) { toast(err.message, true); renderDay(); });
    }
    if (b.dataset.drop) {
      if (!window.confirm('Удалить этот приём?')) return;
      var did = b.dataset.drop;
      loading();
      return send('/api/meals/' + did, { method: 'DELETE' })
        .then(function () { delete state.open[did]; toast('Удалил.'); renderDay(); })
        .catch(function (err) { toast(err.message, true); renderDay(); });
    }
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
    /* ── каталог ─────────────────────────────────────────────────────── */
    if (b.dataset.product) {
      state.cat.view = 'product'; state.cat.product = b.dataset.product;
      return renderCatalog();
    }
    if (b.id === 'toList') {
      state.cat.view = 'list'; state.cat.product = null; state.cat.dup = null;
      return renderCatalog();
    }
    if (b.id === 'dups') { state.cat.view = 'dups'; state.cat.dup = null; return renderCatalog(); }
    if (b.id === 'prev') { state.cat.offset = Math.max(0, state.cat.offset - PAGE);
                           return renderCatalog(); }
    if (b.id === 'next') { state.cat.offset += PAGE; return renderCatalog(); }
    if (b.id === 'save') return saveProduct();
    if (b.dataset.merge != null) return doMerge(Number(b.dataset.merge));

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

  root.addEventListener('click', function (e) {
    var pick = e.target.closest('.pick');
    if (!pick) return;
    state.cat.dup = state.cat.dup || {};
    state.cat.dup[pick.dataset.group] = Number(pick.dataset.pick);
    renderCatalog();
  });

  function saveProduct() {
    var body = {
      name: document.getElementById('pname').value.trim(),
      kcal_per_100g: Number(document.getElementById('pk').value),
      protein_per_100g: Number(document.getElementById('pp').value),
      fat_per_100g: Number(document.getElementById('pf').value),
      carb_per_100g: Number(document.getElementById('pc').value)
    };
    loading();
    send('/api/products/' + state.cat.product, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function () {
      toast('Сохранил. Прошлые записи не изменились.');
      state.cat.view = 'list';
      renderCatalog();
    }).catch(function (e) { toast(e.message, true); renderCatalog(); });
  }

  function doMerge(gi) {
    var g = state.cat._groups[gi];
    var chosen = (state.cat.dup && state.cat.dup[gi]) || g.suggested_winner;
    var losers = g.items.map(function (p) { return p.id; })
                        .filter(function (id) { return id !== chosen; });
    if (!losers.length) return;
    /* Необратимо: проигравшие строки каталога исчезают. Спрашиваем один раз,
       но спрашиваем — вернуть их можно только из бэкапа. */
    if (!window.confirm('Слить ' + losers.length + ' запис' +
        (losers.length === 1 ? 'ь' : 'и') + ' в выбранную? Отменить будет нельзя.')) return;
    loading();
    send('/api/products/merge', {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ winner_id: chosen, loser_ids: losers })
    }).then(function (r) {
      toast('Слил. Перепривязано записей: ' + r.items_repointed + '.');
      state.cat.dup = null;
      renderCatalog();
    }).catch(function (e) { toast(e.message, true); renderCatalog(); });
  }

  var searchTimer = null;

  root.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && e.target.id === 'say') { e.preventDefault(); submitText(); }
  });

  root.addEventListener('input', function (e) {
    if (e.target.id === 'say') state.text = e.target.value;
    if (e.target.id === 'q') {
      /* Ждём паузу в наборе: запрос на каждый символ — это 15 запросов
         на слово и мигающий список. */
      state.cat.q = e.target.value;
      state.cat.offset = 0;
      state.keepFocus = true;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(renderCatalog, 300);
    }
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
  loadFrequent();
  if (tg.setHeaderColor) { try { tg.setHeaderColor('#000000'); } catch (e) { /* старый клиент */ } }
  render();
})();
