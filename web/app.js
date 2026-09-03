/* Мини-апп «Дневник КБЖУ». Без сборщика (§5.7): один файл, никаких зависимостей
   кроме telegram-web-app.js с telegram.org.

   Правило слоя: сервер считает, клиент рисует. Ни одного расчёта КБЖУ здесь
   быть не должно — это INV-1. */
(function () {
  'use strict';

  var tg = window.Telegram && window.Telegram.WebApp;
  var root = document.getElementById('screen');
  var nav = document.getElementById('tabs');
  var state = { tab: 'day', daysAgo: 0, open: {}, prefs: null,
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

  /* Telegram в части клиентов глушит window.confirm — он молча возвращает
     false, и кнопка выглядит мёртвой. Спрашиваем родным диалогом, а confirm
     оставляем запасным путём для обычного браузера. */
  function ask(text, then) {
    if (tg && tg.showConfirm) {
      try { return tg.showConfirm(text, function (ok) { if (ok) then(); }); }
      catch (e) { /* старый клиент — падаем на confirm */ }
    }
    if (window.confirm(text)) then();
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

  /* ── верхняя панель: вид выбирается в настройках (§6.5) ──────────────── */

  function hero(d) {
    var kind = (state.prefs && state.prefs.hero) || 'bars';
    if (kind === 'rings') return heroRings(d);
    if (kind === 'kcal') return heroKcal(d);
    return heroBars(d);
  }

  /* Кольца: все четыре макроса разом, как в Apple Fitness. */
  function heroRings(d) {
    var t = d.totals, g = d.goal;
    var data = [
      [52, 'var(--bar)', pct(t.kcal, g.kcal), 'Ккал', t.kcal, g.kcal],
      [41, 'var(--prot)', pct(t.protein_g, g.protein_g), 'Белок', t.protein_g, g.protein_g],
      [30, 'var(--fat)', pct(t.fat_g, g.fat_g), 'Жир', t.fat_g, g.fat_g],
      [19, 'var(--carb)', pct(t.carb_g, g.carb_g), 'Углеводы', t.carb_g, g.carb_g]
    ];
    var rings = data.map(function (r) {
      var c = 2 * Math.PI * r[0], on = Math.round(c * r[2] / 100);
      return '<circle cx="60" cy="60" r="' + r[0] + '" fill="none" stroke="var(--surface-3)" ' +
             'stroke-width="9"/><circle cx="60" cy="60" r="' + r[0] + '" fill="none" stroke="' +
             r[1] + '" stroke-width="9" stroke-linecap="round" stroke-dasharray="' + on + ' ' +
             Math.round(c - on) + '" transform="rotate(-90 60 60)"/>';
    }).join('');
    var rows = data.map(function (r) {
      return '<div style="display:flex;align-items:baseline;gap:6px">' +
        '<span class="mic" style="flex-grow:1">' + esc(r[3]) + '</span>' +
        '<span class="num" style="font-size:var(--f-body-s);font-weight:600">' + n(r[4]) + '</span>' +
        '<span class="num" style="font-size:var(--f-micro);color:var(--text-faint)">/' +
        n(r[5]) + '</span></div>';
    }).join('');
    return '<section class="card"><div style="display:flex;align-items:center;gap:var(--s-4)">' +
      '<svg width="120" height="120" viewBox="0 0 120 120" aria-hidden="true" ' +
      'style="flex-shrink:0">' + rings + '</svg>' +
      '<div style="flex-grow:1;min-width:0;display:flex;flex-direction:column;gap:6px">' +
      rows + '</div></div>' + heroFoot(d) + '</section>';
  }

  /* Минимум: только остаток калорий, белок уходит в подпись. */
  function heroKcal(d) {
    var t = d.totals, g = d.goal, over = t.kcal_left < 0;
    return '<section class="card">' +
      '<div class="num big" style="font-size:var(--f-hero);font-weight:700;' +
      'letter-spacing:-1.5px;line-height:1' + (over ? ';color:var(--danger)' : '') + '">' +
      n(Math.abs(t.kcal_left)) + '</div>' +
      '<div class="cap" style="margin-top:3px">' +
      (over ? 'ккал перебор' : 'ккал осталось') + ' · белка ещё ' +
      n(Math.max(0, t.protein_left)) + ' г</div>' +
      '<div class="track"><i style="width:' + pct(t.kcal, g.kcal) + '%"></i></div>' +
      '</section>';
  }

  function heroFoot(d) {
    var t = d.totals, g = d.goal;
    var third = (state.prefs && state.prefs.foot_third) === 'weight'
      ? foot('Вес', state.weight, null, 'right')
      : foot('Приёмов', d.meals.length, null, 'right');
    return '<div class="sep" style="margin:var(--s-4) 0 0"></div>' +
      '<div class="foot">' + foot('Жир', t.fat_g, g.fat_g, 'left') +
      foot('Углеводы', t.carb_g, g.carb_g, 'left') + third + '</div>';
  }

  function heroBars(d) {
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
        heroFoot(d) +
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


  /* ── Итоги: графики инлайн-SVG, без библиотек (§5.7) ─────────────────── */

  var PERIODS = [[7, 'Неделя'], [30, '30 дней'], [90, '3 месяца']];

  function renderTotals() {
    document.getElementById('title').textContent = 'Итоги';
    loading();
    var days = state.period || (state.prefs && state.prefs.period) || 30;
    var nothing = function () { return null; };
    Promise.all([
      api('/api/stats/series?days=' + days),
      api('/api/stats/weight?limit=60').catch(function () { return { points: [] }; }),
      api('/api/stats/reconcile?days=' + Math.max(days, 14)).catch(nothing),
      api('/api/stats/contributions?days=' + days).catch(nothing),
      api('/api/stats/accuracy?days=' + days).catch(nothing)
    ]).then(function (res) {
      var d = res[0], w = res[1], rec = res[2], con = res[3], acc = res[4];
      show(periods() +
           reconcileCard(rec) +
           card('Белок по дням', barChart(d, 'protein_g', d.goal.protein_g, 'var(--prot)'),
                'Среднее ' + n(avg(d, 'protein_g')) + ' г при цели ' + n(d.goal.protein_g) + '.') +
           card('Ккал по дням', barChart(d, 'kcal', d.goal.kcal, 'var(--bar)'),
                'Красным — величина перебора.') +
           card('Полнота записей', heat(d),
                'Светлое — записано много, тёмное — почти пусто. Средние выше ' +
                'считаются по дням, где есть хоть одна запись.') +
           (w.points.length > 1
             ? card('Вес', line(w.points), n(w.points.length) + ' замеров.')
             : '') +
           contribCard(con) +
           accuracyCard(acc));
    }).catch(failure);
  }

  /* Флагманская панель §4.0: сходится ли дневник с весами.
     Не обвиняет и не считает «сколько недозаписал» — кладёт рядом два факта. */
  function reconcileCard(r) {
    if (!r) return '';
    if (r.verdict === 'no_weight') {
      return card('Дневник и весы',
        '<div class="state">Нужны хотя бы два замера веса за период.</div>',
        'Без веса сверить записанное не с чем.');
    }
    if (r.verdict === 'not_enough_data') {
      return card('Дневник и весы',
        '<div class="state">Данных пока мало.</div>',
        'На коротком окне вес говорит больше про воду, чем про питание. ' +
        'Вернись к этой панели через пару недель.');
    }
    var agree = r.agrees;
    var head = agree ? 'Сходится' : 'Расходится';
    var color = agree ? 'var(--ok)' : 'var(--warn)';
    var delta = r.weight_delta_kg;
    var sign = delta > 0 ? '+' : '';
    var body =
      '<div style="display:flex;align-items:baseline;gap:var(--s-2)">' +
        '<span style="font-size:var(--f-t3);font-weight:600;color:' + color + '">' +
        esc(head) + '</span></div>' +
      '<div class="field"><label>Записано в среднем</label>' +
        '<span class="num" style="font-size:var(--f-body-s)">' +
        n(r.logged_kcal_per_day) + ' ккал/день</span></div>' +
      '<div class="field"><label>Вес за период</label>' +
        '<span class="num" style="font-size:var(--f-body-s)">' + sign + n(delta, 1) +
        ' кг</span></div>' +
      (agree ? '' :
        '<div class="field"><label>Не объясняется записями</label>' +
        '<span class="num" style="font-size:var(--f-body-s);color:var(--warn)">' +
        '≈' + n(Math.abs(r.gap_kcal_per_day)) + ' ккал/день</span></div>');
    var note = agree
      ? 'Записанное и вес говорят одно и то же. Цифрам можно верить.'
      : 'Вес изменился сильнее, чем объясняет дневник. Обычно это значит, ' +
        'что часть съеденного не попала в записи. Оценка грубая: килограмм ' +
        'ткани считаем за 7700 ккал, а вода и гликоген гуляют сами по себе.';
    return card('Дневник и весы', body, note);
  }

  /* §3.3: абсолютный вклад в калории, а НЕ доля макросов. Доли нормируются
     на съеденное — чем меньше ешь, тем лучше картина, и метрика прячет
     ровно ту проблему, ради которой экран нужен (§4.1). */
  function contribCard(c) {
    if (!c || !c.items.length) return '';
    var max = c.items[0].kcal || 1;
    var rows = c.items.map(function (i) {
      return '<div style="padding:7px 0">' +
        '<div style="display:flex;align-items:baseline;gap:var(--s-2)">' +
          '<span style="flex-grow:1;min-width:0;overflow:hidden;text-overflow:ellipsis;' +
          'white-space:nowrap;font-size:var(--f-body-s)">' + esc(i.name) + '</span>' +
          '<span class="num" style="font-size:var(--f-caption)">' + n(i.kcal) + ' ккал</span>' +
          '<span class="mic" style="width:44px;text-align:right">' + n(i.times) + '×</span>' +
        '</div>' +
        '<div class="track" style="margin-top:5px"><i style="width:' +
          (i.kcal / max * 100) + '%"></i></div></div>';
    }).join('');
    return card('Куда уходят калории', rows,
      'Столько калорий пришло с каждым продуктом за период. Не доля, а сумма: ' +
      'долю легко улучшить, просто съев меньше.');
  }

  /* §3.5: доверие к цифре — какая часть калорий оценена, а не выверена. */
  function accuracyCard(a) {
    if (!a || !a.total_kcal) return '';
    var share = Math.round(a.estimated_share * 100);
    var body =
      '<div class="num" style="font-size:var(--f-t1);font-weight:700;letter-spacing:-0.8px">' +
        n(100 - share) + '%</div>' +
      '<div class="cap" style="margin-top:2px">калорий из выверенных цифр</div>' +
      '<div class="track" style="margin-top:var(--s-3)"><i style="width:' +
        (100 - share) + '%"></i></div>';
    return card('Доверие к цифре', body,
      share > 50
        ? 'Больше половины калорий — оценка модели. Поправь плотности у самых ' +
          'частых продуктов во вкладке «Продукты», и цифра станет твёрже.'
        : 'Оценок ' + n(share) + '%. Позиции со знаком ≈ можно уточнить в каталоге.');
  }

  function periods() {
    return '<div class="periods">' + PERIODS.map(function (p) {
      return '<button data-period="' + p[0] + '" aria-pressed="' +
        ((state.period || 30) === p[0]) + '">' + p[1] + '</button>';
    }).join('') + '</div>';
  }

  function card(title, body, note) {
    return '<section class="card" style="margin-top:var(--s-3)">' +
      '<div class="cap">' + esc(title) + '</div>' +
      '<div class="chart">' + body + '</div>' +
      '<div class="mic note">' + esc(note) + '</div></section>';
  }

  function avg(d, key) {
    var xs = d.days.filter(function (x) { return x.meals > 0; });
    if (!xs.length) return 0;
    return xs.reduce(function (a, x) { return a + x[key]; }, 0) / xs.length;
  }

  /* Столбики и линия цели считаются в ОДНОМ масштабе: разные масштабы уже
     давали «зелёный» столбик выше линии цели — график врал глазу. */
  function barChart(d, key, goal, color) {
    var H = 96, W = 300, xs = d.days;
    var max = Math.max(goal, ...xs.map(function (x) { return x[key]; })) || 1;
    var bw = W / xs.length, gap = Math.min(3, bw * 0.25);
    var y = function (v) { return H - (v / max) * H; };
    var bars = xs.map(function (x, i) {
      var v = x[key], over = v > goal;
      var base = Math.min(v, goal), extra = over ? v - goal : 0;
      var out = '<rect x="' + (i * bw + gap / 2) + '" y="' + y(base) + '" width="' + (bw - gap) +
        '" height="' + (H - y(base)) + '" rx="2" fill="' + color + '"/>';
      if (extra) {
        out += '<rect x="' + (i * bw + gap / 2) + '" y="' + y(v) + '" width="' + (bw - gap) +
          '" height="' + (y(goal) - y(v)) + '" rx="2" fill="var(--danger)"/>' +
          '<rect x="' + (i * bw + gap / 2) + '" y="' + (y(goal) - 1) + '" width="' + (bw - gap) +
          '" height="1" fill="var(--surface)"/>';
      }
      return out;
    }).join('');
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" role="img" ' +
      'aria-label="Столбчатый график, ' + xs.length + ' дней">' + bars +
      '<line x1="0" y1="' + y(goal) + '" x2="' + W + '" y2="' + y(goal) +
      '" stroke="var(--line)" stroke-width="1" stroke-dasharray="4 4"/></svg>' +
      '<div class="legend"><span><i style="background:' + color + '"></i>съедено</span>' +
      '<span><i style="background:var(--danger)"></i>перебор</span>' +
      '<span><i style="background:var(--line)"></i>цель ' + n(goal) + '</span></div>';
  }

  function heat(d) {
    var goal = d.goal.kcal;
    var cells = d.days.map(function (x) {
      var c = x.kcal >= goal * 0.9 ? '#EDF1F5'
            : x.kcal >= goal * 0.6 ? '#7C8899'
            : 'var(--surface-3)';
      return '<div style="background:' + c + '" title="' + esc(x.date) + '"></div>';
    }).join('');
    return '<div class="heat">' + cells + '</div>' +
      '<div class="legend"><span><i style="background:#EDF1F5"></i>записано много</span>' +
      '<span><i style="background:#7C8899"></i>частично</span>' +
      '<span><i style="background:var(--surface-3)"></i>мало</span></div>';
  }

  function line(points) {
    var H = 96, W = 300;
    var kg = points.map(function (p) { return p.kg; });
    var lo = Math.min(...kg), hi = Math.max(...kg), span = (hi - lo) || 1;
    var step = points.length > 1 ? W / (points.length - 1) : 0;
    var path = points.map(function (p, i) {
      return (i ? 'L' : 'M') + (i * step) + ' ' + (H - ((p.kg - lo) / span) * (H - 10) - 5);
    }).join(' ');
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" role="img" ' +
      'aria-label="Линия веса"><path d="' + path + '" fill="none" stroke="var(--data)" ' +
      'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ' +
      'vector-effect="non-scaling-stroke"/></svg>' +
      '<div class="legend"><span>' + n(kg[0], 1) + ' → ' + n(kg[kg.length - 1], 1) + ' кг</span>' +
      '<span>мин ' + n(lo, 1) + ' · макс ' + n(hi, 1) + '</span></div>';
  }

  /* Виды верхней панели. Значения совпадают с ветками в hero() — разъедутся,
     и настройка начнёт молча не срабатывать. */
  var HEROES = [
    ['bars',  'Два «осталось»',  'Крупно то, что ещё можно. Бары держат контекст.'],
    ['rings', 'Кольца и список', 'Все четыре макроса разом, как в Apple Fitness.'],
    ['kcal',  'Только калории',  'Минимум. Белок уходит в подпись.']
  ];

  function renderSettings() {
    document.getElementById('title').textContent = 'Настройки';
    loading();
    api('/api/settings').then(function (d) {
      state.settings = d;
      var p = d.prefs, g = d.goal;
      show(
        group('Вид',
          HEROES.map(function (h) {
            return '<div class="pick' + (p.hero === h[0] ? ' on' : '') +
              '" data-hero="' + h[0] + '"><span class="dot"></span>' +
              '<span style="flex-grow:1"><span style="font-size:var(--f-body-s)">' +
              esc(h[1]) + '</span><div class="mic">' + esc(h[2]) + '</div></span></div>';
          }).join(''),
          'Меняется только вид. Записи, цели и подсчёт не трогаются.') +

        group('Подвал панели',
          [['meals', 'Приёмов за день'], ['weight', 'Последний вес']].map(function (o) {
            return '<div class="pick' + ((p.foot_third || 'meals') === o[0] ? ' on' : '') +
              '" data-foot="' + o[0] + '"><span class="dot"></span>' +
              '<span style="flex-grow:1;font-size:var(--f-body-s)">' + esc(o[1]) +
              '</span></div>';
          }).join(''),
          'Третья ячейка подвала. Жир и углеводы остаются на месте.') +

        group('Ввод',
          [[false, 'Начинать с текста'], [true, 'Сразу открывать микрофон']].map(function (o) {
            return '<div class="pick' + ((p.mic_first || false) === o[0] ? ' on' : '') +
              '" data-mic="' + o[0] + '"><span class="dot"></span>' +
              '<span style="flex-grow:1;font-size:var(--f-body-s)">' + esc(o[1]) +
              '</span></div>';
          }).join(''),
          'Микрофон сразу — быстрее, но клиент спросит доступ на первом же экране.') +

        group('Период в Итогах',
          [[7, 'Неделя'], [30, '30 дней'], [90, '3 месяца']].map(function (o) {
            return '<div class="pick' + ((p.period || 30) === o[0] ? ' on' : '') +
              '" data-defperiod="' + o[0] + '"><span class="dot"></span>' +
              '<span style="flex-grow:1;font-size:var(--f-body-s)">' + esc(o[1]) +
              '</span></div>';
          }).join(''),
          'Какой период открывается по умолчанию.') +

        group('Цели',
          field('gk', 'Калорий в день', g.kcal) +
          field('gp', 'Белок, г', g.protein_g) +
          field('gf', 'Жир, г', g.fat_g) +
          field('gc', 'Углеводы, г', g.carb_g) +
          '<button class="primary" id="saveGoal">Сохранить цели</button>',
          'Белок можно задать на килограмм веса — тогда цель поедет вслед за весом, ' +
          'а не устареет в тот момент, когда ты худеешь.') +

        group('Белок на килограмм',
          field('gpk', 'г на кг веса', 2) +
          '<button class="primary" id="saveKg">Пересчитать белок</button>',
          'Нужен хотя бы один замер веса — иначе умножать не на что.') +

        group('День',
          '<div class="field"><label for="tz">Часовой пояс</label>' +
          '<input id="tz" style="width:150px;text-align:left" value="' + esc(d.timezone) + '"></div>' +
          '<button class="primary" id="saveTz">Сохранить</button>',
          'Часовой пояс режет сутки: сменив его, ты двигаешь границы всех дней задним числом.') +

        group('Ассистент',
          row('Модель', d.coach.model) +
          row('Потрачено за месяц', '$' + n(d.coach.spent_usd, 2) + ' из $' + n(d.coach.limit_usd, 2)),
          d.coach.available
            ? 'Лимит жёсткий: при исчерпании ассистент отвечает отказом, а не молча хуже.'
            : 'Ключ не настроен — вкладка AI недоступна.') +

        '<div class="pager"><button id="toDay">Назад</button></div>');
    }).catch(failure);
  }

  function group(title, body, note) {
    return '<div style="margin-top:var(--s-4)">' +
      '<div class="mic" style="margin:0 var(--s-2) var(--s-2);text-transform:uppercase;' +
      'letter-spacing:.4px;color:var(--text-faint)">' + esc(title) + '</div>' +
      '<section class="card">' + body + '</section>' +
      (note ? '<div class="mic" style="margin-top:var(--s-2);padding:0 var(--s-2);' +
              'line-height:1.5;color:var(--text-faint)">' + esc(note) + '</div>' : '') +
      '</div>';
  }

  function row(label, value) {
    return '<div class="field"><label>' + esc(label) + '</label>' +
      '<span class="num" style="font-size:var(--f-body-s);color:var(--text-muted)">' +
      esc(value) + '</span></div>';
  }

  function patch(path, body, ok) {
    return send(path, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (d) {
      // Настройка обязана подействовать сразу, а не после перезахода:
      // иначе переключатель выглядит сломанным (MA-INV-31).
      if (d && d.prefs) state.prefs = d.prefs;
      toast(ok);
      renderSettings();
    }).catch(function (e) { toast(e.message, true); });
  }

  /* ── AI: вопросы по своим данным (§8.7) ──────────────────────────────── */

  var SUGGESTS = [
    'Почему вес растёт, если по дневнику дефицит?',
    'Чем добрать белок к вечеру?',
    'Что изменилось за последний месяц?'
  ];

  function renderAi() {
    document.getElementById('title').textContent = 'AI';
    var chat = (state.chat || []).map(function (m) {
      return '<div class="bubble ' + (m.me ? 'me' : 'ai') + '">' + esc(m.text) + '</div>';
    }).join('');
    var sug = state.chat && state.chat.length ? '' :
      '<div class="chips">' + SUGGESTS.map(function (q) {
        return '<button class="chip" data-ask="' + esc(q) + '">' + esc(q) + '</button>';
      }).join('') + '</div>';
    var b = state.budget;
    var budget = !b ? '' :
      '<section class="card" style="margin-top:var(--s-3)">' +
        '<div class="mic">Бюджет ассистента на месяц</div>' +
        '<div class="budget"><i style="width:' + pct(b.spent_usd, b.limit_usd) + '%"></i></div>' +
        '<div class="mic" style="margin-top:6px">$' + n(b.spent_usd, 2) + ' из $' +
          n(b.limit_usd, 2) + (b.available ? '' : ' · не настроен ключ') + '</div></section>';
    show('<section class="card">' +
           '<div class="cap">Спроси про свои данные</div>' +
           '<div class="mic" style="margin-top:6px;line-height:1.5">' +
             'Ассистент видит только твои цифры за 30 дней. Он не врач и не ставит диагнозов.' +
           '</div>' + sug + '</section>' +
         chat +
         '<div class="composer"><input id="ask" type="text" placeholder="Вопрос">' +
           '<button id="send" aria-label="Отправить">' +
           '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
           'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
           '<path d="M5 12h13M13 6l6 6-6 6"/></svg></button></div>' +
         budget);
    if (!state.budget) {
      api('/api/coach/budget').then(function (d) { state.budget = d; renderAi(); })
        .catch(function () {});
    }
  }

  function askCoach(text) {
    if (!text || state.asking) return;
    state.asking = true;
    state.chat = (state.chat || []).concat([{ me: true, text: text }]);
    renderAi();
    send('/api/coach', {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: text })
    }).then(function (d) {
      state.chat.push({ me: false, text: d.text });
      state.budget = { spent_usd: d.spent_usd, limit_usd: d.limit_usd, available: true };
      renderAi();
    }).catch(function (e) {
      state.chat.push({ me: false, text: e.message });
      renderAi();
    }).then(function () { state.asking = false; });
  }

  function render() {
    document.getElementById('nav-day').classList.toggle('hidden', state.tab !== 'day');
    document.getElementById('gear').setAttribute('aria-pressed', state.tab === 'set');
    if (state.tab === 'set') return renderSettings();
    if (state.tab === 'day') return renderDay();
    if (state.tab === 'cat') return renderCatalog();
    if (state.tab === 'tot') return renderTotals();
    return renderAi();
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
      /* Есть product_id — пишем мгновенно, без модели. Нет (продукт не
         запиннен) — падаем на обычный путь через текст. */
      var attr = i.product_id
        ? 'data-quick="' + i.product_id + '" data-grams="' + i.grams + '"'
        : 'data-say="' + esc(i.name + ' ' + n(i.grams) + ' г') + '"';
      return '<button class="chip" ' + attr + '>' +
        esc(i.name) + ' <span class="g">· ' + n(i.grams) + ' г</span></button>';
    }).join('') + '</div>';
  }

  function loadWeight() {
    api('/api/stats/weight?limit=1').then(function (d) {
      state.weight = d.points.length ? d.points[d.points.length - 1].kg : null;
      if (state.prefs && state.prefs.foot_third === 'weight') renderDay();
    }).catch(function () {});
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

  /* Ключ идемпотентности: один на попытку записи, а не на запрос.
     Ретрай браузера, двойной тап и повтор после таймаута приходят с тем же
     ключом, и сервер вернёт первый ответ вместо второй записи (§7.5). */
  function newKey() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return String(Date.now()) + '-' + Math.random().toString(36).slice(2);
  }

  function submitText() {
    var inp = document.getElementById('say');
    var text = (inp && inp.value || '').trim();
    if (!text || state.sending) return;
    state.sending = true;
    state.text = '';
    inp.value = '';
    inp.blur();
    loading();
    send('/api/meals', {
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': newKey() },
      body: JSON.stringify({ text: text })
    }).then(handle).catch(function (e) { toast(e.message, true); renderDay(); })
      .then(function () { state.sending = false; });
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
          headers: { 'Content-Type': blob.type, 'Idempotency-Key': newKey() },
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
      var did = b.dataset.drop;
      return ask('Удалить этот приём?', function () {
        loading();
        send('/api/meals/' + did, { method: 'DELETE' })
          .then(function () { delete state.open[did]; toast('Удалил.'); renderDay(); })
          .catch(function (err) { toast(err.message, true); renderDay(); });
      });
    }
    if (b.dataset.quick) {
      loading();
      return send('/api/meals/quick', {
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': newKey() },
        body: JSON.stringify({ product_id: Number(b.dataset.quick),
                               grams: Number(b.dataset.grams) })
      }).then(handle).catch(function (err) { toast(err.message, true); renderDay(); });
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
    if (b.id === 'toDay') { state.tab = 'day'; return render(); }
    if (b.id === 'saveGoal') return patch('/api/settings/goal', {
      kcal: Number(document.getElementById('gk').value),
      protein_g: Number(document.getElementById('gp').value),
      fat_g: Number(document.getElementById('gf').value),
      carb_g: Number(document.getElementById('gc').value)
    }, 'Цели сохранены.');
    if (b.id === 'saveKg') return patch('/api/settings/goal',
      { protein_per_kg: Number(document.getElementById('gpk').value) }, 'Белок пересчитан.');
    if (b.id === 'saveTz') return patch('/api/settings/timezone',
      { timezone: document.getElementById('tz').value.trim() }, 'Часовой пояс сохранён.');
    if (b.dataset.period) { state.period = Number(b.dataset.period); return renderTotals(); }
    if (b.dataset.ask) return askCoach(b.dataset.ask);
    if (b.id === 'send') {
      var el = document.getElementById('ask');
      var t = (el.value || '').trim(); el.value = '';
      return askCoach(t);
    }
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
    if (pick.dataset.hero) {
      return patch('/api/settings/prefs', { hero: pick.dataset.hero }, 'Вид панели изменён.');
    }
    if (pick.dataset.foot) {
      return patch('/api/settings/prefs', { foot_third: pick.dataset.foot }, 'Подвал изменён.');
    }
    if (pick.dataset.mic) {
      return patch('/api/settings/prefs',
        { mic_first: pick.dataset.mic === 'true' }, 'Ввод изменён.');
    }
    if (pick.dataset.defperiod) {
      state.period = null;
      return patch('/api/settings/prefs',
        { period: Number(pick.dataset.defperiod) }, 'Период изменён.');
    }
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
    /* Необратимо: проигравшие строки каталога исчезают. Показываем, сколько
       позиций истории переедет — соглашаться вслепую на необратимое нельзя. */
    var affects = g.affects || 0;
    ask('Слить ' + losers.length + ' запис' + (losers.length === 1 ? 'ь' : 'и') +
        ' в выбранную?' + (affects ? ' Затронет ' + affects + ' позиц' +
        (affects === 1 ? 'ию' : 'ий') + ' истории.' : '') + ' Отменить будет нельзя.',
      function () {
        loading();
        send('/api/products/merge', {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ winner_id: chosen, loser_ids: losers })
        }).then(function (r) {
          toast('Слил. Перепривязано записей: ' + r.items_repointed + '.');
          state.cat.dup = null;
          renderCatalog();
        }).catch(function (e) { toast(e.message, true); renderCatalog(); });
      });
  }

  var searchTimer = null;

  root.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && e.target.id === 'say') { e.preventDefault(); submitText(); }
    if (e.key === 'Enter' && e.target.id === 'ask') {
      e.preventDefault();
      var t = (e.target.value || '').trim(); e.target.value = '';
      askCoach(t);
    }
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

  document.getElementById('gear').addEventListener('click', function () {
    state.tab = state.tab === 'set' ? 'day' : 'set';
    render();
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
  loadWeight();
  /* Настройки нужны до первой отрисовки: иначе панель мигнёт видом по умолчанию
     и только потом переключится на выбранный. */
  api('/api/settings').then(function (d) {
    state.prefs = d.prefs;
    if (d.prefs.mic_first && state.tab === 'day') state.micFirst = true;
    render();
  }).catch(function () { state.prefs = {}; });
  if (tg.setHeaderColor) { try { tg.setHeaderColor('#000000'); } catch (e) { /* старый клиент */ } }
  render();
})();
