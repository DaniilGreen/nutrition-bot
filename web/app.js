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

  /* Радио без роли и без tabindex недостижимо с клавиатуры и молчит для
     скринридера, а состояние несёт только кольцо контрастом 2.7. Это и
     настройки, и выбор победителя при НЕОБРАТИМОМ слиянии дублей. */
  function pk(on) {
    return ' role="radio" tabindex="0" aria-checked="' + (on ? 'true' : 'false') + '"';
  }

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
          /* Конверт §7.4 лежит на верхнем уровне: {"error": {...}}. Раньше
             читалось body.detail.error — форма, которой сервер не отдаёт с
             тех пор, как конверт свели к одному. Из-за этого КАЖДАЯ ошибка
             показывалась как «Сервер ответил N», а ветки по коду молчали. */
          var err = body.error || (body.detail && body.detail.error) || {};
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
        var err = body.error || (body.detail && body.detail.error) || {};
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

  function show(html) { root.classList.remove('working'); root.innerHTML = html; }

  /* §6.2: поле ввода активно сразу, ввод не ждёт загрузки. Раньше скелет
     затирал весь экран вместе с композером — и записать во время загрузки
     было нельзя. */
  /* Пока идёт расшифровка или запись, экран НЕ стирается: общий скелет
     выглядел как падение — панель, лента и чипы исчезали на несколько секунд
     без единого слова. Меняем только поле ввода, всё остальное на месте. */
  function busy(text) {
    /* Строго композер дня: класс .composer носят ещё поле ассистента и
       редактор граммовки, и затирание первого попавшегося ломало чужой ввод. */
    var c = document.getElementById('composer');
    /* Экран без композера (каталог, дубли) гасим прозрачностью, а не скелетом:
       разметка остаётся на месте, высота не прыгает, клики не проходят. */
    if (!c) { root.classList.add('working'); return; }
    c.className = 'composer';
    c.innerHTML =
      '<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>' +
      '<span style="flex-grow:1;color:var(--text-muted);font-size:var(--f-body-s)">' +
      esc(text) + '</span>';
  }

  function loadingDay() {
    show('<section class="card">' +
         '<div class="skel" style="height:14px;width:45%"></div>' +
         '<div class="skel" style="height:34px;width:65%;margin-top:10px"></div>' +
         '<div class="skel" style="height:6px;margin-top:14px"></div>' +
         '</section>' + composer() + chips());
  }

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
          ? 'Нет связи. Проверь интернет и попробуй ещё раз.'
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
    /* Вес по умолчанию (§6.2). Но если замеров нет вовсе, ячейка показывала бы
       прочерк — тогда полезнее число приёмов, чем пустое место. */
    var wantWeight = (state.prefs ? state.prefs.foot_third : 'weight') !== 'meals';
    var kg = d.weight_kg;
    var third = (wantWeight && kg != null)
      ? foot('Вес', kg, null, 'right', 1)
      : foot('Приёмов', d.meals.length, null, 'right');
    return '<div class="sep" style="margin:var(--s-4) 0 0"></div>' +
      '<div class="foot">' + foot('Жир', t.fat_g, g.fat_g, 'left') +
      foot('Углеводы', t.carb_g, g.carb_g, 'left') + third + '</div>';
  }

  /* §6.2: под белком — граммы на килограмм веса. Цель ставят именно в этих
     единицах, и они не устаревают при изменении массы: 170 г при 91 кг и при
     84 кг — это разные вещи, а 1,9 г/кг остаётся собой.
     Нет замеров веса — строку не показываем: выдумывать вес нельзя. */
  function proteinNote(d) {
    var t = d.totals, g = d.goal;
    var base = n(t.protein_g) + ' из ' + n(g.protein_g) + ' г';
    if (!d.weight_kg) return base;
    return base + ' · ' + n(t.protein_g / d.weight_kg, 2) + ' г/кг из ' +
           n(g.protein_g / d.weight_kg, 1);
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
              /* MA-INV-20: --warn значит «отстаёт от цели», а не «это белок».
                 Цель взята — ноль перестаёт быть тревожным. */
              '<span class="num big"' + (protLeft > 0 ? ' style="color:var(--warn)"' : '') +
              '>' + n(Math.max(0, protLeft)) + '</span>' +
              '<span class="num unit">г</span></div>' +
            '<div class="track"><i style="width:' + pct(t.protein_g, g.protein_g) + '%;background:var(--prot)"></i></div>' +
            '<div class="mic" style="margin-top:6px' +
              (protLeft > 0 ? ';color:var(--warn)' : '') + '">' +
              proteinNote(d) + '</div>' +
          '</div>' +
        '</div>' +
        heroFoot(d) +
      '</section>';
  }

  /* `digits` нужен весу: он единственный дробный в подвале. Раньше вызывающий
     форматировал сам и передавал строку, а foot форматировал её повторно —
     число превращалось в NaN и рисовался прочерк. */
  function foot(label, value, goal, align, digits) {
    return '<div style="text-align:' + align + '">' +
      '<div class="mic">' + esc(label) + '</div>' +
      '<div style="margin-top:3px"><span class="num" style="font-size:var(--f-body);font-weight:600">' +
        n(value, digits) + '</span>' +
      (goal ? '<span class="num den"> /' + n(goal) + '</span>' : '') + '</div></div>';
  }

  /* ── лента приёмов ───────────────────────────────────────────────────── */

  /* Метка происхождения цифры (INV-13, FR-22): «≈» — оценка модели,
     «📋» — человек назвал сам. Бот метит так же; расхождение аппки и бота
     на одних данных читалось бы как баг в подсчёте. */
  function itemMark(i) {
    if (i.is_estimate) return ' <span style="color:var(--text-faint)">≈</span>';
    if (i.is_stated) return ' <span style="color:var(--text-faint)">📋</span>';
    return '';
  }

  function feedOrder() {
    return (state.prefs && state.prefs.feed_order) || 'new_first';
  }

  function feed(d) {
    if (!d.meals.length) {
      return '<section class="card" style="margin-top:var(--s-3)">' +
             '<div class="state">За этот день записей нет.</div></section>';
    }
    /* Новое сверху по умолчанию: только что записанное проверяют сразу,
       а утренний кофе — почти никогда. Порядок переключается тут же и
       запоминается, чтобы не спорить с привычкой. */
    var meals = feedOrder() === 'new_first' ? d.meals.slice().reverse() : d.meals;
    var rows = meals.map(function (m) {
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
        /* MA-INV-22: после ухода карточки «Записал» правка обязана быть
           достижима отсюда — иначе «пишем сразу» превращается в
           «записал мусор навсегда». Стейтед-позиции без веса не правим:
           масштабировать не от чего. */
        var editing = state.editItem === i.id;
        var can = i.grams > 0;
        return '<div class="row"' + (can ? ' data-item="' + i.id + '" data-meal="' + m.id +
               '" style="cursor:pointer"' : '') + '>' +
          '<span style="flex-grow:1">' + esc(i.name) +
            itemMark(i) + '</span>' +
          (i.grams > 0 ? '<span class="num g">' + n(i.grams) + ' г</span>'
                       : '<span class="mic g">свои цифры</span>') +
          '<span class="num" style="width:52px;text-align:right">' + n(i.kcal) + '</span>' +
        '</div>' + (editing ? gramsEditor(m.id, i) : '');
      }).join('');
      var macros = '<div class="row" style="border-top:1px solid var(--separator);margin-top:4px;padding-top:8px">' +
        '<span style="flex-grow:1">Б ' + n(m.protein_g) + ' · Ж ' + n(m.fat_g) + ' · У ' + n(m.carb_g) + '</span></div>';
      var acts = '<div class="actions" style="margin-top:var(--s-2)">' +
        '<button data-repeat="' + m.id + '">Повторить</button>' +
        '<button data-drop="' + m.id + '" class="danger">Удалить</button></div>';
      return head + '<div class="items">' + items + macros + acts + '</div>';
    }).join('');
    var newFirst = feedOrder() === 'new_first';
    var head = '<div style="display:flex;align-items:center;margin-bottom:var(--s-2)">' +
      '<span class="cap" style="flex-grow:1">Съедено</span>' +
      '<button class="sortbtn" data-order="' + (newFirst ? 'old_first' : 'new_first') +
        '" aria-label="Порядок записей">' +
        (newFirst ? 'сначала новое' : 'сначала старое') + '</button></div>';
    return '<section class="card" style="margin-top:var(--s-3)">' + head + rows + '</section>';
  }

  function gramsEditor(mealId, item) {
    return '<div class="composer" style="margin:6px 0 10px">' +
      '<input id="newg" type="number" inputmode="numeric" min="1" max="5000" ' +
        'value="' + n(item.grams) + '" ' +
        'style="flex-grow:1;background:none;border:0;color:var(--text);font:inherit;' +
        'outline:none;min-width:0"> ' +
      '<span class="mic" style="margin-right:6px">г</span>' +
      '<button data-save-g="' + item.id + '" data-save-m="' + mealId + '" ' +
        'aria-label="Сохранить вес">' +
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
        'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M20 6L9 17l-5-5"/></svg></button>' +
      '</div>' +
      '<div class="actions" style="margin:0 0 10px">' +
        '<button data-del-i="' + item.id + '" data-del-m="' + mealId + '" class="danger">' +
        'Убрать позицию</button></div>';
  }

  function dropItem(mealId, itemId) {
    ask('Убрать эту позицию из приёма?', function () {
      state.editItem = null;
      busy('Убираю…');
      send('/api/meals/' + mealId + '/items/' + itemId, { method: 'DELETE' })
        .then(function (r) {
          toast(r.meal ? 'Убрал позицию.' : 'Позиций не осталось — приём удалён.');
          renderDay();
        })
        .catch(function (e) { toast(e.message, true); renderDay(); });
    });
  }

  function saveGrams(mealId, itemId) {
    var el = document.getElementById('newg');
    var v = (el && el.value || '').trim();
    if (!v) return;
    state.editItem = null;
    busy('Сохраняю…');
    send('/api/meals/' + mealId + '/items/' + itemId, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ grams: Number(v) })
    }).then(function (r) {
      toast('Было ' + n(r.old_grams) + ' г, стало ' + n(r.item.grams) + ' г.');
      renderDay();
    }).catch(function (e) { toast(e.message, true); renderDay(); });
  }

  /* ── экраны ──────────────────────────────────────────────────────────── */

  function title(d) {
    if (state.daysAgo === 0) return 'Сегодня';
    if (state.daysAgo === 1) return 'Вчера';
    var parts = String(d.day).split('-');
    var months = ['янв','фев','мар','апр','мая','июн','июл','авг','сен','окт','ноя','дек'];
    return Number(parts[2]) + ' ' + months[Number(parts[1]) - 1];
  }

  function dayHtml(d) {
    var extra = '';
    if (state.justLogged) extra = justCard(state.justLogged);
    else if (state.clarify) extra = clarifyCard(state.clarify);
    else if (state.label) extra = labelCard(state.label);
    var pace = d.pace && !paceMuted(d) ? paceCard(d.pace) : '';
    return hero(d) + composer() + chips() + pace + extra + feed(d);
  }

  function paintDay(d) {
    state.lastDay = d;
    state.shownDay = state.daysAgo;
    document.getElementById('title').textContent = title(d);
    document.getElementById('nav-day').classList.toggle('hidden', false);
    document.getElementById('fwd').disabled = state.daysAgo === 0;
    show(dayHtml(d));
    var inp = document.getElementById('say');
    if (inp && state.keepFocus) { inp.focus(); state.keepFocus = false; }
    armJustTimer();
  }

  /* Раскрыть приём, открыть правку веса, сменить порядок — это раскладка
     экрана, а не новые цифры. Рисуем из уже полученного дня: запрос ради
     оформления возвращал те же числа и стоил мигания на весь экран. */
  function repaintDay() {
    if (!state.lastDay || state.shownDay !== state.daysAgo) return renderDay();
    show(dayHtml(state.lastDay));
    armJustTimer();
  }

  /* Скелет — только когда показывать нечего: холодный старт или переход на
     другой день. Обновление того же дня идёт поверх живого экрана. Раньше
     любой чип гасил панель, ленту и чипы, высота схлопывалась с ленты до
     одной карточки — экран моргал и прыгал. */
  function renderDay() {
    var onScreen = !!document.getElementById('composer');
    var fresh = state.lastDay && state.shownDay === state.daysAgo;
    if (!onScreen || !fresh) {
      if (fresh) show(dayHtml(state.lastDay));
      else loadingDay();
    }
    api('/api/day?days_ago=' + state.daysAgo).then(paintDay).catch(failure);
  }

  /* MA-INV-22: карточка «Записал» живёт 5 секунд, потом уходит сама. Правка
     после этого достижима тапом по строке ленты — иначе «пишем сразу»
     превращается в «записал мусор навсегда».

     Таймер снимается при каждой перерисовке: без этого два подряд приёма
     оставляли два таймера, и второй гасил карточку раньше срока. */
  function armJustTimer() {
    if (!state.justLogged) {
      clearTimeout(state.justTimer);
      state.justArmed = null;
      return;
    }
    /* Пять секунд считаются от записи, а не от последней перерисовки: раньше
       любое касание экрана заводило таймер заново и карточка не уходила
       вовсе — ровно то, на что жаловались («через пять сек не пропадает»). */
    if (state.justArmed === state.justLogged.id) return;
    clearTimeout(state.justTimer);
    var shown = state.justLogged.id;
    state.justArmed = shown;
    state.justTimer = setTimeout(function () {
      state.justArmed = null;
      if (state.justLogged && state.justLogged.id === shown && state.tab === 'day') {
        state.justLogged = null;
        repaintDay();
      }
    }, 5000);
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
          /* MA-INV-24: две зоны. Тело строки пишет приём с привычной
             граммовкой — ради этого вкладка и сделана. Шеврон открывает
             карточку. Одна зона на всё превращала каталог в редактор. */
          var g = p.usual_grams || 100;
          return '<div class="item2">' +
            '<button class="body" data-quick="' + p.id + '" data-grams="' + g + '">' +
              '<span class="nm">' + esc(p.name) + est + '</span>' +
              '<span class="mic use">' +
                (p.uses ? n(p.uses) + ' раз · обычно ' + n(g) + ' г' : n(g) + ' г') +
              '</span></button>' +
            '<span class="num kk">' + n(p.kcal_per_100g) + '<span class="per">/100 г</span></span>' +
            '<button class="chev" data-product="' + p.id + '" aria-label="Открыть карточку">' +
              '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
              'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
              '<path d="M9 18l6-6-6-6"/></svg></button></div>';
        }).join('') || '<div class="state">Ничего не нашлось.</div>';
        var hint = d.items.length
          ? '<div class="mic" style="padding:var(--s-2) var(--s-2) 0">' +
            'Тап по строке — записать. Шеврон — открыть карточку.</div>'
          : '';
        var from = d.total ? c.offset + 1 : 0;
        var to = Math.min(c.offset + PAGE, d.total);
        show(
          '<div class="search">' +
            '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" ' +
              'stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/>' +
              '<path d="M20 20l-3.5-3.5" stroke-linecap="round"/></svg>' +
            '<input id="q" type="search" placeholder="Найти продукт" value="' + esc(c.q) + '">' +
          '</div>' +
          '<section class="card">' + hint + '<div class="plist">' + rows + '</div></section>' +
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
        recentCard(d.recent_items, p) +
        '<div class="pager"><button id="toList">К списку</button></div>');
    }).catch(failure);
  }

  /* §6.8: «где я это ел». Без истории непонятно, откуда взялась плотность и
     стоит ли её править — а карточка ровно про это. Здесь же кнопка записать
     привычной порцией: пришёл посмотреть, заодно записал. */
  function recentCard(items, p) {
    var g = p.usual_grams || 100;
    var log = '<div class="actions" style="margin-top:var(--s-3)">' +
      '<button data-quick="' + p.id + '" data-grams="' + g + '">' +
      'Записать ' + n(g) + ' г</button></div>';
    if (!items || !items.length) {
      return card('В приёмах',
        '<div class="state">Этот продукт ещё не встречался в записях.</div>' + log, '');
    }
    var rows = items.map(function (i) {
      return '<div class="row">' +
        '<span style="flex-grow:1">' + esc(i.at) + '</span>' +
        '<span class="num g">' + n(i.grams) + ' г</span>' +
        '<span class="num" style="width:52px;text-align:right">' + n(i.kcal) + '</span>' +
      '</div>';
    }).join('');
    return card('В приёмах', '<div class="items" style="padding-left:0">' + rows +
                '</div>' + log,
                'Последние ' + n(items.length) + ' появления. Столбец справа — ' +
                'ккал по снимку на момент записи, а не по нынешней плотности.');
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
      /* MA-INV-25: человек должен знать заранее, что история не поедет.
         Иначе он ждёт пересчёта съеденного и считает его отсутствие багом. */
      var vow = '<div class="mic" style="padding:0 var(--s-2) var(--s-3);line-height:1.55">' +
        'Слияние меняет только каталог. Съеденное не пересчитается — в приёмах ' +
        'лежит снапшот КБЖУ на момент записи.</div>';
      if (!d.groups.length) {
        show('<section class="card"><div class="state">Дублей не нашлось.</div></section>' +
             '<div class="pager"><button id="toList">К списку</button></div>');
        return;
      }
      state.cat.dup = state.cat.dup || {};
      var blocks = d.groups.map(function (g, gi) {
        var chosen = state.cat.dup[gi] == null ? g.suggested_winner : state.cat.dup[gi];
        var rows = g.items.map(function (p) {
          return '<div class="pick' + (p.id === chosen ? ' on' : '') + '"' + pk(p.id === chosen) +
            ' data-group="' + gi + '" data-pick="' + p.id + '">' +
            '<span class="dot"></span>' +
            '<span style="flex-grow:1;min-width:0"><span style="font-size:var(--f-body-s)">' +
              esc(p.name) + '</span>' +
              '<div class="mic">' + n(p.kcal_per_100g) + ' ккал · ' + esc(p.source) + '</div></span>' +
            '</div>';
        }).join('');
        var warn = g.safe ? '' :
          /* Предупреждение перед необратимым — это --alert, а не «недобор цели». */
          '<div class="mic" style="color:var(--alert);margin-top:var(--s-2);line-height:1.5">' +
          'Плотности разошлись на ' + n(g.spread * 100) + '%. Слияние заметно двинет ' +
          'будущие записи — проверь, точно ли это одна еда.</div>';
        return '<section class="card" style="margin-top:var(--s-3)">' +
          '<div class="cap" style="margin-bottom:var(--s-2)">Оставить одну запись</div>' +
          rows + warn +
          '<button class="primary" data-merge="' + gi + '">Слить в выбранную</button></section>';
      }).join('');
      show(vow + blocks + '<div class="pager"><button id="toList">К списку</button></div>');
      state.cat._groups = d.groups;
    }).catch(failure);
  }


  /* ── Итоги: графики инлайн-SVG, без библиотек (§5.7) ─────────────────── */

  var PERIODS = [[7, 'Неделя'], [30, '30 дней'], [90, '3 месяца']];

  function renderTotals() {
    document.getElementById('title').textContent = 'Итоги';
    loading();
    var days = activePeriod();
    var nothing = function () { return null; };
    Promise.all([
      api('/api/stats/series?days=' + days),
      api('/api/stats/weight?limit=60').catch(function () { return { points: [] }; }),
      api('/api/stats/reconcile?days=' + Math.max(days, 14)).catch(nothing),
      api('/api/stats/contributions?days=' + days).catch(nothing),
      api('/api/stats/contributions?by=protein&days=' + days).catch(nothing),
      api('/api/stats/accuracy?days=' + days).catch(nothing)
    ]).then(function (res) {
      var d = res[0], w = res[1], rec = res[2], con = res[3], pro = res[4], acc = res[5];
      /* Без единой записи графики рисуют пустые сетки и «среднее 0 при цели
         170» — шесть коробок, которые выглядят поломкой, а не отсутствием
         данных. Одна честная карточка понятнее. */
      if (!d.days.some(function (x) { return x.meals > 0; })) {
        show(periods() +
          card('За этот период записей нет',
               '<div class="state">Выбери период побольше или запиши приём — ' +
               'графики появятся сами.</div>', ''));
        return;
      }
      show(periods() +
           reconcileCard(rec) +
           card('Белок по дням', barChart(d, 'protein_g', d.goal.protein_g, 'var(--prot)'),
                'Среднее ' + n(avg(d, 'protein_g')) + ' г при цели ' + n(d.goal.protein_g) + '.') +
           card('Ккал по дням', barChart(d, 'kcal', d.goal.kcal, 'var(--bar)'),
                'Красным — величина перебора.') +
           card('Полнота записей', heat(d),
                'Светлое — записано много, тёмное — почти пусто. Средние выше ' +
                'считаются по дням, где есть хоть одна запись.') +
           weightCard(w) +
           contribCard(con) +
           contribCard(pro, {
             title: 'Откуда приходит белок',
             unit: 'г',
             color: 'var(--prot)',
             note: 'Белок — главный разрыв дневника. Здесь видно, на чём он ' +
                   'держится: если весь набор идёт с одного продукта, ' +
                   'пропустил его — просел весь день.'
           }) +
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
    /* Расхождение — не недобор цели: у него свой тон (D-MA-7). */
    var color = agree ? 'var(--text)' : 'var(--alert)';
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
        '<div class="field"><label>Сверх записанного</label>' +
        '<span class="num" style="font-size:var(--f-body-s);color:var(--alert)">' +
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
  function contribCard(c, opts) {
    if (!c || !c.items.length) return '';
    var o = opts || {};
    var unit = o.unit || 'ккал';
    var max = (c.items[0].amount != null ? c.items[0].amount : c.items[0].kcal) || 1;
    var rows = c.items.map(function (i) {
      /* AC-MA-36: два продукта из одной группы дублей в одном графике — самый
         удачный момент предложить слияние: человек уже смотрит на расход и
         видит, что одна еда разбита надвое. */
      var v = i.amount != null ? i.amount : i.kcal;
      var dup = i.duplicate_group != null
        ? '<button class="badge dup" data-dups="1">слить?</button>'
        : '';
      return '<div style="padding:7px 0">' +
        '<div style="display:flex;align-items:baseline;gap:var(--s-2)">' +
          '<span style="flex-grow:1;min-width:0;overflow:hidden;text-overflow:ellipsis;' +
          'white-space:nowrap;font-size:var(--f-body-s)">' + esc(i.name) + dup + '</span>' +
          /* Число в колонке фиксированной ширины и без переноса: длинное имя
             раньше сжимало его, и «1050 ккал» ломалось на две строки. */
          '<span class="num" style="font-size:var(--f-caption);white-space:nowrap;' +
            'flex-shrink:0">' + n(v) + ' ' + unit + '</span>' +
          '<span class="mic" style="width:38px;text-align:right;flex-shrink:0">' +
            n(i.times) + '×</span>' +
        '</div>' +
        '<div class="track" style="margin-top:5px"><i style="width:' +
          (v / max * 100) + '%;background:' + (o.color || 'var(--bar)') +
          '"></i></div></div>';
    }).join('');
    return card(o.title || 'Куда уходят калории', rows,
      o.note || 'Столько калорий пришло с каждым продуктом за период. Не доля, ' +
      'а сумма: долю легко улучшить, просто съев меньше.');
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

  /* §6.12: вес без правки бесполезен — опечатку «83,6» вместо «93,6» иначе
     не убрать, а именно про такой выброс график и рассказывает. */
  function weightCard(w) {
    var pts = w.points || [];
    var body = pts.length > 1
      ? line(pts)
      : '<div class="state">Замеров пока мало — тренд появится со второго.</div>';
    var note = pts.length > 1
      ? (pts.length < 5
          ? n(pts.length) + ' замера — данных мало, тренд будет гулять.'
          : n(pts.length) + ' замеров.')
      : 'Точнее всего — утром, до еды.';
    return card('Вес', body + weightForm(pts),
                note + ' Нажми на замер, чтобы удалить.');
  }

  function weightForm(pts) {
    var rows = pts.slice(-6).reverse().map(function (p) {
      return '<div class="row" data-wid="' + p.id + '" style="cursor:pointer">' +
        '<span style="flex-grow:1">' + esc(p.at || '') + '</span>' +
        '<span class="num">' + n(p.kg, 1) + ' кг</span>' +
        '<span class="mic" style="width:56px;text-align:right;color:var(--text-faint)">' +
        'удалить</span></div>';
    }).join('');
    return '<div class="composer" style="margin:var(--s-3) 0 0">' +
        '<input id="wkg" type="number" inputmode="decimal" step="0.1" min="20" max="400" ' +
          'placeholder="Вес сегодня, кг" ' +
          'style="flex-grow:1;background:none;border:0;color:var(--text);font:inherit;' +
          'outline:none;min-width:0">' +
        '<button id="wsave" aria-label="Записать вес">' +
          '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
          'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
          '<path d="M20 6L9 17l-5-5"/></svg></button></div>' +
      (rows ? '<div class="items" style="padding-left:0">' + rows + '</div>' : '');
  }

  function saveWeight() {
    var el = document.getElementById('wkg');
    var v = (el && el.value || '').trim();
    if (!v) return;
    var wid = 'weight:' + v;
    send('/api/weight', {
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': keyFor(wid) },
      body: JSON.stringify({ weight_kg: Number(v) })
    }).then(function (r) {
      dropKey(wid);
      var d = r.delta_kg;
      toast('Записал ' + n(r.weight_kg, 1) + ' кг' +
            (d ? ' · ' + (d > 0 ? '+' : '') + n(d, 1) + ' кг' : ''));
      renderTotals();
    }).catch(function (e) { toast(e.message, true); });
  }

  function dropWeight(id) {
    ask('Удалить этот замер?', function () {
      send('/api/weight/' + id, { method: 'DELETE' })
        .then(function () { toast('Удалил замер.'); renderTotals(); })
        .catch(function (e) { toast(e.message, true); });
    });
  }

  /* Один источник правды для периода: подсветка чипа и загрузка данных
     считались по разным выражениям, и при настройке «Неделя» график строился
     за 7 дней, а подсвечен был чип «30 дней». */
  function activePeriod() {
    return state.period || (state.prefs && state.prefs.period) || 30;
  }

  function periods() {
    return '<div class="periods">' + PERIODS.map(function (p) {
      return '<button data-period="' + p[0] + '" aria-pressed="' +
        (activePeriod() === p[0]) + '">' + p[1] + '</button>';
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
    var full = 0, part = 0;
    var cells = d.days.map(function (x) {
      if (x.kcal >= goal * 0.9) full++; else if (x.kcal >= goal * 0.6) part++;
      var c = x.kcal >= goal * 0.9 ? '#EDF1F5'
            : x.kcal >= goal * 0.6 ? '#7C8899'
            : 'var(--surface-3)';
      return '<div style="background:' + c + '" title="' + esc(x.date) + '"></div>';
    }).join('');
    /* Сетка из цветных квадратов невизуально не читается вовсе, а это
       флагманская метрика проекта. Одна подпись даёт её содержание. */
    var said = 'Полнота записей за ' + n(d.days.length) + ' дней: ' + n(full) +
      ' записано полно, ' + n(part) + ' частично, ' +
      n(d.days.length - full - part) + ' почти пусто.';
    return '<div class="heat" role="img" aria-label="' + esc(said) + '">' +
      cells + '</div>' +
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

  /* Превью строится теми же функциями, что и боевая панель, но на данных
     последнего показанного дня. Отдельная «нарисованная» копия неизбежно
     разъехалась бы с настоящей. */
  function heroPreview(kind) {
    var d = state.lastDay;
    if (!d) return '<div class="state">Загружаю…</div>';
    var was = state.prefs && state.prefs.hero;
    state.prefs = Object.assign({}, state.prefs || {}, { hero: kind });
    var html = hero(d);
    state.prefs.hero = was;
    return html;
  }

  function renderSettings() {
    document.getElementById('title').textContent = 'Настройки';
    loading();
    api('/api/settings').then(function (d) {
      state.settings = d;
      var p = d.prefs, g = d.goal;
      show(
        group('Вид',
          HEROES.map(function (h) {
            /* §6.6: превью, а не название. «Два „осталось"» словами не говорит
               ничего — выбирать надо глазами. Рисуем теми же функциями, что и
               настоящую панель, на живых цифрах дня: иначе превью разойдётся
               с реальностью при первой же правке. */
            return '<div class="pick pick-v' + (p.hero === h[0] ? ' on' : '') + '"' + pk(p.hero === h[0]) +
              ' data-hero="' + h[0] + '">' +
              '<div class="pick-h"><span class="dot"></span>' +
                '<span style="flex-grow:1;font-size:var(--f-body-s)">' + esc(h[1]) + '</span>' +
              '</div>' +
              '<div class="mic" style="margin:2px 0 8px 26px">' + esc(h[2]) + '</div>' +
              '<div class="prev">' + heroPreview(h[0]) + '</div></div>';
          }).join(''),
          'Меняется только вид. Записи, цели и подсчёт не трогаются.') +

        group('Подвал панели',
          [['meals', 'Приёмов за день'], ['weight', 'Последний вес']].map(function (o) {
            return '<div class="pick' + ((p.foot_third || 'weight') === o[0] ? ' on' : '') + '"' + pk((p.foot_third || 'weight') === o[0]) +
              ' data-foot="' + o[0] + '"><span class="dot"></span>' +
              '<span style="flex-grow:1;font-size:var(--f-body-s)">' + esc(o[1]) +
              '</span></div>';
          }).join(''),
          'Третья ячейка подвала. Жир и углеводы остаются на месте.') +

        group('Ввод',
          [[false, 'Начинать с текста'], [true, 'Сразу открывать микрофон']].map(function (o) {
            return '<div class="pick' + ((p.mic_first || false) === o[0] ? ' on' : '') + '"' + pk((p.mic_first || false) === o[0]) +
              ' data-mic="' + o[0] + '"><span class="dot"></span>' +
              '<span style="flex-grow:1;font-size:var(--f-body-s)">' + esc(o[1]) +
              '</span></div>';
          }).join(''),
          'Микрофон сразу — быстрее, но клиент спросит доступ на первом же экране.') +

        group('Период в Итогах',
          [[7, 'Неделя'], [30, '30 дней'], [90, '3 месяца']].map(function (o) {
            return '<div class="pick' + ((p.period || 30) === o[0] ? ' on' : '') + '"' + pk((p.period || 30) === o[0]) +
              ' data-defperiod="' + o[0] + '"><span class="dot"></span>' +
              '<span style="flex-grow:1;font-size:var(--f-body-s)">' + esc(o[1]) +
              '</span></div>';
          }).join(''),
          'Какой период открывается по умолчанию.') +

        group('Режим',
          [['cut', 'Похудение'], ['maintain', 'Поддержание'], ['bulk', 'Набор']]
            .map(function (o) {
              return '<div class="pick' + ((g.target || 'maintain') === o[0] ? ' on' : '') + '"' + pk((g.target || 'maintain') === o[0]) +
                ' data-target="' + o[0] + '"><span class="dot"></span>' +
                '<span style="flex-grow:1;font-size:var(--f-body-s)">' + esc(o[1]) +
                '</span></div>';
            }).join(''),
          'От режима зависит раскладка макросов при пересчёте цели.') +

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

        group('О приложении',
          '<div class="composer" style="margin:0">' +
            '<input id="bug" type="text" placeholder="Что пошло не так?" ' +
              'style="flex-grow:1;background:none;border:0;color:var(--text);font:inherit;' +
              'outline:none;min-width:0">' +
            '<button id="bugsend" aria-label="Отправить">' +
              '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
              'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
              '<path d="M5 12h14M13 6l6 6-6 6"/></svg></button></div>',
          'Вместе с текстом уйдут последние приёмы — без них «посчитало не так» ' +
          'не воспроизвести.') +

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
      /* Группа радио обязана быть radiogroup с именем: иначе скринридер
         объявляет варианты вне контекста — «выбрано» без ответа на «что». */
      '<section class="card"' +
        (body.indexOf('role="radio"') > -1
          ? ' role="radiogroup" aria-label="' + esc(title) + '"' : '') +
      '>' + body + '</section>' +
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
      /* §4.3: совет, который нельзя выполнить в один тап, остаётся советом.
         Под ответом — кнопки на продукты, которые человек уже ел: у них есть
         и плотность, и своя граммовка, значит запись без модели. */
      var acts = (m.actions || []).map(function (a) {
        return '<button class="chip" data-quick="' + a.product_id +
          '" data-grams="' + a.grams + '">Записать ' + esc(a.name) +
          ' <span class="g">· ' + n(a.grams) + ' г</span></button>';
      }).join('');
      var busy = m.busy
        ? '<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>' +
          '<span class="mic" style="margin-left:6px">' + esc(m.busy) + '</span>'
        : '';
      return '<div class="bubble ' + (m.me ? 'me' : 'ai') + '"' +
          (m.busy ? ' aria-busy="true"' : '') + '>' +
          esc(m.text) + busy + '</div>' +
        (acts ? '<div class="chips" style="margin:6px 0 var(--s-3)">' + acts + '</div>' : '');
    }).join('');
    var sug = state.chat && state.chat.length ? '' :
      '<div class="chips">' + SUGGESTS.map(function (q) {
        return '<button class="chip" data-ask="' + esc(q) + '">' + esc(q) + '</button>';
      }).join('') + '</div>';
    var b = state.budget;
    /* Бюджет — короткая метка в углу карточки, а не полоса во весь экран.
       Пока потрачено два цента, шкала занимала места больше, чем весь смысл;
       заметной она обязана стать только на исходе. */
    var used = b && b.limit_usd ? b.spent_usd / b.limit_usd : 0;
    var low = used >= 0.8;
    var budget = !b ? '' :
      '<span class="budgetTag' + (low ? ' low' : '') + '">' +
        (b.available ? '$' + n(b.spent_usd, 2) + ' из $' + n(b.limit_usd, 2)
                     : 'ключ не настроен') + '</span>';

    /* Поле ввода последним и прижато к низу: разговор идёт снизу вверх, как
       в любом чате, и рука не тянется через весь экран. */
    show('<section class="card">' +
           '<div class="cardhead"><span class="cap">Спроси про свои данные</span>' +
             budget + '</div>' +
           '<div class="mic" style="margin-top:6px;line-height:1.5">' +
             'Ассистент смотрит твои записи сам: приёмы, продукты, вес. Он не врач и не ставит диагнозов.' +
           '</div>' + sug + '</section>' +
         chat +
         '<div class="composer ask"><input id="ask" type="text" ' +
           'placeholder="Спроси про свои цифры">' +
           '<button id="send" aria-label="Отправить">' +
           '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
           'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
           '<path d="M5 12h13M13 6l6 6-6 6"/></svg></button></div>');
    if (!state.budget) {
      api('/api/coach/budget').then(function (d) { state.budget = d; renderAi(); })
        .catch(function () {});
    }
  }

  /* §6.21.1: индикатор — это сам поток. Пока ассистент ходит за данными,
     говорим словами, КУДА он полез: «жди» не сообщает ничего, а это сообщает. */
  var TOOL_SAYS = {
    day_totals: 'смотрю дни',
    meals_on: 'смотрю приёмы',
    top_products: 'смотрю продукты',
    weight_log: 'смотрю вес'
  };

  function askCoach(text) {
    if (!text || state.asking) return;
    state.asking = true;
    state.chat = (state.chat || []).concat([{ me: true, text: text }]);
    /* Пузырь ответа заводим сразу пустым: в него потечёт текст, и он же
       держит строку состояния. Иначе экран стоит пустым до первого куска. */
    var bubble = { me: false, text: '', busy: 'думаю', actions: [] };
    state.chat.push(bubble);
    renderAi();

    var done = function (err) {
      bubble.busy = null;
      if (err) bubble.text = bubble.text || err;
      state.asking = false;
      renderAi();
    };

    streamCoach(text, bubble, function () { renderAi(); })
      .then(function (ok) {
        if (ok) return done(null);
        /* Поток не сложился — повторяем обычным запросом. Ассистент, который
           на старом клиенте показывает пустоту, хуже медленного (§6.21.1). */
        bubble.text = '';
        bubble.busy = 'думаю';
        renderAi();
        return send('/api/coach', {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question: text })
        }).then(function (d) {
          bubble.text = d.text;
          bubble.actions = d.actions || [];
          state.budget = { spent_usd: d.spent_usd, limit_usd: d.limit_usd, available: true };
          done(null);
        });
      })
      .catch(function (e) { done(e.message); });
  }

  /* Чтение SSE. Возвращает true, если поток дошёл до конца; false — если этот
     клиент потоков не умеет и надо идти запасным путём. */
  function streamCoach(question, bubble, onTick) {
    if (!window.fetch || !window.TextDecoder) return Promise.resolve(false);
    return fetch('/api/coach/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Init-Data': tg.initData
      },
      body: JSON.stringify({ question: question })
    }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          var err = body.error || {};
          var e = new Error(err.message || 'Сервер ответил ' + r.status);
          e.code = err.code;
          throw e;
        });
      }
      if (!r.body || !r.body.getReader) return false;   // старый WebView
      var reader = r.body.getReader();
      var decoder = new TextDecoder();
      var buf = '';
      var finished = false;

      var pump = function () {
        return reader.read().then(function (res) {
          if (res.done) return finished;
          buf += decoder.decode(res.value, { stream: true });
          var parts = buf.split('\n\n');
          buf = parts.pop();
          parts.forEach(function (block) {
            var line = block.replace(/^data: /, '').trim();
            if (!line) return;
            var ev;
            try { ev = JSON.parse(line); } catch (e) { return; }
            if (ev.kind === 'delta') bubble.text += ev.text;
            else if (ev.kind === 'reset') bubble.text = '';
            else if (ev.kind === 'tool') bubble.busy = TOOL_SAYS[ev.text] || 'смотрю данные';
            else if (ev.kind === 'error') { bubble.text = ev.message; finished = true; }
            else if (ev.kind === 'end') {
              bubble.text = ev.text || bubble.text;
              bubble.actions = ev.actions || [];
              state.budget = {
                spent_usd: ev.spent_usd, limit_usd: ev.limit_usd, available: true
              };
              finished = true;
            }
            if (ev.kind === 'delta' || ev.kind === 'reset') bubble.busy = null;
            onTick();
          });
          return pump();
        });
      };
      return pump();
    });
  }

  function render() {
    /* Класс на body: только на вкладке AI ввод закреплён снизу, и только там
       нужен запас места под ним. */
    document.body.classList.toggle('ai', state.tab === 'ai');
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
    /* Пока текст набран, правая кнопка отправляет, а не пишет голос. Раньше
       единственная видимая кнопка при набранном вводе запускала запись —
       отправить можно было только клавишей Enter, которой на телефоне нет
       под рукой. */
    var hasText = !!(state.text || '').trim();
    /* «Сразу открывать микрофон»: включаем запись при первом показе «Дня»,
       один раз за сессию — иначе она перезапускалась бы на каждой перерисовке. */
    if (state.micFirst && !state.micAutoDone && !rec) {
      state.micAutoDone = true;
      setTimeout(function () { if (!state.recording) startRec(); }, 350);
    }
    return '<div class="composer" id="composer">' +
      '<button id="cam" class="ghost" aria-label="Сфотографировать этикетку">' +
        '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
        'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M4 7h3l1.6-2h6.8L17 7h3a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1z"/>' +
        '<circle cx="12" cy="13" r="3.4"/></svg></button>' +
      '<input id="say" type="text" inputmode="text" autocomplete="off" ' +
        'placeholder="' + (writeDay()
          ? esc('Что съел ' + (state.lastDay ? title(state.lastDay).toLowerCase() : 'в тот день') + '?')
          : 'Что съел? Или скажи голосом') +
        '" value="' + esc(state.text || '') + '">' +
      (hasText && !rec
        ? '<button id="sendtext" aria-label="Записать">' +
          '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
          'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
          '<path d="M5 12h14M13 6l6 6-6 6"/></svg></button>'
        : '') +
      '<button id="mic" class="' + (rec ? 'rec' : '') + (hasText ? ' hidden' : '') + '" ' +
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

  /* Багрепорт #33: пишем в тот день, который человек видит перед собой. Вне
     вкладки «День» смотреть не на что — там всегда сегодня. */
  function writeDay() {
    return state.tab === 'day' ? (state.daysAgo || 0) : 0;
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

  function loadFrequent() {
    /* Тихо: пустые чипы — не ошибка, ради них не стоит ломать экран. */
    api('/api/frequent?limit=10').then(function (d) {
      state.frequent = d.items;
      if (state.tab !== 'day') return;
      /* Вставляем строку чипов на место, а не перерисовкой всего дня: та
         ходила в API заново и мигала экраном ради оформления. */
      var old = document.querySelector('.chips');
      var html = chips();
      if (old) { old.outerHTML = html; return; }
      var c = document.querySelector('.composer');
      if (c && html) c.insertAdjacentHTML('afterend', html);
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

  /* §6.20 «Добор дня». Дневник теряет не дни целиком, а вторую половину дня:
     полный день идёт до 20:24, брошенный обрывается в 15:20. Карточка сравнивает
     сегодняшний темп с ЛИЧНОЙ нормой к этому же часу.

     MA-INV-38: говорим цифрами человека, а не оценкой человека. Ни «мало», ни
     «забыл», ни «недозаписал» — только «обычно столько, сейчас столько». */

  function hh(h) { return (h < 10 ? '0' : '') + h + ':00'; }

  /* Ключ отказа — на дату: «ничего не ел» верно ровно для одного дня.
     ponytail: localStorage, то есть на устройство. Потолок известен и записан
     в спеке — серверная отметка это уже «День закрыт», отдельная функция. */
  function paceOffKey(d) { return 'pace-off:' + d.day; }

  function paceMuted(d) {
    try { return localStorage.getItem(paceOffKey(d)) === '1'; }
    catch (e) { return false; }   /* приватный режим — просто показываем */
  }

  function mutePace(d) {
    try { localStorage.setItem(paceOffKey(d), '1'); } catch (e) { /* переживём */ }
  }

  /* Две накопленные кривые: серая — обычный день, жёлтая — сегодняшний.
     Инлайн-SVG, как остальные графики аппки: сборщика в проекте нет (§5.7). */
  function paceSvg(p) {
    var all = (p.norm_points || []).concat(p.today_points || []);
    if (all.length < 2) return '';
    var W = 240, H = 58, pad = 8;
    var lo = all[0][0], hi = all[0][0], max = 1;
    all.forEach(function (q) {
      if (q[0] < lo) lo = q[0];
      if (q[0] > hi) hi = q[0];
      if (q[1] > max) max = q[1];
    });
    var x = function (h) { return hi === lo ? W / 2 : pad + (h - lo) / (hi - lo) * (W - pad * 2); };
    var y = function (v) { return H - 6 - (v / max) * (H - 18); };
    var line = function (pts) {
      return pts.map(function (q) { return n(x(q[0]), 1) + ',' + n(y(q[1]), 1); }).join(' ');
    };
    var last = (p.today_points || [])[(p.today_points || []).length - 1];
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" ' +
      'aria-label="Обычный набор калорий по часам против сегодняшнего" ' +
      'style="display:block;width:100%;height:auto;margin-top:8px">' +
      '<line x1="' + pad + '" y1="' + (H - 5) + '" x2="' + (W - pad) + '" y2="' + (H - 5) +
        '" stroke="var(--separator)" stroke-width="1"/>' +
      '<polyline points="' + line(p.norm_points) + '" fill="none" ' +
        'stroke="var(--bar)" stroke-width="1.6" stroke-linecap="round"/>' +
      '<polyline points="' + line(p.today_points) + '" fill="none" ' +
        'stroke="var(--warn)" stroke-width="2" stroke-linecap="round"/>' +
      (last ? '<circle cx="' + n(x(last[0]), 1) + '" cy="' + n(y(last[1]), 1) +
        '" r="2.8" fill="var(--warn)"/>' : '') +
      '</svg>';
  }

  function paceCard(p) {
    var chips = (p.chips || []).map(function (i) {
      return '<button class="chip" data-quick="' + i.product_id +
        '" data-grams="' + i.grams + '">' + esc(i.name) +
        ' <span class="g">· ' + n(i.grams) + ' г</span></button>';
    }).join('');
    var when = p.last_at
      ? 'Последняя запись в ' + esc(p.last_at) + '. Что было после?'
      : 'Сегодня записей ещё нет. Что уже было?';
    return '<section class="card pace" style="margin-top:var(--s-3)">' +
      '<div class="mic">К ' + hh(p.hour) + ' у тебя обычно ' + n(p.norm_kcal) + '</div>' +
      '<div class="num" style="font-size:var(--f-t1);font-weight:700;color:var(--warn);' +
        'line-height:1.1">' + n(p.today_kcal) + '</div>' +
      paceSvg(p) +
      '<div class="mic" style="margin-top:6px;line-height:1.5">' + when + '</div>' +
      '<div class="chips">' + chips +
        '<button class="chip off" data-pace-off="1">Ничего не ел</button></div>' +
      '</section>';
  }

  /* FR-27: прочитали упаковку — показываем ЧТО прочитали и ждём вес. Цифры
     с этикетки точные, но только на 100 г: сколько съедено — знает человек.
     Показать плотность до ответа обязательно, иначе неверно прочитанная
     таблица запинится навсегда (INV-2) и молча испортит все будущие записи. */
  function labelCard(c) {
    var p = c.per100;
    var opts = [];
    if (c.serving_g) opts.push(['1 порция', n(c.serving_g) + ' г']);
    if (c.package_g) opts.push(['вся пачка', n(c.package_g) + ' г']);
    var buttons = opts.map(function (o) {
      return '<button data-answer="' + esc(o[0]) + '">' + esc(o[0]) +
        ' <span class="g">· ' + o[1] + '</span></button>';
    }).join('');
    return '<section class="card just" style="margin-top:var(--s-3)">' +
      '<div class="ttl">С этикетки: ' + esc(c.item_name) + '</div>' +
      '<div class="mic" style="margin-top:4px">на 100 г — ' + n(p.kcal) + ' ккал · Б ' +
        n(p.protein_g) + ' · Ж ' + n(p.fat_g) + ' · У ' + n(p.carb_g) + '</div>' +
      (c.atwater_warning
        ? '<div class="mic" style="color:var(--alert);margin-top:6px;line-height:1.5">' +
          'Белки, жиры и углеводы не сходятся с калориями — проверь цифры на упаковке, ' +
          'возможно снимок прочитан неточно.</div>'
        : '') +
      (c.hint
        ? '<div class="mic" style="color:var(--alert);margin-top:6px">' +
          'Не разобрал вес. Напиши число или нажми кнопку.</div>'
        : '') +
      '<div class="ttl" style="margin-top:var(--s-3);font-size:var(--f-body-s)">' +
        'Сколько съел?</div>' +
      (buttons ? '<div class="opts">' + buttons + '</div>' : '') +
      '<div class="composer" style="margin-top:var(--s-3)">' +
        '<input id="grams" type="number" inputmode="numeric" min="1" max="5000" ' +
          'placeholder="граммами, напр. 250" ' +
          'style="flex-grow:1;background:none;border:0;color:var(--text);' +
          'font:inherit;outline:none;min-width:0">' +
        '<button data-grams="1" aria-label="Записать">' +
          '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" ' +
          'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
          'stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/>' +
          '</svg></button>' +
      '</div>' +
      '<div class="opts" style="margin-top:var(--s-2)">' +
        '<button data-answer="" data-cancel="1" ' +
        'style="border-color:var(--surface-3);background:var(--surface-2)">Отмена</button>' +
      '</div></section>';
  }

  function clarifyCard(c) {
    var cancel = '<button data-answer="" data-cancel="1" ' +
      'style="border-color:var(--surface-3);background:var(--surface-2)">Отмена</button>';

    /* Сервер спрашивает две разные вещи, и путать их нельзя: "variant" —
       выбор из предложенных имён, "portion" — сколько это было. Раньше
       карточка рисовала только кнопки из options; при вопросе о порции
       options пуст, оставалась одна «Отмена», и приём терялся. */
    if (c.ask === 'portion') {
      return '<section class="card just" style="margin-top:var(--s-3)">' +
        '<div class="ttl">Сколько было: ' + esc(c.item_name) + '?</div>' +
        '<div class="opts">' +
          ['Маленькая', 'Средняя', 'Большая'].map(function (o) {
            return '<button data-answer="' + esc(o) + '">' + esc(o) + '</button>';
          }).join('') +
        '</div>' +
        '<div class="composer" style="margin-top:var(--s-3)">' +
          '<input id="grams" type="number" inputmode="numeric" min="1" max="5000" ' +
            'placeholder="или граммами, напр. 250" ' +
            'style="flex-grow:1;background:none;border:0;color:var(--text);' +
            'font:inherit;outline:none;min-width:0">' +
          '<button data-grams="1" aria-label="Записать">' +
            '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" ' +
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
            'stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/>' +
            '</svg></button>' +
        '</div>' +
        '<div class="opts" style="margin-top:var(--s-2)">' + cancel + '</div></section>';
    }

    var opts = (c.options || []).map(function (o) {
      return '<button data-answer="' + esc(o) + '">' + esc(o) + '</button>';
    }).join('');
    return '<section class="card just" style="margin-top:var(--s-3)">' +
      '<div class="ttl">Уточни: ' + esc(c.item_name) + '</div>' +
      '<div class="opts">' + opts + cancel + '</div></section>';
  }

  function sendAnswer(answer) {
    /* Один путь ответа на два вопроса: «уточни, что это» и «сколько съел с
       этикетки». Различает их сервер по виду черновика — клиент про плотности
       не знает и знать не должен. */
    var c = state.clarify || state.label;
    if (!c) return;
    busy('Записываю…');
    return send('/api/meals/clarify', {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ draft_id: c.draft_id, answer: answer })
    }).then(handle).catch(function (err) { toast(err.message, true); renderDay(); });
  }

  function handle(res) {
    state.clarify = null;
    state.label = null;
    state.justLogged = null;
    if (res.kind === 'logged') {
      state.justLogged = res.meal;
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
    } else if (res.kind === 'clarify') {
      state.clarify = res;
    } else if (res.kind === 'label') {
      state.label = res;
    } else if (res.kind === 'weight') {
      var d = res.delta_kg;
      toast('Вес ' + n(res.weight_kg, 1) + ' кг' +
            (d ? ' · ' + (d > 0 ? '+' : '') + n(d, 1) + ' кг' : ''));
    } else if (res.kind === 'item_removed') {
      /* Коррекция сработала и база изменилась — красный «Не понял» здесь
         заставлял повторять команду поверх уже выполненной. */
      toast('Убрал: ' + (res.names || []).join(', ') + '.');
    } else if (res.kind === 'item_adjusted') {
      toast(res.name + ': было ' + n(res.old_grams) + ' г, стало ' +
            n(res.grams) + ' г.');
    } else if (res.kind === 'correction_failed') {
      toast(res.message, true);
    } else if (res.kind === 'stats') {
      toast('Похоже на вопрос, а не на еду. Записывать не стал — цифры сверху свежие.');
    } else {
      toast(res.message || 'Не понял, что записать.', true);
    }
    /* Раньше здесь стоял безусловный возврат на сегодня — и запись во вчера
       мгновенно уводила с того дня, куда только что писали. */
    renderDay();
  }

  /* Ключ идемпотентности: один на попытку записи, а не на запрос.
     Ретрай браузера, двойной тап и повтор после таймаута приходят с тем же
     ключом, и сервер вернёт первый ответ вместо второй записи (§7.5). */
  function newKey() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return String(Date.now()) + '-' + Math.random().toString(36).slice(2);
  }

  /* Ключ выдаётся на ДЕЙСТВИЕ, а не на клик (§7.5). Ключ, созданный при
     отправке, у двух кликов разный — и защита не срабатывает: так двойной тап
     по чипу писал два приёма и удваивал калории. Держим по ключу на дело
     («этот продукт этой граммовкой», «повтор этого приёма»); после успешной
     записи ключ снимаем, иначе осознанная вторая порция не запишется. */
  function keyFor(id) {
    if (!state.keys) state.keys = {};
    if (!state.keys[id]) state.keys[id] = newKey();
    return state.keys[id];
  }

  function dropKey(id) {
    if (state.keys) delete state.keys[id];
  }

  /* 409 in_flight значит «первый запрос ещё идёт» — это не сбой, и говорить о
     нём человеку нечего: кнопка и так в состоянии «пишу». */
  function oops(e) {
    if (e && e.code === 'in_flight') return;
    toast(e.message, true);
  }

  function submitText() {
    var inp = document.getElementById('say');
    var text = (inp && inp.value || '').trim();
    if (!text || state.sending) return;
    state.sending = true;
    /* Ключ фиксируем на попытку, а не на запрос: если сеть отвалилась и человек
       нажал ещё раз, сервер вернёт первый ответ вместо второй записи. */
    var key = keyFor('text');
    inp.value = '';
    inp.blur();
    busy('Записываю…');
    send('/api/meals', {
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
      body: JSON.stringify({ text: text, days_ago: writeDay() })
    }).then(function (r) {
      state.text = '';
      dropKey('text');
      return handle(r);
    }).catch(function (e) {
      /* Текст НЕ теряем: пропавшая фраза — это непоставленная запись, а
         полнота записей и есть главная дыра проекта (§4.0). */
      state.text = text;
      toast(e.message + ' Текст сохранён.', true);
      renderDay();
    }).then(function () { state.sending = false; });
  }

  /* ── этикетка ────────────────────────────────────────────────────────── */

  /* Снимок ужимаем до 1280 px и в JPEG перед отправкой. Современный телефон
     отдаёт 4–8 МБ; таблица КБЖУ читается и с ужатого, а мегабайты на мобильном
     интернете — это секунды ожидания и оборванные загрузки. Не получилось
     ужать (нестандартный формат, нет canvas) — шлём как есть: лучше дорого,
     чем никак. */
  function shrink(file, cb) {
    if (!window.URL || !window.URL.createObjectURL) return cb(file);
    var url = URL.createObjectURL(file);
    var img = new Image();
    img.onload = function () {
      var k = Math.min(1, 1280 / Math.max(img.width, img.height));
      var c = document.createElement('canvas');
      c.width = Math.round(img.width * k);
      c.height = Math.round(img.height * k);
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      if (!c.toBlob) return cb(file);
      c.toBlob(function (b) { cb(b && b.size ? b : file); }, 'image/jpeg', 0.82);
    };
    img.onerror = function () { URL.revokeObjectURL(url); cb(file); };
    img.src = url;
  }

  function sendLabel(file) {
    if (state.sending) return;
    state.sending = true;
    busy('Читаю этикетку…');
    shrink(file, function (blob) {
      send('/api/meals/label?days_ago=' + writeDay(), {
        headers: {
          'Content-Type': blob.type || 'image/jpeg',
          'Idempotency-Key': keyFor('label')
        },
        body: blob
      }).then(function (r) { dropKey('label'); return handle(r); })
        .catch(function (e) {
          oops(e);
          renderDay();
        }).then(function () { state.sending = false; });
    });
  }

  /* ── голос ───────────────────────────────────────────────────────────── */

  var recorder = null, chunks = [];

  /* Кнопку микрофона переключаем на месте, а не перерисовкой всего дня:
     renderDay ходит за данными, и на время ответа экран мигал. */
  function setMic(on) {
    var b = document.getElementById('mic');
    if (!b) return;
    b.className = on ? 'rec' : '';
    b.setAttribute('aria-label', on ? 'Остановить запись' : 'Записать голосом');
    b.innerHTML = on
      ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" ' +
        'aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>'
      : '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" aria-hidden="true">' +
        '<rect x="9" y="3" width="6" height="11" rx="3"/>' +
        '<path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>';
  }

  function startRec() {
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      toast('Здесь голос не записывается — набери текстом.', true);
      return;
    }
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      chunks = [];
      /* Формат выбираем явно и из поддерживаемых: без указания браузер даёт
         что хочет, а Safari и Telegram на iOS пишут mp4. Опус в webm первым —
         он самый компактный, а объём это время загрузки на мобильной сети. */
      var want = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus',
                  'audio/ogg', 'audio/mp4'];
      var pick = null;
      if (window.MediaRecorder && MediaRecorder.isTypeSupported) {
        for (var i = 0; i < want.length && !pick; i++) {
          if (MediaRecorder.isTypeSupported(want[i])) pick = want[i];
        }
      }
      recorder = pick ? new MediaRecorder(stream, { mimeType: pick })
                      : new MediaRecorder(stream);
      recorder.ondataavailable = function (e) { if (e.data.size) chunks.push(e.data); };
      recorder.onstop = function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        state.recording = false;
        var blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
        if (!blob.size) { setMic(false); return; }
        /* Никакого renderDay: он уходит за данными и своим ответом затирает
           индикатор через пару сотен миллисекунд. Именно это и выглядело
           дёрганьем — экран дважды перерисовывался за одну запись. */
        busy('Расшифровываю…');
        /* Ключ на запись, а не на отправку: ретрай после оборванной сети
           должен прийти с тем же — иначе одна и та же фраза ляжет дважды. */
        send('/api/meals/voice?days_ago=' + writeDay(), {
          headers: { 'Content-Type': blob.type, 'Idempotency-Key': keyFor('voice') },
          body: blob
        }).then(function (r) { dropKey('voice'); return handle(r); })
          .catch(function (e) { oops(e); renderDay(); });
      };
      recorder.start();
      state.recording = true;
      setMic(true);
    }).catch(function () {
      toast('Микрофон недоступен — можно набрать текстом.', true);
    });
  }

  function stopRec() { if (recorder && recorder.state !== 'inactive') recorder.stop(); }

  /* ── события ─────────────────────────────────────────────────────────── */

  nav.addEventListener('click', function (e) {
    var b = e.target.closest('button[data-tab]');
    if (!b) return;
    state.tab = b.dataset.tab;
    state.daysAgo = 0;
    clearTimeout(state.justTimer);
    state.justLogged = null;
    [].forEach.call(nav.querySelectorAll('button'), function (x) {
      if (x.dataset.tab === state.tab) x.setAttribute('aria-current', 'page');
      else x.removeAttribute('aria-current');
    });
    if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
    render();
  });

  root.addEventListener('click', function (e) {
    /* Тап по позиции раскрывает правку веса. Ловим до поиска кнопки: строка
       позиции — не кнопка, иначе она была бы вложенной в кнопку приёма. */
    var wrow = e.target.closest('.row[data-wid]');
    if (wrow) return dropWeight(Number(wrow.dataset.wid));

    var row = e.target.closest('.row[data-item]');
    if (row && !e.target.closest('.composer')) {
      var id = Number(row.dataset.item);
      state.editItem = state.editItem === id ? null : id;
      return repaintDay();
    }

    var b = e.target.closest('button');
    if (!b) return;

    if (b.dataset.meal) {
      state.open[b.dataset.meal] = !state.open[b.dataset.meal];
      return repaintDay();
    }
    if (b.dataset.paceOff) {
      if (state.lastDay) mutePace(state.lastDay);
      /* Из данных карточку не выбрасываем: сервер отдаст её снова при следующем
         запросе дня, а решение «сегодня не спрашивать» живёт на клиенте. */
      return repaintDay();
    }
    if (b.id === 'cam') {
      var shot = document.getElementById('shot');
      if (shot) { shot.value = ''; shot.click(); }
      return;
    }
    if (b.id === 'sendtext') return submitText();
    if (b.id === 'mic') return state.recording ? stopRec() : startRec();
    if (b.dataset.repeat) {
      if (state.sending) return;
      state.sending = true;
      busy('Записываю…');
      var rid = 'repeat:' + b.dataset.repeat;
      return send('/api/meals/' + b.dataset.repeat + '/repeat?days_ago=' + writeDay(),
                  { headers: { 'Idempotency-Key': keyFor(rid) } })
        .then(function (r) { dropKey(rid); return handle(r); })
        .catch(function (err) { oops(err); renderDay(); })
        .then(function () { state.sending = false; });
    }
    if (b.dataset.drop) {
      var did = b.dataset.drop;
      return ask('Удалить этот приём?', function () {
        busy('Удаляю…');
        send('/api/meals/' + did, { method: 'DELETE' })
          .then(function () { delete state.open[did]; toast('Удалил.'); renderDay(); })
          .catch(function (err) { toast(err.message, true); renderDay(); });
      });
    }
    if (b.dataset.quick) {
      if (state.sending) return;
      state.sending = true;
      busy('Записываю…');
      var stay = state.tab === 'ai' || state.tab === 'cat';
      var qid = 'quick:' + b.dataset.quick + ':' + b.dataset.grams;
      return send('/api/meals/quick', {
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': keyFor(qid) },
        body: JSON.stringify({ product_id: Number(b.dataset.quick),
                               grams: Number(b.dataset.grams),
                               days_ago: writeDay() })
      }).then(function (r) {
        /* С вкладки AI не уводим на «День»: человек читает ответ и может
           записать ещё одно. Достаточно подтвердить тостом. */
        /* С вкладок AI и «Продукты» не уводим на «День»: человек читает ответ
           или листает каталог и может записать ещё. Уход туда стоил лишнего
           тапа на каждый следующий продукт и рассинхронивал подсветку вкладки. */
        dropKey(qid);
        if (stay) {
          toast(r.meal ? 'Записал ' + n(r.meal.kcal) + ' ккал.' : 'Готово.');
          return render();
        }
        return handle(r);
      }).catch(function (err) {
        oops(err);
        return stay ? render() : renderDay();
      }).then(function () { state.sending = false; });
    }
    if (b.dataset.say) {
      var inp = document.getElementById('say');
      inp.value = b.dataset.say;
      state.keepFocus = true;
      return submitText();
    }
    if (b.dataset.undo) {
      var id = b.dataset.undo;
      if (state.sending) return;
      state.sending = true;
      busy('Отменяю…');
      return send('/api/meals/' + id, { method: 'DELETE' })
        .then(function () { state.justLogged = null; toast('Отменил.'); renderDay(); })
        .catch(function (err) { oops(err); renderDay(); })
        .then(function () { state.sending = false; });
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
    if (b.id === 'bugsend') {
      var bi = document.getElementById('bug');
      var note = (bi && bi.value || '').trim();
      if (!note) return;   // кнопка при пустом поле просто ничего не делает
      bi.value = '';
      return send('/api/bug', {
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note: note })
      }).then(function () { toast('Отправил. Спасибо.'); })
        .catch(function (e) { toast(e.message, true); });
    }
    if (b.id === 'wsave') return saveWeight();
    if (b.dataset.order) {
      state.prefs = Object.assign({}, state.prefs || {}, { feed_order: b.dataset.order });
      repaintDay();
      /* Порядок запоминаем, но экран не ждёт ответа сервера: это оформление,
         и мигать из-за него нечестно. */
      send('/api/settings/prefs', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ feed_order: b.dataset.order })
      }).catch(function () {});
      return;
    }
    if (b.dataset.dups) {
      state.tab = 'cat';
      state.cat = { view: 'dups', offset: 0, q: '' };
      [].forEach.call(nav.querySelectorAll('button'), function (x) {
        if (x.dataset.tab === 'cat') x.setAttribute('aria-current', 'page');
        else x.removeAttribute('aria-current');
      });
      return renderCatalog();
    }
    if (b.dataset.period) { state.period = Number(b.dataset.period); return renderTotals(); }
    if (b.dataset.ask) return askCoach(b.dataset.ask);
    if (b.id === 'send') {
      var el = document.getElementById('ask');
      var t = (el.value || '').trim(); el.value = '';
      return askCoach(t);
    }
    if (b.dataset.merge != null) return doMerge(Number(b.dataset.merge));

    if (b.dataset.delI) {
      return dropItem(Number(b.dataset.delM), Number(b.dataset.delI));
    }
    if (b.dataset.saveG) {
      return saveGrams(Number(b.dataset.saveM), Number(b.dataset.saveG));
    }
    if (b.dataset.grams) {
      var gi = document.getElementById('grams');
      var grams = (gi && gi.value || '').trim();
      if (!grams) { toast('Введи вес в граммах или выбери размер.', true); return; }
      return sendAnswer(grams);
    }

    if (b.hasAttribute('data-answer')) {
      if (b.dataset.cancel) {
        state.clarify = null; state.label = null;
        return repaintDay();
      }
      return sendAnswer(b.dataset.answer);
    }
  });

  root.addEventListener('click', function (e) {
    var pick = e.target.closest('.pick');
    if (!pick) return;
    if (pick.dataset.target) {
      return patch('/api/settings/goal', { target: pick.dataset.target }, 'Режим изменён.');
    }
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
    busy('Сохраняю…');
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
        busy('Объединяю…');
        send('/api/products/merge', {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ winner_id: chosen, loser_ids: losers })
        }).then(function (r) {
          toast('Объединил. Записей перенесено: ' + r.items_repointed + '.');
          state.cat.dup = null;
          renderCatalog();
        }).catch(function (e) { toast(e.message, true); renderCatalog(); });
      });
  }

  var searchTimer = null;

  root.addEventListener('keydown', function (e) {
    var pick = e.target.closest && e.target.closest('.pick');
    if (pick && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault();
      pick.click();
      return;
    }
    if (e.key === 'Enter' && e.target.id === 'say') { e.preventDefault(); submitText(); }
    if (e.key === 'Enter' && e.target.id === 'grams') {
      e.preventDefault();
      var v = (e.target.value || '').trim();
      if (v) sendAnswer(v);
      return;
    }
    if (e.key === 'Enter' && e.target.id === 'ask') {
      e.preventDefault();
      var t = (e.target.value || '').trim(); e.target.value = '';
      askCoach(t);
    }
  });

  root.addEventListener('input', function (e) {
    if (e.target.id === 'say') {
      var was = !!(state.text || '').trim();
      state.text = e.target.value;
      /* Перерисовываем композер только на переходе «пусто ↔ не пусто»:
         на каждом символе это выбивало бы курсор. */
      if (was !== !!state.text.trim()) {
        var c = document.getElementById('composer');
        if (c) {
          c.outerHTML = composer();
          var i = document.getElementById('say');
          if (i) { i.value = state.text; i.focus(); }
        }
      }
      return;
    }
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

  /* Слушатель на скрытом поле, а не на кнопке: кнопка живёт внутри #screen и
     пересоздаётся на каждой перерисовке, поле — снаружи и переживает их. */
  document.getElementById('shot').addEventListener('change', function (e) {
    var f = e.target.files && e.target.files[0];
    if (f) sendLabel(f);
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

  /* Верхний отступ считает клиент, а не мы: у выреза устройства и у панели
     Telegram («Закрыть», «…») разные величины, и складывать их надо обе —
     иначе шапка уезжает под часы. Величины появились в Bot API 8.0; на клиенте
     постарше их просто нет, и CSS остаётся на env(safe-area-inset-top). */
  /* Молчание клиента и его ноль — РАЗНЫЕ ответы, и путать их дорого.
     null — клиент до Bot API 8.0 величин не знает, остаёмся на env().
     0 — клиент знает и говорит «отступ не нужен»: так открывается обычный, не
     полноэкранный режим, где Telegram сам поставил контент под свою шапку.
     Ноль, принятый за молчание, кладёт поверх env(safe-area-inset-top) — и под
     шапкой Telegram появляется вторая пустая полоса в высоту выреза.
     Чистая функция: её поведение проверяет `node web/check.mjs`. */
  function topInset(device, chrome) {
    if (!device && !chrome) return null;
    return ((device || {}).top || 0) + ((chrome || {}).top || 0);
  }

  function applyTopInset() {
    var top = topInset(tg.safeAreaInset, tg.contentSafeAreaInset);
    if (top === null) return;
    document.documentElement.style.setProperty('--top-inset', top + 'px');
  }

  tg.ready();
  tg.expand();
  applyTopInset();
  if (tg.onEvent) {
    /* Величины меняются на повороте и при переходе в полный экран. */
    try {
      tg.onEvent('safeAreaChanged', applyTopInset);
      tg.onEvent('contentSafeAreaChanged', applyTopInset);
      tg.onEvent('viewportChanged', applyTopInset);
    } catch (e) { /* старый клиент не знает этих событий */ }
  }
  loadFrequent();
  /* Настройки нужны до первой отрисовки: иначе панель мигнёт видом по умолчанию
     и только потом переключится на выбранный. */
  api('/api/settings').then(function (d) {
    state.prefs = d.prefs;
    /* Настройка обязана иметь поведение за собой (MA-INV-31). Раньше здесь
       значение просто присваивалось и не читалось нигде — переключатель
       выглядел рабочим и не делал ничего. */
    state.micFirst = !!d.prefs.mic_first;
  }).catch(function () {
    /* Настройки не пришли — рисуем с видом по умолчанию, а не пустой экран. */
    state.prefs = {};
  }).then(render);
  if (tg.setHeaderColor) { try { tg.setHeaderColor('#000000'); } catch (e) { /* старый клиент */ } }
})();
