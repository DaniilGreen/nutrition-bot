/* Мини-апп «Дневник КБЖУ». Без сборщика (§5.7): один файл, никаких зависимостей
   кроме telegram-web-app.js с telegram.org.

   Правило слоя: сервер считает, клиент рисует. Ни одного расчёта КБЖУ здесь
   быть не должно — это INV-1. */
(function () {
  'use strict';

  var tg = window.Telegram && window.Telegram.WebApp;
  /* Версия фронта — из адреса этого же скрипта (`/app.js?v=<хэш>`, MA-INV-35).
     currentScript есть только при синхронном исполнении, поэтому берём сразу. */
  var V = ((document.currentScript && document.currentScript.src || '')
    .match(/[?&]v=([^&]+)/) || [])[1] || '';
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

  /* §6.25: запись в ДРУГОЙ день, чем открыт, — подтверждение тостом с отменой. Экран
     остаётся на месте: человек листает вчера и повторяет оттуда по одной позиции. */
  function toastUndo(text, mealId) {
    toastAction(text, 'Отменить', 'data-undo="' + mealId + '"');
  }

  /* MA-INV-69: удалённое возвращается из тоста. 8 с — «Отменить» требует времени прочесть и
     дотянуться (дизайн-системы дают 5–10 с, если в тосте есть действие). */
  function toastAction(text, label, attr) {
    var el = document.getElementById('toast');
    el.innerHTML = '<span style="flex-grow:1">' + esc(text) + '</span>' +
      '<button class="undo" ' + attr + '>' + esc(label) + '</button>';
    el.className = 'toast with-undo';
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 8000);
  }

  function restoreToast(text, token) {
    if (token) toastAction(text, 'Вернуть', 'data-restore="' + esc(token) + '"');
    else toast(text);
  }

  /* «Сегодня / Вчера / сб, 11 окт» — день последнего раза в каталоге (§6.25). День
     считает сервер по поясу человека; здесь только подпись. */
  var WD = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
  var MON = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
  function dayName(ago, iso, cap) {
    if (ago === 0) return cap ? 'Сегодня' : 'сегодня';
    if (ago === 1) return cap ? 'Вчера' : 'вчера';
    var a = String(iso || '').split('-');
    if (a.length !== 3) return '';
    var d = new Date(Number(a[0]), Number(a[1]) - 1, Number(a[2]));
    return (ago < 7 ? WD[d.getDay()] + ', ' : '') + Number(a[2]) + ' ' + MON[Number(a[1]) - 1];
  }

  /* ── шторка порции (§6.25) ─────────────────────────────────────────────
     Одна на два места: позиция прошлого дня («ещё раз») и продукт в каталоге.
     Живёт вне #screen: перерисовка экрана её не сносит, а она не сносит экран. */
  var sheetEl = document.createElement('div');
  document.body.appendChild(sheetEl);

  function portions(ref) {
    var r = Math.round(ref);
    if (!(r > 0)) return [];
    var xs = [Math.round(r / 2), r, Math.round(r * 1.5)];
    return xs.filter(function (g, i) { return g > 0 && xs.indexOf(g) === i; });
  }

  function openSheet(sh) {
    sh.pick = sh.fixed ? 1 : (Math.round(sh.ref) || 100);
    sh.own = false;
    state.sheet = sh;
    paintSheet();
  }
  function closeSheet() { state.sheet = null; paintSheet(); }

  /* MA-INV-68: цифра под граммовкой — предпросмотр. Записанное считает сервер. */
  function sheetPreview(sh) {
    var g = Number(sh.pick) || 0;
    var pfc = sh.per.p != null
      ? '<span class="mic num"><span style="color:var(--prot)">Б ' + n(sh.per.p * g) + '</span> · ' +
        '<span style="color:var(--fat)">Ж ' + n(sh.per.f * g) + '</span> · ' +
        '<span style="color:var(--carb)">У ' + n(sh.per.c * g) + '</span></span>'
      : '';
    return '<span class="num" style="font-size:var(--f-t2);font-weight:700">' +
      n(sh.per.kcal * g) + '</span><span class="mic">ккал</span>' +
      '<span style="flex-grow:1"></span>' + pfc;
  }

  /* FR-39.6, макет C: набор перед записью — граммы ±, убрать, добавить из каталога.
     Цифры здесь — предпросмотр по снапшоту на грамм; записанное пересчитывает сервер. */
  function openSetSheet(x) {
    state.sheet = {
      kind: 'set', name: x.name, title: x.name, keep: 'today', q: '', found: [],
      n0: x.items.length,
      items: x.items.map(function (i, k) {
        var g = i.grams || 1;
        return { src: k, name: i.name, grams: i.grams, base: i.grams,
                 per: { kcal: i.kcal / g, p: i.protein_g / g, f: i.fat_g / g, c: i.carb_g / g } };
      })
    };
    paintSheet();
  }

  function setChanged(sh) {
    return sh.items.length !== sh.n0 || sh.items.some(function (it, k) {
      return it.src !== k || it.grams !== it.base;
    });
  }

  function paintSetSheet(sh) {
    var t = { kcal: 0, p: 0, f: 0, c: 0, g: 0 };
    var rows = sh.items.map(function (it, k) {
      t.kcal += it.per.kcal * it.grams; t.p += it.per.p * it.grams;
      t.f += it.per.f * it.grams; t.c += it.per.c * it.grams; t.g += it.grams;
      return '<div class="srow">' +
        '<span class="nm">' + esc(it.name) + '</span>' +
        '<button class="step" data-sdec="' + k + '" aria-label="Меньше: ' + esc(it.name) + '">−</button>' +
        '<span class="num sg">' + n(it.grams) + '\u00a0г</span>' +
        '<button class="step" data-sinc="' + k + '" aria-label="Больше: ' + esc(it.name) + '">+</button>' +
        '<span class="num kk">' + n(it.per.kcal * it.grams) + '</span>' +
        '<button class="sx" data-sdrop="' + k + '" aria-label="Убрать: ' + esc(it.name) + '">✕</button>' +
      '</div>';
    }).join('') || '<div class="state">Пусто — добавь что-нибудь ниже.</div>';
    var keep = setChanged(sh)
      ? '<div class="where" role="group" aria-label="Что сделать с изменениями">' +
          ['today', 'update'].map(function (v) {
            var on = sh.keep === v;
            return '<button data-skeep="' + v + '" aria-pressed="' + on + '"' +
              (on ? ' class="on"' : '') + '>' + (v === 'today' ? 'Только сегодня' : 'Обновить набор') +
              '</button>';
          }).join('') + '</div>'
      : '';
    sheetEl.innerHTML =
      '<div class="sheet-back" data-sclose="1"></div>' +
      '<div class="sheet" role="dialog" aria-modal="true" aria-label="Набор ' + esc(sh.title) + '">' +
        '<div class="grip" aria-hidden="true"></div>' +
        '<div style="display:flex;gap:var(--s-2);align-items:flex-start">' +
          '<div style="flex-grow:1;min-width:0"><div class="sh-t">' + esc(sh.title) + '</div>' +
          '<div class="mic" style="margin-top:4px">Поправь под сегодняшний — и запиши</div></div>' +
          '<button class="x" data-sclose="1" aria-label="Закрыть">✕</button></div>' +
        '<div class="slist">' + rows + '</div>' +
        '<label class="composer" style="margin:0">' +
          '<input id="sq" type="search" autocomplete="off" placeholder="Добавить: найди продукт" ' +
          'value="' + esc(sh.q) + '" aria-label="Добавить продукт в набор" ' +
          'style="flex-grow:1;background:none;border:0;color:var(--text);font:inherit;' +
          'outline:none;min-width:0">' + '<button class="scam" data-scam="1" aria-label="Фото этикетки">' +
          '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
          'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
          '<path d="M3 8h3l2-3h8l2 3h3v11H3z"/><circle cx="12" cy="13" r="3.5"/></svg></button>' + '</label>' +
        '<div class="sh-chips" id="schips">' + foundChips(sh) + '</div>' +
        /* Перенос блоками, а не словами: на 320 при больших числах «У 34» рвалось на две строки. */
        '<div class="sh-prev" style="flex-wrap:wrap"><span class="num" ' +
          'style="font-size:var(--f-t2);font-weight:700">' + n(t.kcal) + '</span>' +
          '<span class="mic" style="white-space:nowrap">ккал · ' + n(t.g) + '\u00a0г</span>' +
          '<span style="flex-grow:1"></span><span class="mic num" style="white-space:nowrap">' +
          '<span style="color:var(--prot)">Б ' + n(t.p) + '</span> · ' +
          '<span style="color:var(--fat)">Ж ' + n(t.f) + '</span> · ' +
          '<span style="color:var(--carb)">У ' + n(t.c) + '</span></span></div>' +
        keep +
        '<button class="primary" data-setgo="1"' + (sh.items.length ? '' : ' disabled') + '>' +
          'Записать · ' + n(t.kcal) + ' ккал</button>' +
      '</div>';
  }

  function setWrite() {
    var sh = state.sheet;
    if (!sh || state.sending || !sh.items.length) return;
    state.sending = true;
    busy('Записываю…');
    /* Имя позиции — эхом: если набор успели обновить с другого устройства, сервер откажет,
       а не положит граммы на чужую позицию. */
    var picks = sh.items.map(function (i) {
      return i.src != null ? { src: i.src, name: i.name, grams: i.grams }
                           : { product_id: i.product, grams: i.grams };
    });
    var update = setChanged(sh) && sh.keep === 'update';
    var sid = 'setlog:' + sh.name + ':' + JSON.stringify(picks) + ':' + update;
    send('/api/sets/log', {
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': keyFor(sid) },
      body: JSON.stringify({ name: sh.name, items: picks, update: update, days_ago: writeDay() })
    }).then(function (r) {
      dropKey(sid);
      closeSheet();
      if (update) loadFrequent();  // состав набора поменялся — и ккал в чипе тоже
      return r.kind === 'logged' ? logged(r, 0) : handle(r);
    }).catch(function (err) {
      oops(err);
      rerender();  // busy() погасил экран — без перерисовки он остался бы неживым
    }).then(function () { state.sending = false; });
  }

  /* FR-39.7: лист правки состава рецепта. Граммы — цифрами в поле (точные веса партии), набор
     текста обновляет только итог, без перерисовки: иначе поле теряло бы фокус на каждой цифре. */
  function openRecipeSheet(p, r) {
    state.sheet = {
      kind: 'recipe', id: p.id, title: p.name, q: '', found: [],
      yieldG: r && r.yield_g ? r.yield_g : '',
      items: (r ? r.items : []).map(function (i, k) {
        return { src: k, name: i.name, grams: i.grams,
                 per: { kcal: i.kcal_per_100g / 100, p: i.protein_per_100g / 100,
                        f: i.fat_per_100g / 100, c: i.carb_per_100g / 100 } };
      })
    };
    paintSheet();
  }

  function recipeTotals(sh) {
    var t = { kcal: 0, p: 0, f: 0, c: 0, g: 0 };
    sh.items.forEach(function (it) {
      var g = Number(it.grams) || 0;
      t.kcal += it.per.kcal * g; t.p += it.per.p * g; t.f += it.per.f * g; t.c += it.per.c * g;
      t.g += g;
    });
    var y = Number(sh.yieldG) > 0 ? Number(sh.yieldG) : t.g;
    return { t: t, y: y, per: y > 0 ? 100 / y : 0 };
  }

  function recipePreview(sh) {
    var x = recipeTotals(sh);
    return '<span class="num" style="font-size:var(--f-t2);font-weight:700">' +
        n(x.t.kcal * x.per) + '</span><span class="mic" style="white-space:nowrap">ккал на 100\u00a0г</span>' +
      '<span style="flex-grow:1"></span><span class="mic num" style="white-space:nowrap">' +
        '<span style="color:var(--prot)">Б ' + n(x.t.p * x.per, 1) + '</span> · ' +
        '<span style="color:var(--fat)">Ж ' + n(x.t.f * x.per, 1) + '</span> · ' +
        '<span style="color:var(--carb)">У ' + n(x.t.c * x.per, 1) + '</span></span>' +
      '<span class="mic" style="flex-basis:100%">Партия ' + n(x.t.g) + '\u00a0г · ' +
        n(x.t.kcal) + ' ккал</span>';
  }

  function paintRecipeSheet(sh) {
    var rows = sh.items.map(function (it, k) {
      return '<div class="srow">' +
        '<span class="nm">' + esc(it.name) + '</span>' +
        '<label class="gin"><input type="number" inputmode="decimal" min="1" max="20000" ' +
          'data-rgrams="' + k + '" value="' + esc(it.grams) + '" aria-label="Граммы: ' +
          esc(it.name) + '"><span class="mic">г</span></label>' +
        '<button class="sx" data-sdrop="' + k + '" aria-label="Убрать: ' + esc(it.name) + '">✕</button>' +
      '</div>';
    }).join('') || '<div class="state">Пусто — найди ингредиенты ниже.</div>';
    sheetEl.innerHTML =
      '<div class="sheet-back" data-sclose="1"></div>' +
      '<div class="sheet" role="dialog" aria-modal="true" aria-label="Состав рецепта ' + esc(sh.title) + '">' +
        '<div class="grip" aria-hidden="true"></div>' +
        '<div style="display:flex;gap:var(--s-2);align-items:flex-start">' +
          '<div style="flex-grow:1;min-width:0"><div class="sh-t">' + esc(sh.title) + '</div>' +
          '<div class="mic" style="margin-top:4px">Состав рецепта — плотность пересчитается</div></div>' +
          '<button class="x" data-sclose="1" aria-label="Закрыть">✕</button></div>' +
        '<div class="slist">' + rows + '</div>' +
        '<label class="composer" style="margin:0">' +
          '<input id="sq" type="search" autocomplete="off" placeholder="Добавить ингредиент" ' +
          'value="' + esc(sh.q) + '" aria-label="Добавить ингредиент" ' +
          'style="flex-grow:1;background:none;border:0;color:var(--text);font:inherit;' +
          'outline:none;min-width:0">' + '<button class="scam" data-scam="1" aria-label="Фото этикетки">' +
          '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
          'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
          '<path d="M3 8h3l2-3h8l2 3h3v11H3z"/><circle cx="12" cy="13" r="3.5"/></svg></button>' + '</label>' +
        '<div class="sh-chips" id="schips">' + foundChips(sh) + '</div>' +
        '<label class="field" style="margin:0"><span class="mic" style="flex-grow:1">' +
          'Вышло готового, г — если при готовке ушла вода</span>' +
          '<input id="ryield" class="num" type="number" inputmode="decimal" min="1" max="50000" ' +
          'placeholder="= сумме" value="' + esc(sh.yieldG) + '"></label>' +
        '<div class="sh-prev" id="rprev" style="flex-wrap:wrap">' + recipePreview(sh) + '</div>' +
        '<button class="primary" data-rsave="1"' + (sh.items.length ? '' : ' disabled') + '>' +
          'Сохранить рецепт</button>' +
      '</div>';
  }

  function recipeSave() {
    var sh = state.sheet;
    if (!sh || state.sending || !sh.items.length) return;
    var bad = sh.items.some(function (i) { return !(Number(i.grams) > 0); });
    if (bad) return toast('У каждого ингредиента нужен вес больше нуля.', true);
    if (sh.items.some(function (i) { return Number(i.grams) > 20000; })) {
      return toast('Ингредиент тяжелее 20 кг — проверь число.', true);
    }
    if (Number(sh.yieldG) > 50000) return toast('Вес готового больше 50 кг — проверь число.', true);
    state.sending = true;
    busy('Пересчитываю…');
    send('/api/products/' + sh.id + '/recipe', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        yield_g: Number(sh.yieldG) > 0 ? Number(sh.yieldG) : null,
        items: sh.items.map(function (i) {
          return i.src != null ? { src: i.src, name: i.name, grams: Number(i.grams) }
                               : { product_id: i.product, grams: Number(i.grams) };
        })
      })
    }).then(function (r) {
      closeSheet();
      toast('Рецепт пересчитан: ' + n(r.product.kcal_per_100g) + ' ккал на 100 г.');
      toCard(sh.id);
    }).catch(function (err) {
      oops(err);
      if (err && err.code === 'recipe_stale') {
        closeSheet();            // состав поменяли с другого устройства — открыть старый
        return toCard(sh.id);    // снова значило бы упереться в тот же отказ
      }
      root.classList.remove('working');  // лист остаётся открытым — правку не теряем
    }).then(function () { state.sending = false; });
  }

  /* Лист рецепта открывают и из карточки, и из шторки в списке (§6.26) — итог смотрим в карточке. */
  function toCard(id) {
    state.cat.view = 'product';
    state.cat.product = id;
    renderProduct();
  }

  var setSearchTimer = null;

  function foundChips(sh) {
    /* FR-39.8: набранное всегда можно добавить как есть — разберёт модель. Раньше в состав
       попадало только найденное в каталоге, а набранный текст молча пропадал. */
    var q = (sh.q || '').trim();
    var free = sh.parsing
      ? '<button class="chip" disabled>разбираю…</button>'
      : q.length >= 2
        ? '<button class="chip free" data-sfree="1">+ «' + esc(q) + '» — разобрать</button>'
        : '';
    return free + sh.found.map(function (p) {
      return '<button class="chip" data-sadd="' + p.id + '">+ ' + esc(p.name) + '</button>';
    }).join('');
  }

  /* FR-39.8: строки от разбора текста или этикетки — в лист. Плотность на грамм из каталога —
     это предпросмотр (MA-INV-68); сохраняет и пересчитывает сервер. */
  function addIngredients(sh, r) {
    (r.items || []).forEach(function (x) {
      var p = x.product;
      if (sh.kind === 'recipe' && p.id === sh.id) return;  // рецепт сам в себя не входит
      sh.items.push({ product: p.id, name: p.name, grams: x.grams, base: null,
                      per: { kcal: p.kcal_per_100g / 100, p: p.protein_per_100g / 100,
                             f: p.fat_per_100g / 100, c: p.carb_per_100g / 100 } });
    });
    if (r.skipped && r.skipped.length) {
      toast('Не понял: ' + r.skipped.join(', ') + '. Напиши продукт и вес иначе.', true);
    } else if (!(r.items || []).length) {
      toast('Не нашёл, что добавить — напиши продукт и вес.', true);
    }
  }

  function ingredientsFrom(sh, req) {
    sh.parsing = true;
    showFound(sh);
    req.then(function (r) {
      if (state.sheet !== sh) return;
      addIngredients(sh, r);
      sh.q = ''; sh.found = [];
    }).catch(function (err) { oops(err); })
      .then(function () {
        sh.parsing = false;
        if (state.sheet === sh) paintSheet();
      });
  }

  function parseIngredient(sh) {
    var text = (sh.q || '').trim();
    if (text.length < 2 || sh.parsing) return;
    ingredientsFrom(sh, send('/api/ingredients/parse', {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text })
    }));
  }

  /* Скрытое поле снаружи листа: лист перерисовывается, поле и его слушатель — нет. */
  document.getElementById('ingshot').addEventListener('change', function (e) {
    var f = e.target.files && e.target.files[0];
    var sh = state.sheet;
    e.target.value = '';
    if (!f || !sh || sh.parsing) return;
    shrink(f, function (blob) {
      ingredientsFrom(sh, send('/api/ingredients/label', {
        headers: { 'Content-Type': blob.type || 'image/jpeg' },
        body: blob
      }));
    });
  });

  /* Найденное — только в контейнер чипов, без перерисовки листа: медленный ответ поиска
     иначе уводил фокус из поля граммов, а прокрутка прыгала наверх (ревью 15.10.2026). */
  function showFound(sh) {
    var box = document.getElementById('schips');
    if (box && state.sheet === sh) box.innerHTML = foundChips(sh);
  }

  function paintSheet() {
    var sh = state.sheet;
    if (!sh) { sheetEl.innerHTML = ''; return; }
    if (sh.mode === 'entry') return paintEntrySheet(sh);
    if (sh.kind === 'set' || sh.kind === 'recipe') {
      /* Добавил/убрал позицию — лист перерисован, но прокрутка остаётся, где была. */
      var old = sheetEl.querySelector('.sheet');
      var top = old ? old.scrollTop : 0;
      (sh.kind === 'set' ? paintSetSheet : paintRecipeSheet)(sh);
      var now = sheetEl.querySelector('.sheet');
      if (now && top) now.scrollTop = top;
      return;
    }
    var ref = Math.round(sh.ref);
    var chips = sh.fixed ? '' : portions(sh.ref).map(function (g) {
      var on = !sh.own && g === sh.pick;
      return '<button class="chip' + (on ? ' on' : '') + '" data-sg="' + g + '" aria-pressed="' +
        on + '">' + n(g) + ' г' + (g === ref ? ' · ' + esc(sh.refLabel) : '') + '</button>';
    }).join('') +
      '<button class="chip' + (sh.own ? ' on' : '') + '" data-sown="1" aria-pressed="' +
      sh.own + '">своё…</button>';
    if (sh.fixed) chips = '';
    var own = sh.own
      ? '<label class="composer" style="margin:0">' +
        '<input id="sgrams" type="number" inputmode="numeric" min="1" max="5000" ' +
        'value="' + (sh.pick || '') + '" aria-label="Граммы" ' +
        'style="flex-grow:1;background:none;border:0;color:var(--text);font:inherit;' +
        'outline:none;min-width:0"><span class="mic">г</span></label>'
      : '';
    var where = sh.from > 0
      ? '<div class="where" role="group" aria-label="Куда записать">' +
        '<button data-sto="0" aria-pressed="' + (sh.to === 0) + '"' +
          (sh.to === 0 ? ' class="on"' : '') + '>Сегодня</button>' +
        '<button data-sto="' + sh.from + '" aria-pressed="' + (sh.to === sh.from) + '"' +
          (sh.to === sh.from ? ' class="on"' : '') + '>' + esc(sh.fromLabel) + '</button></div>'
      : '';
    /* MA §6.26: у рецепта состав — в один тап отсюда, а не через карточку. */
    var extra = sh.src === 'cat' ? ''
      : sh.kind === 'item'
        ? '<button class="ghost" data-sedit="1">Править эту запись</button>'
      : sh.recipe
        ? '<div class="sh-two"><button class="ghost" data-srecipe="1">Состав рецепта ›</button>' +
          '<button class="ghost" data-scard="1">Карточка ›</button></div>'
        : '<button class="ghost" data-scard="1">Карточка продукта ›</button>';
    sheetEl.innerHTML =
      '<div class="sheet-back" data-sclose="1"></div>' +
      '<div class="sheet" role="dialog" aria-modal="true" aria-label="' + esc(sh.title) + '">' +
        '<div class="grip" aria-hidden="true"></div>' +
        '<div style="display:flex;gap:var(--s-2);align-items:flex-start">' +
          '<div style="flex-grow:1;min-width:0"><div class="sh-t">' + esc(sh.title) + '</div>' +
          '<div class="mic" style="margin-top:4px">' + esc(sh.sub) + '</div></div>' +
          '<button class="x" data-sclose="1" aria-label="Закрыть">✕</button></div>' +
        (chips ? '<div class="sh-chips">' + chips + '</div>' : '') + own + where +
        '<div class="sh-prev" id="sprev">' + sheetPreview(sh) + '</div>' +
        '<button class="primary" data-sgo="1">Записать ' +
          esc(sh.to === 0 ? 'сегодня' : 'в ' + sh.fromLabel) + '</button>' +
        extra +
      '</div>';
    if (sh.own) {
      var inp = document.getElementById('sgrams');
      if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
    }
  }

  function sheetWrite() {
    var sh = state.sheet;
    var g = Number(sh.pick);
    if (!sh.fixed && !(g > 0 && g <= 5000)) return toast('Вес вне разумных пределов.', true);
    if (state.sending) return;
    state.sending = true;
    var key = 'sheet:' + sh.kind + ':' + (sh.item || sh.product) + ':' + g + ':' + sh.to;
    var hdr = { 'Content-Type': 'application/json', 'Idempotency-Key': keyFor(key) };
    var req = sh.kind === 'item'
      ? send('/api/meals/' + sh.meal + '/items/' + sh.item + '/repeat',
             { headers: hdr, body: JSON.stringify(sh.fixed ? { days_ago: sh.to }
                                                             : { grams: g, days_ago: sh.to }) })
      : send('/api/meals/quick',
             { headers: hdr, body: JSON.stringify({ product_id: sh.product, grams: g,
                                                     days_ago: 0 }) });
    var to = sh.to;
    closeSheet();
    req.then(function (r) { dropKey(key); logged(r, to); })
      .catch(function (err) { oops(err); })
      .then(function () { state.sending = false; });
  }

  /* Записали в открытый день — обычная карточка «Записал». В другой — тост с отменой,
     экран на месте (§6.25). */
  function logged(r, toDaysAgo) {
    if (state.tab === 'day' && toDaysAgo === state.daysAgo) return handle(r);
    if (r.meal) {
      toastUndo('Записал в ' + (toDaysAgo === 0 ? 'сегодня' : 'тот день') + ': ' +
                mealTitle(r.meal) + ' · ' + n(r.meal.kcal) + ' ккал', r.meal.id);
    } else {
      toast('Готово.');
    }
    rerender();
  }

  document.getElementById('toast').addEventListener('click', function (e) {
    var b = e.target.closest('button[data-undo],button[data-restore]');
    if (!b) return;
    this.hidden = true;
    if (b.dataset.restore) {
      return send('/api/meals/restore', {
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ undo: b.dataset.restore })
      }).then(function (r) {
        toast('Вернул.');
        if (r.meal) { state.open[r.meal.id] = true; }
        rerender();
      }).catch(function (err) {
        /* MA-INV-80: запись с тех пор поправили, слили набором или вернули в боте — снимок
           вернул бы прошлое поверх или задвоил позиции. Экран мог это ещё не показать. */
        if (err.code !== 'undo_stale') return oops(err);
        toast('Запись уже изменилась — вернуть нельзя.', true);
        rerender();
      });
    }
    send('/api/meals/' + b.dataset.undo, { method: 'DELETE' })
      .then(function () { state.fresh = null; toast('Отменил.'); rerender(); })
      .catch(function (err) { oops(err); });
  });

  sheetEl.addEventListener('click', function (e) {
    var sh = state.sheet;
    if (!sh) return;
    var t = e.target.closest('[data-sclose],button');
    if (!t) return;
    if (t.dataset.sclose) return closeSheet();
    if ((sh.kind === 'recipe' || sh.kind === 'set') && t.dataset.sfree) return parseIngredient(sh);
    if ((sh.kind === 'recipe' || sh.kind === 'set') && t.dataset.scam) {
      e.preventDefault();  // кнопка внутри <label>: без этого фокус уезжал бы в поле поиска
      if (!sh.parsing) document.getElementById('ingshot').click();
      return;
    }
    if (sh.kind === 'recipe') {
      if (t.dataset.sdrop) { sh.items.splice(Number(t.dataset.sdrop), 1); return paintSheet(); }
      if (t.dataset.rsave) return recipeSave();
      if (t.dataset.sadd) {
        var rp = sh.found.filter(function (x) { return String(x.id) === t.dataset.sadd; })[0];
        if (!rp || rp.id === sh.id) return;  // рецепт сам в себя не входит
        sh.items.push({ product: rp.id, name: rp.name, grams: refGrams(rp),
                        per: { kcal: rp.kcal_per_100g / 100, p: rp.protein_per_100g / 100,
                               f: rp.fat_per_100g / 100, c: rp.carb_per_100g / 100 } });
        sh.q = ''; sh.found = [];
        return paintSheet();
      }
      return;
    }
    if (sh.kind === 'set') {
      var it = sh.items[Number(t.dataset.sdec || t.dataset.sinc || t.dataset.sdrop)];
      var step = it && it.grams < 30 ? 5 : 10;  // ложка соуса и 120 г начинки — разный шаг
      if (t.dataset.sdec && it) { it.grams = Math.max(step, it.grams - step); return paintSheet(); }
      if (t.dataset.sinc && it) { it.grams = Math.min(5000, it.grams + step); return paintSheet(); }
      if (t.dataset.sdrop && it) { sh.items.splice(sh.items.indexOf(it), 1); return paintSheet(); }
      if (t.dataset.skeep) { sh.keep = t.dataset.skeep; return paintSheet(); }
      if (t.dataset.setgo) return setWrite();
      if (t.dataset.sadd) {
        var p = sh.found.filter(function (x) { return String(x.id) === t.dataset.sadd; })[0];
        if (!p) return;
        sh.items.push({ product: p.id, name: p.name, grams: refGrams(p), base: null,
                        per: { kcal: p.kcal_per_100g / 100, p: p.protein_per_100g / 100,
                               f: p.fat_per_100g / 100, c: p.carb_per_100g / 100 } });
        sh.q = ''; sh.found = [];
        return paintSheet();
      }
      return;
    }
    if (sh.mode === 'entry') {
      if (t.dataset.ssave) return entryAct(sh, 'save');
      if (t.dataset.sagain) return entryAct(sh, 'again');
      if (t.dataset.sremove) return entryAct(sh, 'remove');
      if (t.dataset.sfix) return entryAct(sh, 'fix');
    }
    if (t.dataset.sg) { sh.pick = Number(t.dataset.sg); sh.own = false; return paintSheet(); }
    if (t.dataset.sown) { sh.own = true; return paintSheet(); }
    if (t.dataset.sto != null) { sh.to = Number(t.dataset.sto); return paintSheet(); }
    if (t.dataset.sgo) return sheetWrite();
    if (t.dataset.srecipe) {
      if (state.sending) return;
      t.disabled = true;  // чтение, не запись: экран не гасим, шторка ждёт ответа
      return api('/api/products/' + sh.product).then(function (d) {
        if (state.sheet === sh) openRecipeSheet(d.product, d.recipe);
      }).catch(function (err) { t.disabled = false; oops(err); });
    }
    if (t.dataset.scard) {
      var pid = sh.product;
      closeSheet();
      state.cat.view = 'product';
      state.cat.product = pid;
      return renderCatalog();
    }
  });
  sheetEl.addEventListener('input', function (e) {
    if (state.sheet && state.sheet.kind === 'recipe' &&
        (e.target.dataset.rgrams != null || e.target.id === 'ryield')) {
      var rs = state.sheet;
      if (e.target.id === 'ryield') rs.yieldG = e.target.value;
      else rs.items[Number(e.target.dataset.rgrams)].grams = e.target.value;
      var rpv = document.getElementById('rprev');
      if (rpv) rpv.innerHTML = recipePreview(rs);
      return;
    }
    if (e.target.id === 'sq' && state.sheet &&
        (state.sheet.kind === 'set' || state.sheet.kind === 'recipe')) {
      var sh = state.sheet, q = e.target.value;
      sh.q = q;
      showFound(sh);  // чип «+ «текст»» следует за вводом сразу, не ждёт поиска
      clearTimeout(setSearchTimer);
      if (q.trim().length < 2) { sh.found = []; return showFound(sh); }
      setSearchTimer = setTimeout(function () {
        api('/api/products?sort=name&limit=6&q=' + encodeURIComponent(q.trim()))
          .then(function (d) {
            if (state.sheet !== sh || sh.q !== q) return;  // пока шёл запрос, ввод ушёл дальше
            sh.found = d.items.filter(function (x) {
              return !x.stated && !(sh.kind === 'recipe' && x.id === sh.id);
            });
            showFound(sh);
          }).catch(function () {});
      }, 250);
      return;
    }
    if (e.target.id === 'sfix' && state.sheet) { state.sheet.fix = e.target.value; return; }
    if (e.target.id !== 'sgrams' || !state.sheet) return;
    var sv = document.getElementById('ssave');
    if (sv) sv.textContent = 'Сохранить · ' + n(Number(e.target.value)) + ' г';
    state.sheet.pick = Number(e.target.value);
    var pv = document.getElementById('sprev');
    if (pv) pv.innerHTML = sheetPreview(state.sheet);
  });
  sheetEl.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeSheet();
    if (e.key === 'Enter' && e.target.id === 'sfix' && state.sheet) {
      e.preventDefault();
      return entryAct(state.sheet, 'fix');
    }
    if (e.key === 'Enter' && e.target.id === 'sgrams') {
      return state.sheet && state.sheet.mode === 'entry' ? entryAct(state.sheet, 'save')
                                                         : sheetWrite();
    }
    if (e.key === 'Enter' && e.target.id === 'sq' && state.sheet &&
        (state.sheet.kind === 'set' || state.sheet.kind === 'recipe')) {
      e.preventDefault();
      parseIngredient(state.sheet);
    }
  });

  /* §6.27: шторка записи — одна на любой день. Снимок КБЖУ на грамм — из самой позиции
     (MA-INV-67, INV-3); у «своих цифр» без веса граммы не выбираются, остальное доступно. */
  function itemSheet(mealId, itemId) {
    var d = state.lastDay;
    var m = d && (d.meals || []).filter(function (x) { return x.id === mealId; })[0];
    var i = m && (m.items || []).filter(function (x) { return x.id === itemId; })[0];
    if (!i) return;
    var g = i.grams > 0 ? i.grams : 0;
    var label = dayName(state.daysAgo, d.day, false);
    openSheet({
      kind: 'item', mode: 'entry', meal: mealId, item: itemId, title: i.name,
      sub: label + ' ' + m.at + ' · ' + (g ? n(g) + ' г · ' : 'свои цифры · ') +
           n(i.kcal) + ' ккал' + (INPUT_MARK[m.input_type] ? ' · ' + INPUT_MARK[m.input_type] : ''),
      ref: g, base: g, fixed: !g, fix: '',
      per: g ? { kcal: i.kcal / g, p: i.protein_g / g, f: i.fat_g / g, c: i.carb_g / g }
             : { kcal: i.kcal, p: i.protein_g, f: i.fat_g, c: i.carb_g },
      from: state.daysAgo, to: 0
    });
  }

  /* Вес чипами: половина · как есть · полтора · ×2 (ресёрч §6.27: популярные порции под
     пальцем, без набора цифр) и «своё». */
  function entryChips(sh) {
    var g = Math.round(sh.base);
    var xs = [Math.round(g / 2), g, Math.round(g * 1.5), g * 2];
    return xs.filter(function (x, k) { return x > 0 && xs.indexOf(x) === k; }).map(function (x) {
      var on = !sh.own && x === sh.pick;
      var tail = x === g ? ' · как есть' : x === g * 2 ? ' · ×2' : '';
      return '<button class="chip' + (on ? ' on' : '') + '" data-sg="' + x + '" aria-pressed="' +
        on + '">' + n(x) + ' г' + tail + '</button>';
    }).join('') + '<button class="chip' + (sh.own ? ' on' : '') + '" data-sown="1" aria-pressed="' +
      sh.own + '">своё…</button>';
  }

  function paintEntrySheet(sh) {
    var changed = !sh.fixed && Number(sh.pick) !== Math.round(sh.base);
    var own = sh.own
      ? '<label class="composer" style="margin:0">' +
        '<input id="sgrams" type="number" inputmode="numeric" min="1" max="2000" ' +
        'value="' + (sh.pick || '') + '" aria-label="Граммы" ' +
        'style="flex-grow:1;background:none;border:0;color:var(--text);font:inherit;' +
        'outline:none;min-width:0"><span class="mic">г</span></label>'
      : '';
    sheetEl.innerHTML =
      '<div class="sheet-back" data-sclose="1"></div>' +
      '<div class="sheet" role="dialog" aria-modal="true" aria-label="' + esc(sh.title) + '">' +
        '<div class="grip" aria-hidden="true"></div>' +
        '<div style="display:flex;gap:var(--s-2);align-items:flex-start">' +
          '<div style="flex-grow:1;min-width:0"><div class="sh-t">' + esc(sh.title) + '</div>' +
          '<div class="mic" style="margin-top:4px">' + esc(sh.sub) + '</div></div>' +
          '<button class="x" data-sclose="1" aria-label="Закрыть">✕</button></div>' +
        (sh.fixed ? '' : '<div class="sh-chips">' + entryChips(sh) + '</div>') + own +
        '<label class="composer" style="margin:0">' +
          '<input id="sfix" type="text" autocomplete="off" enterkeyhint="done" ' +
          'placeholder="Поправить словами: «это был булгур»" value="' + esc(sh.fix) + '" ' +
          'aria-label="Поправить словами" style="flex-grow:1;background:none;border:0;' +
          'color:var(--text);font:inherit;outline:none;min-width:0">' +
          '<button class="scam" data-sfix="1" aria-label="Поправить" style="width:auto;' +
            'padding:0 12px;font:inherit;font-size:var(--f-body-s)">' +
            (sh.busy === 'fix' ? '…' : 'Ок') + '</button></label>' +
        '<div class="sh-prev" id="sprev">' + sheetPreview(sh) + '</div>' +
        /* При «своё…» кнопка есть всегда: набор цифр не перерисовывает шторку (фокус),
           подпись обновляется на месте. */
        (changed || sh.own
          ? '<button class="primary" id="ssave" data-ssave="1">Сохранить · ' + n(sh.pick) +
            ' г</button>'
          : '') +
        '<div class="sh-two">' +
          '<button class="ghost" data-sagain="1">↻ Ещё раз сегодня</button>' +
          '<button class="ghost danger" data-sremove="1">Убрать</button></div>' +
      '</div>';
    if (sh.own) {
      var inp = document.getElementById('sgrams');
      if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
    }
  }

  function entryAct(sh, what) {
    if (state.sending || sh.busy) return;
    var base = '/api/meals/' + sh.meal + '/items/' + sh.item;
    var g = Number(sh.pick);
    if (what === 'save' && !(g > 0 && g <= 2000)) return toast('Вес — от 1 до 2000 г за позицию.', true);
    var key = 'entry:' + what + ':' + sh.item + ':' + g;
    var req;
    if (what === 'save') {
      req = send(base, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                         body: JSON.stringify({ grams: g }) });
    } else if (what === 'again') {
      req = send(base + '/repeat', {
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': keyFor(key) },
        body: JSON.stringify(sh.fixed ? { days_ago: 0 } : { grams: g, days_ago: 0 }) });
    } else if (what === 'remove') {
      req = send(base, { method: 'DELETE' });
    } else {
      var text = (sh.fix || '').trim();
      if (text.length < 2) return toast('Напиши, что это было: «это был булгур», «индейка 150 г».', true);
      req = send(base + '/fix', { headers: { 'Content-Type': 'application/json' },
                                  body: JSON.stringify({ text: text }) });
    }
    sh.busy = what;
    state.sending = true;
    if (what === 'fix') paintSheet();  // «…» на кнопке: разбор идёт к модели, это секунды
    req.then(function (r) {
      dropKey(key);
      closeSheet();
      if (what === 'again') return logged(r, 0);
      if (what === 'remove') {
        restoreToast('Убрал: ' + sh.title + (r.meal ? '' : ' — запись опустела и удалена'), r.undo);
      } else if (what === 'save') {
        toast(sh.title + ': было ' + n(r.old_grams) + ' г, стало ' + n(r.item.grams) + ' г.');
      } else {
        toast('Поправил: ' + mealTitle(r.meal) + '.');
        state.open[r.meal.id] = true;
      }
      renderDay();
    }).catch(function (err) {
      sh.busy = null;
      oops(err);
      if (state.sheet === sh) paintSheet();  // шторка остаётся — правку не теряем
    }).then(function () { state.sending = false; });
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

  function show(html) { root.classList.remove('working'); root.innerHTML = html; markStale(); }

  /* MA-INV-74: сохранённый, ещё не подтверждённый сервером день приглушён и не
     нажимается — кроме поля ввода: записать можно сразу, ввод не ждёт сети. */
  function markStale() {
    root.classList.toggle('stale', state.tab === 'day' && !!state.staleDay &&
                                   state.lastDay === state.staleDay);
  }

  /* Перерисовать всё, КРОМЕ поля ввода: узел композера не покидает документ, и
     фокус, каретка и набор через IME не сбиваются. Пересоздание под пальцем
     человека — то же стирание экрана, что запрещает MA-INV-32. */
  function showAround(html) {
    var keep = document.getElementById('composer');
    var tmp = document.createElement('div');
    tmp.innerHTML = html;
    var slot = tmp.querySelector('#composer');
    if (!keep || keep.parentNode !== root || !slot) return show(html);
    root.classList.remove('working');
    [].slice.call(root.childNodes).forEach(function (x) { if (x !== keep) root.removeChild(x); });
    var before = true;
    [].slice.call(tmp.childNodes).forEach(function (x) {
      if (x === slot) { before = false; return; }
      root.insertBefore(x, before ? keep : null);
    });
    markStale();
  }

  function naming() {
    var a = document.activeElement;
    return !!a && (a.id === 'sessname' || a.id === 'setname');
  }

  function typing() {
    var s = document.getElementById('say');
    return !!s && (document.activeElement === s || !!s.value.trim());
  }

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

  /* Скелет — только при заходе на экран. Перерисовка того же экрана (выбор в
     настройках, дублях, смена периода) идёт поверх живого содержимого: иначе
     каждый тап гасил экран до скелета и он мигал (MA-INV-32). Ключ сбрасывается
     при переходе между вкладками — там скелет уместен. */
  function loadingFor(key) {
    if (state.screenKey === key) return;
    state.screenKey = key;
    loading();
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
    var html = kind === 'rings' ? heroRings(d) : kind === 'kcal' ? heroKcal(d) : heroBars(d);
    /* MA-INV-74: свежий не пришёл — цифры остаются, но подписаны своим временем,
       пока следующая загрузка не пройдёт. Устаревшее не выдаёт себя за текущее. */
    var off = state.offline;
    if (!off || off.day !== d) return html;
    var t = new Date(off.at);
    return html.replace('<section class="card">', '<section class="card">' +
      '<div class="stale-mark" role="status">не обновлено · ' +
      (t.getHours() < 10 ? '0' : '') + t.getHours() + ':' +
      (t.getMinutes() < 10 ? '0' : '') + t.getMinutes() + '</div>');
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
      rows + '</div></div>' + weekStrip(d) + heroFoot(d) + '</section>';
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
      weekLine(d) +
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
      /* FR-47, словарь: «приём» — приём пищи, а не запись. Счёт — групп сервера (MA-INV-79);
         у сохранённого дня старой формы групп нет — тогда записей. */
      : foot('Приёмов', mealCount(d), null, 'right');
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
        weekStrip(d) +
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
    /* Неразрывный пробел: на 320 «≈» уезжало одно на новую строку под имя. */
    if (i.is_estimate) return '\u00a0<span style="color:var(--text-faint)">≈</span>';
    if (i.is_stated) return '\u00a0<span style="color:var(--text-faint)">📋</span>';
    return '';
  }

  function feedOrder() {
    return (state.prefs && state.prefs.feed_order) || 'new_first';
  }

  /* Как записан приём: приписка только там, где заголовок сам об этом не
     говорит. У текста и голоса название и есть сказанное. */
  var INPUT_MARK = { label: 'этикетка', photo: 'фото', menu: 'меню' };

  /* Заголовок строки — то, что записано, а не то, что было сказано (MA-INV-64).
     Фраза ввода не переживает правку веса («паста 10 г» при записанных 15) и
     у фото её нет вовсе — там стояло служебное слово «этикетка». */
  function mealTitle(m) {
    /* FR-39.2: у набора есть имя — оно и заголовок, и строка тоста «Записал …». Перечень позиций
       на 320 съедал его многоточием; состав виден, когда карточку раскрыть. */
    if (m.template_name) return m.template_name;
    var parts = (m.items || []).map(function (i) {
      return i.name + (i.grams > 0 ? ' ' + n(i.grams) + ' г' : '');
    });
    if (!parts.length) return m.raw_input || '';
    return parts.length > 2
      ? parts.slice(0, 2).join(', ') + ' и ещё ' + (parts.length - 2)
      : parts.join(', ');
  }

  /* FR-47, MA-INV-79, MA-INV-81: лента дня блоками. Приём пищи из нескольких записей — группой,
     из одной — обычной строкой. Группы считает сервер; здесь только раскладка по
     `meals[].session` и сверка: группа рисуется, только если под ней ровно её `meal_ids`, подряд —
     иначе сумма в шапке разошлась бы с записями под ней или лента пошла бы не по времени. Не
     сошлось или полей нет (сохранённый день старой формы) — записи строками, как до FR-47.
     `meals` с сервера — по времени. Числа шапки — с сервера, без своего округления (INV-1).
     Чистая функция: ветки проверяет `node web/check.mjs`. */
  function feedModel(d, newFirst) {
    var meals = (d && d.meals) || [], ss = (d && d.sessions) || [];
    var of = {}, at = {};
    meals.forEach(function (m, pos) {
      var s = typeof m.session === 'number' ? ss[m.session] : null;
      if (!s || s.index !== m.session) return;
      (of[m.session] = of[m.session] || []).push(m);
      (at[m.session] = at[m.session] || []).push(pos);
    });
    var out = [], seen = {};
    meals.forEach(function (m) {
      var k = m.session, xs = of[k], s = xs && ss[k], ids = (s && s.meal_ids) || [];
      var whole = !!xs && xs.length > 1 && xs.length === ids.length &&
        at[k][xs.length - 1] - at[k][0] === xs.length - 1 &&
        xs.every(function (x) { return ids.indexOf(x.id) >= 0; });
      if (!whole) return out.push({ meal: m });
      if (seen[k]) return;
      seen[k] = true;
      var c = xs.length, r = c % 10, h = c % 100, one = r === 1 && h !== 11, noWeight = null;
      xs.forEach(function (x) {
        (x.items || []).forEach(function (i) {
          if (!(i.grams > 0) && noWeight === null) noWeight = i.name;
        });
      });
      out.push({
        meals: newFirst ? xs.slice().reverse() : xs.slice(),
        group: {
          key: 'g' + ids[0], ids: ids.slice(), n: c, one: one,
          range: s.start === s.end ? s.start : s.start + '–' + s.end,
          count: c + ' ' + (one ? 'запись'
                           : r >= 2 && r <= 4 && (h < 12 || h > 14) ? 'записи' : 'записей'),
          kcal: s.kcal, p: s.protein_g, f: s.fat_g, c: s.carb_g, approx: !!s.estimated,
          noWeight: noWeight  // FR-39.2: позиция без веса — набором не сохранить
        }
      });
    });
    return newFirst ? out.reverse() : out;
  }

  /* Подвал «Приёмов» (FR-47, словарь): приёмы пищи сервера; у сохранённого дня старой формы
     групп нет — тогда записи. Чистая функция: проверяет `node web/check.mjs`. */
  function mealCount(d) {
    return ((d && (d.sessions || d.meals)) || []).length;
  }

  /* MA-INV-81, §6.28: шапка приёма пищи — сумма читается первой, записи под ней живые, как
     обычные строки (тап — раскрыть, позиция — шторка, «Удалить» с «Вернуть»). Шапка — только
     подпись, не кнопка: действие группы — тихий чип «📌 В набор» последней строкой на рельсе
     (вариант Б, 26.10.2026 — строка действия за тапом по шапке владелец отверг). Тап по кнопке сразу ставит
     на её место форму имени. Порядок записей «сначала новое» кнопку не двигает: она про всю
     группу, а не про одну запись. */
  function sessionBlock(b) {
    var g = b.group;
    var label = 'Приём пищи ' + g.range + ': ' + (g.approx ? 'примерно ' : '') + n(g.kcal) +
      ' ккал, белки ' + n(g.p) + ', жиры ' + n(g.f) + ', углеводы ' + n(g.c) + ' г, ' + g.count;
    /* Подпись группы читает скринридер из aria-label блока — шапку прячем, чтобы не дважды. */
    var head =
      '<div class="sess-h" aria-hidden="true">' +
        '<span class="st">Приём пищи<span class="num rng">' + esc(g.range) + '</span></span>' +
        '<span class="num sk">' + (g.approx ? '<span class="ap">≈</span>' : '') + n(g.kcal) +
        '</span>' +
        '<span class="mic num pfc"><span style="color:var(--prot)">Б ' + n(g.p) + '</span> · ' +
          '<span style="color:var(--fat)">Ж ' + n(g.f) + '</span> · ' +
          '<span style="color:var(--carb)">У ' + n(g.c) + '</span>' +
          '<span class="cnt"> · ' + esc(g.count) + '</span></span>' +
      '</div>';
    var act;
    if (g.noWeight !== null) {
      /* FR-39.2: у набора каждая позиция с весом. «Свои цифры» без веса — объясняем сразу
         и вместо кнопки, а не отказом сервера после ввода имени. */
      act = '<div class="mic sess-a sess-no">Набором не сохранить: у «' + esc(g.noWeight) +
        '» свои цифры без веса.</div>';
    } else if (state.sessNaming === g.key) {
      act = '<div class="naming sess-a">' +
        '<label class="mic" for="sessname">Имя набора · ' + esc(g.count) +
          (g.one ? ' станет' : ' станут') + ' одной</label>' +
        '<div class="naming-row">' +
          '<input id="sessname" type="text" maxlength="64" autocomplete="off" enterkeyhint="done" ' +
            'value="' + esc(state.sessName || '') + '">' +
          '<button class="primary" data-sesssave="' + g.key + '">Готово</button></div>' +
        '<button class="link" data-sesscancel="' + g.key + '" style="align-self:flex-start">Отмена</button>' +
      '</div>';
    } else {
      /* Чип, а не кнопка в рост записи (владелец 26.10: «дюже большая»): видно ~30px, а цель
         пальца — вся строка 44px; видимую пилюлю рисует вложенный span. */
      act = '<div class="sess-a"><button class="sess-chip" data-sessname="' + g.key + '">' +
        '<span>📌 В набор</span></button></div>';
    }
    return '<div class="sess" role="group" aria-label="' + esc(label) + '">' +
      head + '<div class="sess-b">' +
      b.meals.map(function (m) { return mealBlock(m, true); }).join('') + act + '</div></div>';
  }

  /* FR-47: «📌 Сохранить набором» — записи приёма пищи сливаются в одну с именем. Id шлём те,
     что на экране; совпали ли они с приёмом пищи сейчас, решает сервер (MA-INV-79). Слияние
     необратимо одной кнопкой — ключ на дело, двойной тап не сольёт дважды (§7.5). Ответ читаем
     терпимо: запись-набор может прийти новым id, слитые — любым списком `merged_ids`. */
  function saveSession(gkey) {
    var inp = document.getElementById('sessname');
    var nm = ((inp || {}).value || '').trim();
    var b = feedModel(state.lastDay, false).filter(function (x) {
      return x.group && x.group.key === gkey;
    })[0];
    if (!b || state.sending) return;
    if (!nm) { if (inp) inp.focus(); return; }  // пустое «Готово» — вернуть в поле, а не молчать
    state.sessName = nm;
    state.sending = true;
    var btn = document.querySelector('[data-sesssave]');
    if (btn) { btn.disabled = true; btn.textContent = 'Сохраняю…'; }
    busy('Сохраняю набор…');
    var key = 'sess:' + b.group.ids.join(',') + ':' + nm;
    send('/api/sessions/template', {
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': keyFor(key) },
      body: JSON.stringify({ meal_ids: b.group.ids, name: nm })
    }).then(function (r) {
      dropKey(key);
      state.sessNaming = null; state.sessName = '';
      b.group.ids.concat(r.merged_ids || []).forEach(function (id) {
        delete state.open[id];
        if (state.fresh === id) state.fresh = null;
      });
      /* Набор раскрыт: состав, который только что слили, виден сразу — это и подтверждение. */
      if (r.meal) state.open[r.meal.id] = true;
      toast('Набор «' + r.name + '» сохранён: ' + b.group.count +
            (b.group.one ? ' стала' : ' стали') + ' одной. Он в «Продуктах» → «Моё».');
      renderDay();
      loadFrequent();  // чип нового набора — сразу (§6.26)
    }).catch(function (err) {
      if (err.code === 'not_a_session') {
        /* Запись добавили, удалили или поправили с другого устройства — нужен свежий день. Имя
           остаётся в поле, если приём пищи с той же первой записью ещё есть. */
        dropKey(key);
        toast('Приём пищи изменился — проверь и сохрани ещё раз.', true);
        return renderDay();
      }
      if (err.code === 'bad_name') toast('Имя набора — от 1 до 64 знаков.', true);
      else if (err.code === 'bad_set') {
        dropKey(key);
        toast('Набором не сохранить: нужны две позиции и больше, и у каждой — вес.', true);
      } else if (oops(err)) return;  // oops уже освежил день
      /* MA-INV-32: отказ не стирает экран. Из полученного дня, без сети: поле ввода вернулось из
         busy(), поле имени — с набранным именем и в фокусе. */
      repaintDay();
      var again = document.getElementById('sessname');
      if (again) { again.focus(); again.setSelectionRange(again.value.length, again.value.length); }
    }).then(function () { state.sending = false; });
  }

  function feed(d) {
    if (!d.meals.length) {
      return '<section class="card" style="margin-top:var(--s-3)">' +
             '<div class="state">За этот день записей нет.</div></section>';
    }
    /* Новое сверху по умолчанию: только что записанное проверяют сразу,
       а утренний кофе — почти никогда. Порядок переключается тут же и
       запоминается, чтобы не спорить с привычкой. Внутри приёма пищи — тот же порядок. */
    var rows = feedModel(d, feedOrder() === 'new_first').map(function (b) {
      return b.group ? sessionBlock(b) : mealBlock(b.meal);
    }).join('');
    var newFirst = feedOrder() === 'new_first';
    var head = '<div class="feedhead" style="display:flex;align-items:center;margin-bottom:var(--s-2)">' +
      '<span class="cap" style="flex-grow:1">Съедено</span>' +
      '<button class="sortbtn" data-order="' + (newFirst ? 'old_first' : 'new_first') +
        '" aria-label="Порядок записей">' +
        (newFirst ? 'сначала новое' : 'сначала старое') + '</button></div>';
    return '<section class="card" style="margin-top:var(--s-3)">' + head + rows + '</section>';
  }

  /* Одна запись ленты — одинаково и отдельно, и внутри приёма пищи (MA-INV-82). */
  function mealBlock(m, inGroup) {
    var opened = !!state.open[m.id];
    var isFresh = state.fresh === m.id && state.daysAgo === 0;
    /* «≈» вне многоточия: длинное имя режется, метка оценки остаётся (MA-INV-26). */
    var mark = m.estimated ? '<span class="ap">\u00a0≈</span>' : '';
    var head =
      '<button class="meal" data-meal="' + m.id + '" aria-expanded="' + opened + '">' +
        '<span class="num t">' + esc(m.at) + '</span>' +
        '<span class="n"><span class="nt">' + esc(mealTitle(m)) + '</span>' + mark + '</span>' +
        (INPUT_MARK[m.input_type]
          ? '<span class="how">· ' + INPUT_MARK[m.input_type] + '</span>' : '') +
        '<span class="num k">' + n(m.kcal) + '</span>' +
      '</button>';
    if (!opened) return head;
    var items = m.items.map(function (i) {
      /* MA-INV-22, §6.27: правка достижима из ленты всегда — тап по строке открывает
         шторку этой записи в любой день, и у «своих цифр» тоже (без чипов веса). */
      return '<div class="row" data-item="' + i.id + '" data-meal="' + m.id +
             '" style="cursor:pointer">' +
        '<span style="flex-grow:1">' + esc(i.name) +
          itemMark(i) + '</span>' +
        (i.grams > 0 ? '<span class="num g">' + n(i.grams) + ' г</span>'
                     : '<span class="mic g">свои цифры</span>') +
        '<span class="num" style="width:52px;text-align:right">' + n(i.kcal) + '</span>' +
        /* §6.25: с прошлого дня — «ещё раз сегодня». На сегодняшнем дне повтор
           удобнее чипами частого, и кнопка там лишняя. */
        (state.daysAgo > 0
          ? '<button class="again" data-again-m="' + m.id + '" data-again-i="' + i.id + '" ' +
            'aria-label="Записать ' + esc(i.name) + ' в сегодня">↻</button>'
          : '') +
      '</div>';
    }).join('');
    var macros = '<div class="row" style="border-top:1px solid var(--separator);margin-top:4px;padding-top:8px">' +
      '<span style="flex-grow:1">Б ' + n(m.protein_g) + ' · Ж ' + n(m.fat_g) + ' · У ' + n(m.carb_g) + '</span></div>';
    /* FR-39.2: несколько взвешенных позиций — можно сохранить набор. Уже набор — кнопки нет,
       в шапке бейдж с именем. Имя вводится прямо в карточке, без модалки (макет A). */
    var canSet = !m.template_name && m.items.length > 1 &&
                 m.items.every(function (i) { return i.grams > 0; });
    var acts = state.naming === m.id
      ? '<div class="naming">' +
          '<label class="mic" for="setname">Как назвать набор? Дальше это слово запишет тот же ' +
            'состав</label>' +
          '<div class="naming-row">' +
            '<input id="setname" type="text" maxlength="64" autocomplete="off" ' +
              'enterkeyhint="done">' +
            '<button class="primary" data-setsave="' + m.id + '">Готово</button></div>' +
          '<button class="link" data-setcancel="1" style="align-self:flex-start">Отмена</button>' +
        '</div>'
      : '<div class="actions" style="margin-top:var(--s-2)">' +
          '<button data-repeat="' + m.id + '">' +
            (state.daysAgo > 0 ? 'Повторить запись сегодня' : 'Повторить') + '</button>' +
          '<button data-drop="' + m.id + '" class="danger">Удалить</button></div>' +
          /* Отдельной строкой, а не третьей кнопкой в ряду: на 320 в ряду 204px, «Повторить» +
             «Сохранить набор» = 245px — ряд рвался на три строки. Набор сохраняют изредка,
             ему хватает ссылки; «Удалить» остаётся последним. */
          /* В приёме пищи у группы своё «Сохранить набором» (слияние); здесь — только эта
             запись, без слияния (FR-39.2), и подпись это говорит (§6.28). */
          (canSet ? '<button class="link" data-setname="' + m.id + '">' +
            (inGroup ? 'Сохранить запись набором' : 'Сохранить набор') + '</button>' : '');
    /* На 320 подпись «↻ сегодня» у каждой позиции съедала строку — кнопка-иконка,
       а смысл один раз над позициями. */
    var hint = isFresh
      ? '<div class="mic" style="padding:2px 0 4px;color:var(--accent)">только что · ' +
        'тап по строке — поправить</div>'
      : state.daysAgo > 0
        ? '<div class="mic" style="padding:2px 0 4px">↻ — ещё раз сегодня. ' +
          'Тап по позиции — поправить.</div>' : '';
    var body = head + '<div class="items">' + hint + items + macros + acts + '</div>';
    return isFresh ? '<div class="fresh">' + body + '</div>' : body;
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
    if (state.clarify) extra = clarifyCard(state.clarify);
    else if (state.label) extra = labelCard(state.label);
    else if (state.photo) extra = photoCard(state.photo);
    else if (state.scan) extra = scanCard(state.scan);
    else if (state.proposal) extra = proposalCard(state.proposal);
    /* Пока открыта карточка записи, «Добор» молчит: иначе он выталкивал её
       за край экрана (фото 17.09.2026). */
    var pace = d.pace && !extra && !paceMuted(d) ? paceCard(d.pace) : '';
    return hero(d) + composer() + chips() + pace + extra + feed(d);
  }

  function paintDay(d, around) {
    state.lastDay = d;
    state.shownDay = state.daysAgo;
    if (state.offline && state.offline.day !== d) state.offline = null;
    document.getElementById('title').textContent = title(d);
    document.getElementById('nav-day').classList.toggle('hidden', false);
    document.getElementById('fwd').disabled = state.daysAgo === 0;
    /* Поле имени набора в ленте пересоздаётся вместе с ней: фокус возвращаем, иначе
       клавиатура закрывалась посреди ввода. Текст держит state.sessName. */
    var was = document.activeElement && document.activeElement.id;
    if (around) showAround(dayHtml(d)); else show(dayHtml(d));
    var nf = (was === 'sessname' || was === 'setname') && document.getElementById(was);
    if (nf) { nf.focus(); nf.setSelectionRange(nf.value.length, nf.value.length); }
    var inp = document.getElementById('say');
    if (inp && state.keepFocus) { inp.focus(); state.keepFocus = false; }
    if (state.freshScroll) {
      state.freshScroll = false;
      var fr = root.querySelector('.fresh');
      /* По центру, а не «до края»: у нижнего края запись пряталась под тостом и панелью. */
      if (fr && fr.scrollIntoView) fr.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }

  /* Раскрыть приём, открыть правку веса, сменить порядок — это раскладка
     экрана, а не новые цифры. Рисуем из уже полученного дня: запрос ради
     оформления возвращал те же числа и стоил мигания на весь экран. */
  function repaintDay() {
    if (!state.lastDay || state.shownDay !== state.daysAgo) return renderDay();
    show(dayHtml(state.lastDay));
  }

  /* Скелет — только когда показывать нечего: холодный старт или переход на
     другой день. Обновление того же дня идёт поверх живого экрана. Раньше
     любой чип гасил панель, ленту и чипы, высота схлопывалась с ленты до
     одной карточки — экран моргал и прыгал. */
  function renderDay() {
    var onScreen = !!document.getElementById('composer');
    var fresh = state.lastDay && state.shownDay === state.daysAgo;
    if (!onScreen) {
      if (fresh) show(dayHtml(state.lastDay));
      else loadingDay();
    } else if (!fresh) {
      /* Листание дней: прошлый день остаётся под приглушением, пока не придёт
         новый. Скелет схлопывал высоту на каждом шаге — экран мигал. */
      root.classList.add('working');
    }
    var want = state.daysAgo;
    api('/api/day?days_ago=' + want).then(function (d) {
      if (want === 0) saveBoot(d);
      // Быстрое листание: ответ на уже пролистанный день не рисуем.
      if (want === state.daysAgo) paintDay(d, typing());
    }).catch(failure);
  }

  /* ── Продукты ────────────────────────────────────────────────────────── */

  var PAGE = 20;

  /* FR-39.5: «Моё» вторым — рецепт должен находиться сразу после сохранения, даже несъеденный. */
  var SORTS = [['recent', 'Недавние'], ['mine', 'Моё'], ['frequent', 'Частые'], ['name', 'А–Я']];
  var WHY = { curated: 'выверено вручную', label: 'с этикетки',
              manual: 'исправлено тобой', recipe: 'твой рецепт', llm: 'оценка модели' };

  /* Граммовка «по умолчанию» для «+» и шторки: последний раз, иначе привычная. */
  function refGrams(p) { return p.last_grams || p.usual_grams || 100; }

  /* §6.25: «свои цифры» (меню, названные КБЖУ) в каталоге не живут, но в «Недавних» стоят:
     справа ккал порции, «+» повторяет снимок позиции (MA-INV-67). */
  function statedRow(p, sort) {
    var amount = p.grams > 0 ? n(p.grams) + ' г' : 'порция';
    var use = (sort === 'recent' && p.last_time ? p.last_time : times(p.uses)) +
              ' · ' + amount;
    return '<div class="item2">' +
      '<button class="body" data-psheet="' + p.id + '">' +
        '<span class="nm">' + esc(p.name) + '<span class="badge">свои цифры</span></span>' +
        '<span class="mic use">' + esc(use) + '</span></button>' +
      '<span class="num kk">' + n(p.kcal) + '<span class="per"> ккал</span></span>' +
      '<button class="plus" data-again-m="' + p.meal_id + '" data-again-i="' + p.item_id + '" ' +
        'aria-label="Записать ' + esc(p.name) + ' ещё раз сегодня">+</button></div>';
  }

  /* FR-39.5: набор — свой состав. Справа ккал целиком, «+» пишет тот же состав сегодня
     (сервер узнаёт имя набора до похода в модель — бесплатно и мгновенно). */
  function setRow(x) {
    var parts = x.items.map(function (i) { return i.name + ' ' + n(i.grams) + ' г'; }).join(' · ');
    return '<div class="item2">' +
      '<button class="body" data-setsheet="' + esc(x.name) + '">' +
        '<span class="nm">' + esc(x.name) + '</span>' +
        '<span class="mic use">' + esc(parts) + '</span></button>' +
      '<span class="num kk">' + n(x.kcal) + '<span class="per"> ккал</span></span>' +
      '<button class="plus" data-set="' + esc(x.name) + '" ' +
        'aria-label="Записать набор ' + esc(x.name) + '">+</button></div>';
  }

  function statedSheet(p) {
    var g = p.grams > 0 ? p.grams : 0;
    openSheet({
      kind: 'item', src: 'cat', meal: p.meal_id, item: p.item_id, title: p.name,
      sub: 'свои цифры · ' + dayName(p.last_days_ago, p.last_date, false) + ' ' + p.last_time +
           (g ? ' · ' + n(g) + '\u00a0г' : ''),
      ref: g, refLabel: 'как было', fixed: !g,
      per: g ? { kcal: p.kcal / g, p: p.protein_g / g, f: p.fat_g / g, c: p.carb_g / g }
             : { kcal: p.kcal, p: p.protein_g, f: p.fat_g, c: p.carb_g },
      from: 0, to: 0
    });
  }

  function productSheet(p) {
    var last = p.last_days_ago != null
      ? ' · ' + dayName(p.last_days_ago, p.last_date, false) + ' ' + n(p.last_grams) + '\u00a0г' : '';
    openSheet({
      kind: 'product', product: p.id, title: p.name, recipe: p.source === 'recipe',
      sub: n(p.kcal_per_100g) + ' ккал / 100 г · ' + (WHY[p.source] || p.source) + last,
      ref: refGrams(p), refLabel: p.last_grams ? 'как в прошлый раз' : 'обычно',
      per: { kcal: p.kcal_per_100g / 100, p: p.protein_per_100g / 100,
             f: p.fat_per_100g / 100, c: p.carb_per_100g / 100 },
      from: 0, to: 0
    });
  }

  function renderCatalog() {
    var c = state.cat;
    if (c.view === 'product') return renderProduct();
    if (c.view === 'dups') return renderDups();
    var sort = c.sort || 'recent';
    document.getElementById('title').textContent = 'Продукты';
    loadingFor('cat:list:' + sort);
    var list = api('/api/products?limit=' + PAGE + '&offset=' + c.offset + '&sort=' + sort +
        (c.q ? '&q=' + encodeURIComponent(c.q) : ''));
    /* Наборы не листаются страницами (их единицы) и в поиск не попадают — показываем
       на первой странице «Моё» без запроса. */
    var withSets = sort === 'mine' && !c.offset && !c.q;
    (withSets
      ? Promise.all([list, api('/api/sets')]).then(function (r) { r[0].sets = r[1].items; return r[0]; })
      : list)
      .then(function (d) {
        c.items = {};
        var lastDay = null;
        var rows = d.items.map(function (p) {
          c.items[p.id] = p;
          var est = p.source === 'llm'
            ? '<span class="badge">оценка</span>'
            : p.source === 'label' ? '<span class="badge">с этикетки</span>'
            : p.source === 'recipe' && sort !== 'mine' ? '<span class="badge">рецепт</span>' : '';
          var g = refGrams(p);
          /* §6.25, «Недавние»: группы по дню последнего раза. */
          var head = '';
          if (sort === 'recent' && p.last_date !== lastDay) {
            lastDay = p.last_date;
            head = '<div class="dayhead">' + esc(dayName(p.last_days_ago, p.last_date, true)) +
                   '</div>';
          }
          if (p.stated) return head + statedRow(p, sort);
          var use = sort === 'recent' && p.last_time
            ? p.last_time + ' · ' + n(p.last_grams) + ' г'
            : (p.uses ? times(p.uses) + ' · обычно ' + n(p.usual_grams) + ' г'
                      : n(p.usual_grams) + ' г');
          /* MA-INV-24 (пересмотр §6.25): тело строки — шторка порции, «+» — запись
             сразу. Промах по телу больше не пишет приём. */
          return head + '<div class="item2">' +
            '<button class="body" data-psheet="' + p.id + '">' +
              '<span class="nm">' + esc(p.name) + est + '</span>' +
              '<span class="mic use">' + esc(use) + '</span></button>' +
            '<span class="num kk">' + n(p.kcal_per_100g) + '<span class="per">/100 г</span></span>' +
            '<button class="plus" data-quick="' + p.id + '" data-grams="' + g + '" ' +
              'aria-label="Записать ' + esc(p.name) + ' ' + n(g) + ' г">+</button></div>';
        }).join('');
        if (sort === 'mine') {
          c.sets = {};
          (d.sets || []).forEach(function (x) { c.sets[x.name] = x; });
          var sets = (d.sets || []).map(setRow).join('');
          rows = (sets ? '<div class="dayhead">Наборы · ешь целиком</div>' + sets : '') +
                 (rows ? '<div class="dayhead">Рецепты · берёшь по граммам</div>' + rows : '');
        }
        rows = rows || '<div class="state">' +
          (sort === 'mine' && !c.q
            ? 'Своего пока нет. Напиши «рецепт …» с ингредиентами — или сохрани набор из записи.'
            : sort === 'name' || c.q ? 'Ничего не нашлось.' : 'Пока ничего не записано.') + '</div>';
        var hint = d.items.length
          ? '<div class="mic" style="padding:var(--s-2) var(--s-2) 0">' +
            'Тап — выбрать порцию. «+» — записать сразу.</div>'
          : '';
        var sorts = '<div class="chips" role="group" aria-label="Порядок">' +
          SORTS.map(function (x) {
            var on = x[0] === sort;
            return '<button class="chip' + (on ? ' on' : '') + '" data-sort="' + x[0] +
                   '" aria-pressed="' + on + '">' + x[1] + '</button>';
          }).join('') + '</div>';
        var from = d.total ? c.offset + 1 : 0;
        var to = Math.min(c.offset + PAGE, d.total);
        show(
          '<div class="search">' +
            '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" ' +
              'stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/>' +
              '<path d="M20 20l-3.5-3.5" stroke-linecap="round"/></svg>' +
            '<input id="q" type="search" placeholder="Найти продукт" value="' + esc(c.q) + '">' +
          '</div>' + sorts +
          '<section class="card" style="margin-top:var(--s-3)">' + hint +
            '<div class="plist">' + rows + '</div></section>' +
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
    document.getElementById('title').textContent = 'Продукт';
    loadingFor('cat:product:' + state.cat.product);
    /* MA-INV-78: вход «recipe-<id>» открывает состав один раз — флаг снимается до
       ответа, иначе упавшая загрузка оставила бы его до следующей карточки. */
    var withRecipe = state.cat.recipe;
    state.cat.recipe = false;
    /* Ответ опоздал — человек уже на другом экране или продукте: не рисуем ни
       карточку, ни ошибку, ни шторку поверх чужого экрана. */
    var pid = state.cat.product;
    var here = function () {
      return state.tab === 'cat' && state.cat.view === 'product' && state.cat.product === pid;
    };
    api('/api/products/' + pid).then(function (d) {
      if (!here()) return;
      state.cat.card = d;
      state.cat.editing = false;
      state.cat.pick = Math.round(refGrams(d.product));
      paintProduct();
      if (withRecipe && d.product.source === 'recipe') openRecipeSheet(d.product, d.recipe);
    }).catch(function (e) {
      if (!here()) return;
      /* Продукта нет (удалили, слили, устаревший id в адресе — MA-INV-78): вкладка
         «Продукты» дальше открывает список, а не ту же ошибку по кругу. */
      state.cat.view = 'list'; state.cat.product = null;
      failure(e);
    });
  }

  /* §6.25, В1: карточка начинается с записи — пришёл за продуктом, чтобы съесть его ещё
     раз. Правка плотности по кнопке, а не формой с порога. */
  function paintProduct() {
    var d = state.cat.card;
    var p = d.product;
    var c = state.cat;
    document.getElementById('title').textContent = 'Продукт';
    var last = p.last_days_ago != null
      ? ' · последний раз ' + dayName(p.last_days_ago, p.last_date, false) + ', ' +
        n(p.last_grams) + '\u00a0г' : '';
    var g = c.pick;
    var chips = portions(refGrams(p)).map(function (x) {
      var on = x === g;
      return '<button class="chip' + (on ? ' on' : '') + '" data-cg="' + x + '" aria-pressed="' +
             on + '">' + n(x) + ' г</button>';
    }).join('') + '<button class="chip" data-psheet="' + p.id + '">своё…</button>';
    var head = '<section class="card">' +
      '<div style="font-size:var(--f-t2);font-weight:700;line-height:1.25">' + esc(p.name) +
      '</div><div class="mic" style="margin-top:6px">' + esc(WHY[p.source] || p.source) +
        ' · съедено ' + times(p.uses) + esc(last) + '</div>' +
      '<div class="sh-chips" style="margin-top:var(--s-4)">' + chips + '</div>' +
      /* MA-INV-68: ккал на кнопке — предпросмотр, запишет сервер. */
      '<button class="primary" style="margin-top:var(--s-3)" data-quick="' + p.id +
        '" data-grams="' + g + '">Записать сегодня · ' + n(g) + ' г · ' +
        n(p.kcal_per_100g * g / 100) + ' ккал</button></section>';
    var density = c.editing
      ? '<section class="card" style="margin-top:var(--s-3)">' +
          '<input id="pname" aria-label="Название" value="' + esc(p.name) + '" ' +
          'style="width:100%;background:var(--surface-2);border:1px solid var(--surface-3);' +
          'border-radius:var(--r-sm);color:var(--text);font:inherit;font-size:var(--f-body);' +
          'padding:11px;outline:none;box-sizing:border-box">' +
          '<div style="margin-top:var(--s-4)">' +
            field('pk', 'Ккал / 100 г', p.kcal_per_100g) +
            field('pp', 'Белок, г', p.protein_per_100g) +
            field('pf', 'Жир, г', p.fat_per_100g) +
            field('pc', 'Углеводы, г', p.carb_per_100g) +
          '</div>' +
          (p.source === 'recipe'
            ? '<div class="mic" style="margin-top:var(--s-3);line-height:1.5">Это рецепт: ' +
              'следующая правка состава пересчитает плотность из ингредиентов и заменит эти ' +
              'цифры.</div>'
            : '') +
          '<button class="primary" id="save">Сохранить</button>' +
          '<div class="mic" style="margin-top:var(--s-3);line-height:1.5">' +
            'Правка меняет будущие записи. Уже съеденное не двинется — в истории ' +
            'хранится снимок на момент записи.</div></section>'
      : '<section class="card" style="margin-top:var(--s-3)">' +
          '<div style="display:flex;align-items:center;margin-bottom:var(--s-2)">' +
            '<span class="cap" style="flex-grow:1">На 100 г</span>' +
            '<button class="link" data-cedit="1">Править</button></div>' +
          '<div class="dtiles">' +
            tile(p.kcal_per_100g, 'ккал', '') + tile(p.protein_per_100g, 'белок', 'var(--prot)') +
            tile(p.fat_per_100g, 'жир', 'var(--fat)') + tile(p.carb_per_100g, 'углев', 'var(--carb)') +
          '</div></section>';
    show(head + density + recipeCard(d.recipe, p) + recentCard(d.recent_items, p) +
         '<div class="pager"><button id="toList">К списку</button></div>');
  }

  /* FR-39.7: из чего рецепт и кнопка правки. Состава нет — рецепт заводили до 15.10, когда
     состав не хранили; говорим честно и даём собрать заново. */
  function recipeCard(r, p) {
    if (p.source !== 'recipe') return '';
    if (!r) {
      return card('Состав', '<div class="rcard"><div class="mic" style="padding:var(--s-2) 0 var(--s-3);' +
        'line-height:1.5">Состав этого рецепта ' +
        'не сохранился — его заводили до 15.10, тогда состав не хранили. Собери его здесь: дальше ' +
        'его можно будет править, а плотность пересчитается сама.</div>' +
        '<button class="primary" style="margin-top:0" data-recipe="' + p.id + '">' +
          'Собрать состав</button></div>',
        'Съеденное раньше не изменится — в истории снимок на момент записи.');
    }
    var rows = r.items.map(function (i) {
      return '<div class="row"><span style="flex-grow:1">' + esc(i.name) + '</span>' +
        '<span class="num g">' + n(i.grams) + '\u00a0г</span></div>';
    }).join('');
    var weight = 'Всего ' + n(r.total_g) + '\u00a0г' +
      (r.yield_g ? ' · вышло готового ' + n(r.yield_g) + '\u00a0г' : '');
    return card('Состав',
      '<div class="rcard"><div class="items" style="padding-left:0">' + rows + '</div>' +
      '<div class="mic" style="padding:var(--s-2) 0">' + weight + '</div>' +
      '<button class="primary" style="margin-top:var(--s-2)" data-recipe="' + p.id + '">' +
        'Править состав</button></div>',
      'Правка пересчитает плотность рецепта. Съеденное раньше не изменится.');
  }

  function tile(v, label, color) {
    return '<div class="dtile"><div class="num tv"' + (color ? ' style="color:' + color + '"' : '') +
           '>' + n(v, 1) + '</div><div class="mic">' + label + '</div></div>';
  }

  /* §6.8: «где я это ел» — откуда взялась плотность и стоит ли её править. У каждого
     раза «↻ сегодня»: тот же вес по нынешней плотности каталога (§6.25). */
  function recentCard(items, p) {
    if (!items || !items.length) {
      return card('Когда ел', '<div class="state">Этот продукт ещё не встречался в записях.</div>', '');
    }
    var rows = items.map(function (i) {
      return '<div class="row">' +
        '<span style="flex-grow:1">' + esc(i.at) + '</span>' +
        '<span class="num g">' + n(i.grams) + ' г</span>' +
        '<span class="num" style="width:44px;text-align:right">' + n(i.kcal) + '</span>' +
        '<button class="again wide" data-quick="' + p.id + '" data-grams="' + i.grams + '" ' +
          'aria-label="Записать ' + n(i.grams) + ' г в сегодня">↻ сегодня</button>' +
      '</div>';
    }).join('');
    return card('Когда ел', '<div class="items" style="padding-left:0">' + rows + '</div>',
                'Ккал — по снимку на момент записи; «↻ сегодня» запишет тот же вес ' +
                'по нынешней плотности.');
  }

  function field(id, label, value) {
    /* MA-INV-72: плотность хранится точной, а в поле — до 0,1. Сырое «183,496402» из базы
       читалось как сбой. */
    var shown = value == null || value === '' ? '' : Math.round(Number(value) * 10) / 10;
    return '<div class="field"><label for="' + id + '">' + esc(label) + '</label>' +
      '<input id="' + id + '" class="num" type="number" step="0.1" inputmode="decimal" ' +
      'value="' + esc(shown) + '"></div>';
  }

  /* «1 раз», «2 раза», «5 раз», «22 раза», «12 раз». */
  function times(k) {
    var a = k % 10, b = k % 100;
    return n(k) + (a >= 2 && a <= 4 && (b < 12 || b > 14) ? ' раза' : ' раз');
  }

  function renderDups() {
    document.getElementById('title').textContent = 'Дубли';
    loadingFor('cat:dups');
    api('/api/products/duplicates/groups').then(function (d) {
      /* MA-INV-25: человек должен знать заранее, что история не поедет.
         Иначе он ждёт пересчёта съеденного и считает его отсутствие багом. */
      var vow = '<div class="mic" style="padding:0 var(--s-2) var(--s-3);line-height:1.55">' +
        'Слияние меняет только каталог. Съеденное не пересчитается — в записях ' +
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



  /* ── неделя (§6.23) ──────────────────────────────────────────────────── */

  var WDAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
  var MONTHS_GEN = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл',
                    'авг', 'сен', 'окт', 'ноя', 'дек'];
  var CHEV_L = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M15 18l-6-6 6-6"/></svg>';
  var CHEV_R = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M9 18l6-6-6-6"/></svg>';

  /* Плашку показываем, когда есть о чём говорить: не в понедельник (неделя и
     день совпадают), не на прошлых днях и не без единого прошедшего записанного
     дня — иначе это дубль дневного остатка. */
  function weekShown(d) {
    var w = d.week_balance;
    if (!w || !w.goal_kcal || w.verdict === 'empty') return null;
    if (w.days[0].date === d.day) return null;
    var before = w.days.some(function (x) { return x.kcal != null && x.date < d.day; });
    return before ? w : null;
  }

  function weekToday(w, day) {
    for (var i = 0; i < w.days.length; i++) if (w.days[i].date === day) return w.days[i];
    return null;
  }

  function weekRight(w, day) {
    var t = weekToday(w, day);
    var todayIn = t && !t.over;            // сегодня ещё не в балансе — оно «оставшееся»
    if (w.floor_hit) return null;
    if (w.per_day_kcal == null) {
      return '<span class="badge ' + (w.verdict === 'over' ? 'warn' : 'ok') + '">' +
        (w.verdict === 'over' ? 'перебор' : 'в плане') + '</span>';
    }
    if (w.remaining_days === 1) {
      return '<div class="mic">' + (todayIn ? 'на сегодня' : 'на завтра') + '</div>' +
        '<div class="num" style="font-size:var(--f-body-s);font-weight:600">' +
        n(w.per_day_kcal) + '</div>';
    }
    return '<div class="mic">до воскресенья</div>' +
      '<div class="num" style="font-size:var(--f-body-s);font-weight:600">' +
      (todayIn && !(t && t.kcal) ? 'с сегодня по ' : 'по ') + n(w.per_day_kcal) + '</div>' +
      '<div class="mic">в день</div>';
  }

  function weekStrip(d) {
    var w = weekShown(d);
    if (!w) return '';
    var over = w.balance_kcal > 0;
    if (w.verdict === 'few_logs') {
      return '<div class="wk muted"><div class="l"><div class="mic">Неделя</div>' +
        '<div class="cap">Записано ' + n(w.logged_days) + ' дн. — для баланса маловато</div></div>' +
        '<div class="r"><span class="badge">мало записей</span></div></div>';
    }
    var right = weekRight(w, d.day);
    return '<div class="wk">' +
      '<div class="l"><div class="mic" style="color:var(--text)">' +
        (over ? 'Перебор за неделю' : 'Запас на неделе') + '</div>' +
        '<div class="num v"' + (over ? ' style="color:var(--warn)"' : '') + '>' +
        n(Math.abs(w.balance_kcal)) + ' <span class="unit">ккал</span></div></div>' +
      (right ? '<div class="r">' + right + '</div>'
             : '<div class="mic full">Неделю уже не догнать — ешь по обычной цели, ' +
               'с понедельника новая.</div>') +
      '</div>';
  }

  function weekLine(d) {
    var w = weekShown(d);
    if (!w || w.verdict === 'few_logs') return '';
    var over = w.balance_kcal > 0;
    return '<div class="cap" style="margin-top:6px">Неделя: ' +
      (over ? 'перебор ' : 'запас ') + n(Math.abs(w.balance_kcal)) +
      (w.per_day_kcal != null ? ' · до вс по ' + n(w.per_day_kcal) : '') + '</div>';
  }

  function weekTitle(w, ago) {
    var a = w.start.split('-'), b = w.end.split('-');
    var dates = Number(a[2]) + (a[1] !== b[1] ? ' ' + MONTHS_GEN[Number(a[1]) - 1] : '') +
      '–' + Number(b[2]) + ' ' + MONTHS_GEN[Number(b[1]) - 1];
    if (ago === 0) return 'Эта неделя · ' + dates;
    if (ago === 1) return 'Прошлая неделя · ' + dates;
    return dates;
  }

  /* Столбики в одном масштабе с пунктиром цели; красным — только часть выше
     цели (MA-INV-27). Подписи дней — HTML под SVG: текст внутри растянутого
     SVG на 320 px сжимался бы до нечитаемого. */
  function weekBars(w, today) {
    var W = 300, H = 120, base = 112, slot = W / 7, bw = 26;
    var top = Math.max(w.goal_kcal * 1.25, 1);
    w.days.forEach(function (x) { if (x.kcal != null) top = Math.max(top, x.kcal * 1.08); });
    var y = function (v) { return base - v / top * 104; };
    var words = [];
    var bars = w.days.map(function (x, i) {
      var cx = i * slot + slot / 2, x0 = cx - bw / 2;
      if (x.kcal == null) {
        words.push(WDAYS[i] + (x.date > today ? ' впереди' : ' не записан'));
        if (x.date > today) return '';
        return '<rect x="' + (cx - 7) + '" y="' + (base - 3) + '" width="14" height="2" rx="1" ' +
          'fill="var(--text-faint)"/>';
      }
      var goal = w.goal_kcal, v = x.kcal, low = Math.min(v, goal);
      words.push(WDAYS[i] + ' ' + n(v) + (v > goal ? ' — перебор ' + n(v - goal) : ''));
      var out = '<rect x="' + x0 + '" y="' + y(low) + '" width="' + bw + '" height="' +
        (base - y(low)) + '" rx="4" fill="var(--bar)"/>';
      if (v > goal) {
        out += '<rect x="' + x0 + '" y="' + y(v) + '" width="' + bw + '" height="' +
          (y(goal) - y(v)) + '" rx="4" fill="var(--danger)"/>' +
          '<rect x="' + x0 + '" y="' + (y(goal) - 0.5) + '" width="' + bw +
          '" height="1" fill="var(--surface)"/>';
      }
      return out;
    }).join('');
    var labels = w.days.map(function (x, i) {
      var cls = x.date === today ? ' class="now"' : (x.date > today ? ' class="later"' : '');
      return '<span' + cls + '>' + WDAYS[i] + '</span>';
    }).join('');
    return '<div class="wkchart">' +
      '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' +
        esc(words.join(', ')) + '">' +
        '<line x1="0" x2="' + W + '" y1="' + y(w.goal_kcal) + '" y2="' + y(w.goal_kcal) +
          '" stroke="var(--line)" stroke-width="1" stroke-dasharray="3 4" ' +
          'vector-effect="non-scaling-stroke"/>' + bars + '</svg>' +
      '<div class="wkdays">' + labels + '</div></div>' +
      '<div class="legend"><span><i style="background:var(--line)"></i>цель ' +
        n(w.goal_kcal) + ' в день</span>' +
        '<span><i style="background:var(--danger)"></i>перебор</span></div>';
  }

  function weekCard(w, ago) {
    /* «Сегодня» — от сервера: он знает границу суток и часовой пояс, а день
       экрана «День» может ещё не прийти, если «Итоги» открыли сразу. */
    var today = w.today;
    var head = '<div class="wkhead">' +
      '<button class="iconbtn" data-wk="1" aria-label="Предыдущая неделя"' +
        (ago >= 4 ? ' disabled' : '') + '>' + CHEV_L + '</button>' +
      '<div class="cap" aria-live="polite">' + esc(weekTitle(w, ago)) + '</div>' +
      '<button class="iconbtn" data-wk="-1" aria-label="Следующая неделя"' +
        (ago <= 0 ? ' disabled' : '') + '>' + CHEV_R + '</button>' +
      '</div>';
    var badge = w.verdict === 'on_track' ? '<span class="badge ok">в плане</span>'
      : w.verdict === 'over' ? '<span class="badge warn">перебор</span>'
      : w.verdict === 'few_logs' ? '<span class="badge">мало записей</span>' : '';
    if (w.verdict === 'empty') {
      return '<section class="card" id="weekcard">' + head +
        '<div class="state">За эту неделю записей нет.</div></section>';
    }
    var over = w.balance_kcal > 0;
    /* «из M прошедших» — с регистрации, числом сервера: бот пишет то же (AC-230). */
    var past = w.past_days;
    var counted = w.counted_days;
    return '<section class="card" id="weekcard">' + head +
      '<div style="display:flex;align-items:baseline;gap:var(--s-2);margin-top:var(--s-2);' +
        'max-width:420px">' +
        '<span class="num" style="font-size:var(--f-t1);font-weight:700' +
          (over ? ';color:var(--warn)' : '') + '">' + n(Math.abs(w.balance_kcal)) + '</span>' +
        '<span class="cap">' + (over ? 'ккал перебор' : 'ккал в запасе') + '</span>' +
        '<span style="margin-left:auto">' + badge + '</span></div>' +
      '<div class="mic" style="margin-top:2px">' + (ago > 0
        ? 'записано ' + n(w.logged_days) + ' дн. из ' + n(past)
        : 'записано ' + n(w.past_logged_days) + ' дн. из ' + n(past) + ' прошедших') + '</div>' +
      '<div class="chart">' + weekBars(w, ago > 0 ? '9999' : today) + '</div>' +
      (counted ? '<div class="rows">' +
        '<div class="field"><label>Цель за ' + n(counted) + ' дн.</label>' +
          '<span class="num">' + n(w.week_goal_kcal) + '</span></div>' +
        '<div class="field"><label>Съедено</label><span class="num">' +
          n(w.consumed_kcal) + '</span></div>' +
        '<div class="field"><label>В среднем за день</label><span class="num">' +
          n(w.average_kcal) + '</span></div>' +
      '</div>' : '') +
      '<div class="mic note">Пустые дни не считаются ни нулём, ни целью. ' +
        'Сегодня входит в баланс, только когда цель уже превышена.</div>' +
      '</section>';
  }

  function loadWeek() {
    var ago = state.weekAgo || 0;
    api('/api/week?weeks_ago=' + ago).then(function (r) {
      var el = document.getElementById('weekcard');
      if (el && r.week) el.outerHTML = weekCard(r.week, ago);
    }).catch(function (e) { toast(e.message, true); });
  }

  /* ── Итоги: графики инлайн-SVG, без библиотек (§5.7) ─────────────────── */

  var PERIODS = [[7, 'Неделя'], [30, '30 дней'], [90, '3 месяца']];

  function renderTotals() {
    document.getElementById('title').textContent = 'Итоги';
    loadingFor('tot');
    var days = activePeriod();
    var nothing = function () { return null; };
    Promise.all([
      api('/api/stats/series?days=' + days),
      api('/api/stats/weight?limit=60').catch(function () { return { points: [] }; }),
      api('/api/stats/reconcile?days=' + Math.max(days, 14)).catch(nothing),
      api('/api/stats/contributions?days=' + days).catch(nothing),
      api('/api/stats/contributions?by=protein&days=' + days).catch(nothing),
      api('/api/stats/accuracy?days=' + days).catch(nothing),
      api('/api/week?weeks_ago=' + (state.weekAgo || 0)).catch(nothing),
      api('/api/body-scans').catch(nothing)
    ]).then(function (res) {
      /* Опоздавший ответ — с вкладки ушли или период уже другой (чип, настройки):
         рисовать его значит показать не тот период поверх верного. */
      if (state.tab !== 'tot' || activePeriod() !== days) return;
      var land = state.land;   /* MA-INV-78: прокрутка к карточке — только при входе */
      state.land = null;
      var d = res[0], w = res[1], rec = res[2], con = res[3], pro = res[4], acc = res[5];
      var wk = res[6] && res[6].week ? weekCard(res[6].week, state.weekAgo || 0) : '';
      var comp = compCard(res[7]);
      /* Без единой записи графики рисуют пустые сетки и «среднее 0 при цели
         170» — шесть коробок, которые выглядят поломкой, а не отсутствием
         данных. Одна честная карточка понятнее. */
      if (!d.days.some(function (x) { return x.meals > 0; })) {
        show(wk + comp + periods() +
          card('За этот период записей нет',
               '<div class="state">Выбери период побольше или запиши, что съел, — ' +
               'графики появятся сами.</div>', ''));
        return landAt(land);
      }
      show(wk + periods() +
           reconcileCard(rec) +
           comp +
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
      landAt(land);
    }).catch(function (e) {
      if (state.tab === 'tot' && activePeriod() === days) failure(e);
    });
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

  /* Карточка, к которой прокрутить «Итоги» при входе (MA-INV-78). Нет её — верх. */
  function landAt(cls) {
    var el = cls && root.querySelector('.' + cls);
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'start' });
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
    loadingFor('set');
    Promise.all([
      api('/api/settings'),
      api('/api/body-scans').catch(function () { return null; })
    ]).then(function (res) {
      var d = res[0], o = res[1];
      state.settings = d;
      state.tz = d.timezone;
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

        group('Граница суток',
          [[0, 'В полночь'], [3, 'В 3:00'], [4, 'В 4:00'], [5, 'В 5:00'], [6, 'В 6:00']]
            .map(function (o) {
              var on = (p.day_start || 0) === o[0];
              return '<div class="pick' + (on ? ' on' : '') + '"' + pk(on) +
                ' data-daystart="' + o[0] + '"><span class="dot"></span>' +
                '<span style="flex-grow:1;font-size:var(--f-body-s)">' + esc(o[1]) +
                '</span></div>';
            }).join(''),
          'С какого часа начинается новый день. Перекус после полуночи засчитается во вчера.') +

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

        group('Цель по проценту жира',
          [[null, 'Не считать'], [20, '20%'], [18, '18%'], [15, '15%'], [12, '12%'],
           [10, '10%']].map(function (o2) {
            var on = (p.target_fat_pct == null ? null : Number(p.target_fat_pct)) === o2[0];
            return '<div class="pick' + (on ? ' on' : '') + '"' + pk(on) +
              ' data-fatgoal="' + (o2[0] == null ? '' : o2[0]) + '"><span class="dot"></span>' +
              '<span style="flex-grow:1;font-size:var(--f-body-s)">' + esc(o2[1]) +
              '</span></div>';
          }).join(''),
          'Нужна только для срока в «Итогах»: на калории, белок и расход не влияет. ' +
          'Процент — в единицах InBody, он ниже, чем показал бы другой метод.') +

        bodyGroups(p, g, o) +

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
          o && o.proposal
            ? 'Есть свежий замер InBody — белок точнее считать от безжировой массы: ' +
              'кнопка «Пересчитать цели» выше.'
            : 'Нужен хотя бы один замер веса — иначе умножать не на что.') +

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
          'Вместе с текстом уйдут последние записи — без них «посчитало не так» ' +
          'не воспроизвести.') +

        group('Ассистент',
          row('Модель', d.coach.model) +
          /* id — чтобы смена лимита обновила строку на месте, без перерисовки экрана. */
          '<div class="field"><label>Потрачено за месяц</label>' +
            '<span class="num" id="coach-spent" style="font-size:var(--f-body-s);' +
            'color:var(--text-muted)">' + esc(spentLine(d.coach.spent_usd, d.coach.limit_usd)) +
            '</span></div>',
          d.coach.available
            ? 'Лимит жёсткий: при исчерпании ассистент отвечает отказом, а не молча хуже.'
            : 'Ключ не настроен — вкладка AI недоступна.') +

        limitGroup(d) +

        '<div class="pager"><button id="toDay">Назад</button></div>');
    }).catch(failure);
  }


  /* Настройки по замеру (§6.24): обмен, активность, белок, пересчёт. */
  function bodyGroups(p, g, o) {
    if (!o || !o.scans || !o.scans.length) {
      return group('Состав тела',
        '<div class="state" style="text-align:left;padding:var(--s-2) 0">Сфотографируй бланк ' +
        'InBody камерой на экране «День» — появятся обмен и белок по составу тела.</div>', '');
    }
    var src = p.bmr_source || 'inbody';
    var fresh = o.stale_days != null && o.stale_days <= 180;
    var tile = function (key, title, v, sub, disabled) {
      var on = src === key && !disabled;
      return '<div class="pick tile' + (on ? ' on' : '') + '"' + pk(on) +
        (disabled ? ' aria-disabled="true"' : ' data-bmr="' + key + '"') + '>' +
        '<div class="tile-h"><span class="dot"></span><span class="mic">' + title + '</span></div>' +
        '<div class="num v">' + (v != null ? n(v) : '—') + '</div>' +
        '<div class="mic">' + sub + '</div></div>';
    };
    var bmr = src === 'formula' && o.formula_bmr != null ? o.formula_bmr : o.inbody_bmr;
    var act = p.activity || 'medium';
    var factor = { low: 1.2, medium: 1.375, high: 1.55 }[act];
    var prop = o.proposal;
    var acts = [
      ['low', 'Мало движения', 'сидячая работа, почти без тренировок'],
      ['medium', 'Средне', '3–4 тренировки в неделю'],
      ['high', 'Много', 'почти каждый день или физическая работа']
    ];
    return group('Базовый обмен',
        '<div class="tiles" role="radiogroup" aria-label="Базовый обмен">' +
          tile('inbody', 'По InBody', o.inbody_bmr, fresh ? 'из безжировой массы'
               : 'замер старше полугода', !fresh) +
          tile('formula', 'По формуле', o.formula_bmr, 'рост, вес, возраст',
               o.formula_bmr == null) +
        '</div>',
        'Обмен по InBody точнее при нетипичном составе тела: он не опирается на ' +
        'средний состав по весу.') +
      group('Активность',
        acts.map(function (a) {
          var on = act === a[0];
          return '<div class="pick' + (on ? ' on' : '') + '"' + pk(on) +
            ' data-activity="' + a[0] + '"><span class="dot"></span>' +
            '<span style="flex-grow:1;font-size:var(--f-body-s)">' + esc(a[1]) +
            ' <span class="mic">· ' + esc(a[2]) + '</span></span></div>';
        }).join(''),
        n(bmr) + ' × ' + n(factor, 3) + ' = расход ≈ ' + n(Math.round(bmr * factor)) +
        ' ккал в день.') +
      (prop
        ? group('Белок',
            '<div class="field"><label>По безжировой массе</label><span class="num" ' +
              'style="color:var(--prot)">' + n(prop.proposed.protein_g) + ' г</span></div>' +
            '<div class="field"><label>Сейчас в целях</label><span class="num">' +
              n(g.protein_g) + ' г</span></div>' +
            '<button class="primary" data-scanprop="1">Пересчитать цели</button>' +
            '<div id="propslot"></div>',
            'Жиру белок не нужен — считаем от ' + n(prop.fat_free_kg, 1) +
            ' кг безжировой массы, но не меньше 1,6 г на кг веса.')
        : '');
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


  /* Правка целей формой (MA-INV-54): сервер решает, кто берёт разницу, а экран
     говорит словами, что сдвинулось — иначе «углеводы +100» выглядело бы сбоем. */
  var MOVED_WORD = { kcal: ['калории', ''], protein_g: ['белок', ' г'],
                     fat_g: ['жир', ' г'], carb_g: ['углеводы', ' г'] };

  function saveGoal() {
    var body = {
      kcal: Number(document.getElementById('gk').value),
      protein_g: Number(document.getElementById('gp').value),
      fat_g: Number(document.getElementById('gf').value),
      carb_g: Number(document.getElementById('gc').value)
    };
    send('/api/settings/goal', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (r.changed === 'none') { toast('Цели не менялись.'); return; }
      var parts = Object.keys(r.moved || {}).map(function (k) {
        var w = MOVED_WORD[k];
        return w[0] + ' ' + signed(r.moved[k]) + w[1];
      });
      var kept = r.moved && !r.moved.kcal ? ', калории те же' : '';
      toast('Цели сохранены: ' + parts.join(', ') + kept + '.' +
            (r.low_protein ? ' Белка меньше 1,2 г на кг — на сушке это потеря мышц.' : ''),
            !!r.low_protein);
      renderSettings();
    }).catch(function (e) { toast(e.message, true); });
  }

  var MONTHS_IN = ['январе', 'феврале', 'марте', 'апреле', 'мае', 'июне', 'июле',
                   'августе', 'сентябре', 'октябре', 'ноябре', 'декабре'];

  /* Лимит на ИИ (§6.5, MA-INV-65). Сервер отдаёт блок `ai_limit` только владельцу — у
     остальных группы нет вовсе. Права проверяет сервер: скрытая кнопка защитой не
     считается. Кнопки — тем же способом, что цель по жиру: отметка на месте, без
     перерисовки экрана (MA-INV-32). */
  function limitGroup(d) {
    var lim = d.ai_limit;
    if (!lim) return '';
    var cur = Number(lim.usd);
    return group('Лимит на ИИ',
      lim.options.map(function (v) {
        var on = v === cur;
        return '<div class="pick' + (on ? ' on' : '') + '"' + pk(on) +
          ' data-ailimit="' + v + '"><span class="dot"></span>' +
          '<span style="flex-grow:1;font-size:var(--f-body-s)">$' + v + ' в месяц</span></div>';
      }).join(''),
      limitNote(d.coach.spent_usd, cur));
  }

  function spentLine(spent, limit) {
    return '$' + n(spent, 2) + ' из $' + n(limit, 2);
  }

  function limitNote(spent, limit) {
    return 'Потрачено ' + spentLine(spent, limit) + ' в ' + MONTHS_IN[new Date().getMonth()] +
      '. Новый лимит действует сразу. При исчерпании отказывают только ассистент и фото; ' +
      'запись текстом и голосом — никогда.';
  }

  /* Отметка переключается сразу, а тексты с цифрами — по ответу сервера: показывать
     новый потолок, который не сохранился, хуже, чем подождать полсекунды. Отказ —
     отметка возвращается туда, где была. */
  function saveLimit(pick) {
    var picks = root.querySelectorAll('.pick[data-ailimit]');
    var before = root.querySelector('.pick[data-ailimit].on');
    var mark = function (el) {
      [].forEach.call(picks, function (x) {
        var on = x === el;
        x.classList.toggle('on', on);
        x.setAttribute('aria-checked', on ? 'true' : 'false');
      });
    };
    mark(pick);
    /* Номер запроса: два быстрых нажатия — два ответа, и старший не должен затереть
       тексты младшего, когда придёт вторым. */
    var seq = state.limitSeq = (state.limitSeq || 0) + 1;
    return send('/api/settings/ai-limit', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ usd: Number(pick.dataset.ailimit) })
    }).then(function (r) {
      if (seq !== state.limitSeq) return;
      if (state.settings && state.settings.coach) state.settings.coach.limit_usd = r.limit_usd;
      var spent = document.getElementById('coach-spent');
      if (spent) spent.textContent = spentLine(r.spent_usd, r.limit_usd);
      var note = pick.closest('section') && pick.closest('section').nextElementSibling;
      if (note) note.textContent = limitNote(r.spent_usd, r.limit_usd);
      state.budget = null;       // полоска бюджета во вкладке AI перечитает потолок
      toast('Лимит: $' + n(r.limit_usd) + ' в месяц.');
    }).catch(function (e) {
      if (seq === state.limitSeq && before) mark(before);
      toast(e.message, true);
    });
  }

  function patch(path, body, ok, quiet) {
    /* Поздний ответ стартовых настроек не должен затереть то, что человек
       поменял, пока он шёл (MA-INV-74). */
    state.settingsEdited = true;
    return send(path, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (d) {
      // Настройка обязана подействовать сразу, а не после перезахода:
      // иначе переключатель выглядит сломанным (MA-INV-31).
      if (d && d.prefs) state.prefs = d.prefs;
      if (state.settings && d && d.prefs) state.settings.prefs = d.prefs;
      if (d && d.timezone) state.tz = d.timezone;  // по нему сверяется сохранённый день
      toast(ok);
      // `quiet` — настройка, которая ничего не меняет на этом экране: отметку
      // мы уже переключили на месте, а перерисовка тут была бы тем самым
      // миганием, из-за которого настройки и правили.
      if (!quiet) renderSettings();
    }).catch(function (e) { toast(e.message, true); });
  }

  /* ── AI: вопросы по своим данным (§8.7) ──────────────────────────────── */

  var SUGGESTS = [
    'Почему вес растёт, если по дневнику дефицит?',
    'Чем добрать белок к вечеру?',
    'Что изменилось за последний месяц?'
  ];

  function bubbleInner(m) {
    var busy = m.busy
      ? '<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>' +
        '<span class="mic" style="margin-left:6px">' + esc(m.busy) + '</span>'
      : '';
    /* §6.21.2: из разметки модели рисуем только **жирный**. Замена идёт после esc —
       в тексте уже нет ни одного «<», так что HTML от модели сюда не попадёт. */
    return esc(m.text).replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>') + busy;
  }

  /* §6.21.2: последние реплики открытого разговора — чтобы «разложи» было про прошлый
     ответ. Пузыри с ошибкой и недописанные не шлём: это не слова ассистента. Сервер
     всё равно обрежет и проверит (MA-INV-66). */
  function chatHistory() {
    return (state.chat || []).filter(function (m) {
      return !m.busy && !m.err && m.text;
    }).slice(-6).map(function (m) {
      return { role: m.me ? 'user' : 'assistant', text: m.text };
    });
  }

  /* Поток ответа правит только свой пузырь: полная перерисовка на каждом
     куске пересоздавала поле ввода — набранный текст пропадал, фокус слетал. */
  function paintBubble(m) {
    var i = (state.chat || []).indexOf(m);
    var el = state.tab === 'ai' && root.querySelector('.bubble[data-i="' + i + '"]');
    if (!el) return renderAi();
    el.innerHTML = bubbleInner(m);
    if (m.busy) el.setAttribute('aria-busy', 'true'); else el.removeAttribute('aria-busy');
  }

  function renderAi() {
    document.getElementById('title').textContent = 'AI';
    var input = document.getElementById('ask');
    var typed = input ? input.value : '';
    var focused = input && document.activeElement === input;
    var chat = (state.chat || []).map(function (m, i) {
      /* §4.3: совет, который нельзя выполнить в один тап, остаётся советом.
         Под ответом — кнопки на продукты, которые человек уже ел: у них есть
         и плотность, и своя граммовка, значит запись без модели. */
      var acts = (m.actions || []).map(function (a) {
        return '<button class="chip" data-quick="' + a.product_id +
          '" data-grams="' + a.grams + '">Записать ' + esc(a.name) +
          ' <span class="g">· ' + n(a.grams) + ' г</span></button>';
      }).join('');
      return '<div class="bubble ' + (m.me ? 'me' : 'ai') + '" data-i="' + i + '"' +
          (m.busy ? ' aria-busy="true"' : '') + '>' +
          bubbleInner(m) + '</div>' +
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
             'Ассистент смотрит твои записи сам: съеденное, продукты, вес. Он не врач и не ставит диагнозов.' +
           '</div>' + sug + '</section>' +
         chat +
         '<div class="composer ask"><input id="ask" type="text" ' +
           'placeholder="Спроси про свои цифры">' +
           '<button id="send" aria-label="Отправить">' +
           '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
           'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
           '<path d="M5 12h13M13 6l6 6-6 6"/></svg></button></div>');
    var again = document.getElementById('ask');
    if (again && typed) again.value = typed;
    if (again && focused) again.focus();
    if (!state.budget) {
      api('/api/coach/budget').then(function (d) { state.budget = d; renderAi(); })
        .catch(function () {});
    }
  }

  /* §6.21.1: индикатор — это сам поток. Пока ассистент ходит за данными,
     говорим словами, КУДА он полез: «жди» не сообщает ничего, а это сообщает. */
  var TOOL_SAYS = {
    day_totals: 'смотрю дни',
    meals_on: 'смотрю записи',
    top_products: 'смотрю продукты',
    weight_log: 'смотрю вес'
  };

  function askCoach(text) {
    if (!text || state.asking) return;
    state.asking = true;
    var history = chatHistory();
    state.chat = (state.chat || []).concat([{ me: true, text: text }]);
    /* Пузырь ответа заводим сразу пустым: в него потечёт текст, и он же
       держит строку состояния. Иначе экран стоит пустым до первого куска. */
    var bubble = { me: false, text: '', busy: 'думаю', actions: [] };
    state.chat.push(bubble);
    renderAi();

    var done = function (err) {
      bubble.busy = null;
      if (err) { bubble.text = bubble.text || err; bubble.err = true; }
      state.asking = false;
      renderAi();
    };

    streamCoach(text, history, bubble, function () { paintBubble(bubble); })
      .then(function (ok) {
        if (ok) return done(null);
        /* Поток не сложился — повторяем обычным запросом. Ассистент, который
           на старом клиенте показывает пустоту, хуже медленного (§6.21.1). */
        bubble.text = '';
        bubble.busy = 'думаю';
        renderAi();
        return send('/api/coach', {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question: text, history: history })
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
  function streamCoach(question, history, bubble, onTick) {
    if (!window.fetch || !window.TextDecoder) return Promise.resolve(false);
    return fetch('/api/coach/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Init-Data': tg.initData
      },
      body: JSON.stringify({ question: question, history: history })
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
            else if (ev.kind === 'error') {
              bubble.text = ev.message; bubble.err = true; finished = true;
            }
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

  /* Обновить текущую вкладку, не считая это переходом: без скелета. */
  function rerender() {
    if (state.tab === 'cat') return renderCatalog();
    if (state.tab === 'ai') return renderAi();
    return render();
  }

  function render() {
    state.screenKey = null;
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
      '<button id="cam" class="ghost" aria-label="Сфотографировать блюдо или этикетку">' +
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
    var xs = state.frequent || [], ss = state.sets || [];
    /* Один рейтинг (FR-40, §6.26): набор встаёт рядом с продуктами по числу раз за 30 дней, а
       не первым; несъеденный за окно — только в «Моё». sort устойчивый: ничья — продукт выше. */
    var all = xs.map(function (i) {
      /* Есть product_id — пишем мгновенно, без модели. Нет (продукт не
         запиннен) — падаем на обычный путь через текст. */
      var attr = i.product_id
        ? 'data-quick="' + i.product_id + '" data-grams="' + i.grams + '"'
        : 'data-say="' + esc(i.name + ' ' + n(i.grams) + ' г') + '"';
      return { t: i.times || 0, html: '<button class="chip" ' + attr + '>' +
        esc(i.name) + ' <span class="g">· ' + n(i.grams) + ' г</span></button>' };
    }).concat(ss.filter(function (x) { return x.uses_30d > 0; }).map(function (x) {
      /* FR-39.2, макет A: тап по набору пишет состав целиком, без модели. */
      return { t: x.uses_30d, html: '<button class="chip" data-set="' + esc(x.name) + '">' +
        esc(x.name) + ' <span class="g">· ' + n(x.kcal) + ' ккал</span></button>' };
    }));
    if (!all.length) return '';
    all.sort(function (a, b) { return b.t - a.t; });
    return '<div class="chips">' + all.map(function (c) { return c.html; }).join('') + '</div>';
  }

  function loadFrequent() {
    /* Тихо: пустые чипы — не ошибка, ради них не стоит ломать экран. */
    Promise.all([
      api('/api/frequent?limit=10'),
      api('/api/sets').catch(function () { return { items: [] }; })
    ]).then(function (r) {
      state.frequent = r[0].items;
      state.sets = r[1].items;
      /* Чипы — в сохранённый старт: со следующего открытия строка на месте с
         первого кадра, лента не съезжает вниз. */
      if (state.lastDay && state.lastDay.days_ago === 0 && state.lastDay !== state.staleDay &&
          !state.offline) saveBoot(state.lastDay);
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
      '<div class="mic" style="margin-top:6px;line-height:1.5">' + when + '</div>' +
      '<div class="chips">' + chips +
        '<button class="chip off" data-pace-off="1">Ничего не ел</button></div>' +
      '</section>';
  }


  /* ── состав тела (§6.24) ─────────────────────────────────────────────── */

  var TARGET_WORD = { cut: 'похудение', maintain: 'поддержание', bulk: 'набор' };
  var ACTIVITY_WORD = { low: 'мало движения', medium: 'средняя', high: 'много' };

  function ddmm(iso) {
    var a = String(iso).split('-');
    return a[2] + '.' + a[1] + '.' + a[0];
  }

  function signed(v, digits) {
    if (v == null) return '—';
    return (v > 0 ? '+' : v < 0 ? '−' : '') + n(Math.abs(v), digits);
  }

  /* Ось — изменение от первого замера, а не абсолютные кг: на общей шкале
     15–45 кг сдвиг на килограмм даёт плоские линии, а две оси нечестны.
     Серая полоса ±1 кг — погрешность прибора между днями. */
  function compChart(scans) {
    var W = 300, H = 120, pad = 14;
    var first = scans[0];
    if (scans.length === 1) {
      return '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Один замер: жир ' +
        n(first.body_fat_kg, 1) + ' кг, мышцы ' + n(first.skeletal_muscle_kg, 1) + ' кг">' +
        '<line x1="40" x2="260" y1="40" y2="40" stroke="var(--fat)" stroke-width="2" stroke-dasharray="5 5"/>' +
        '<line x1="40" x2="260" y1="80" y2="80" stroke="var(--data)" stroke-width="2" stroke-dasharray="5 5"/>' +
        '<circle cx="40" cy="40" r="5" fill="var(--fat)"/>' +
        '<circle cx="260" cy="40" r="5" fill="var(--surface)" stroke="var(--fat)" stroke-width="1.5"/>' +
        '<rect x="35" y="75" width="10" height="10" fill="var(--data)"/>' +
        '<rect x="255" y="75" width="10" height="10" fill="var(--surface)" stroke="var(--data)" stroke-width="1.5"/>' +
        '</svg>' +
        '<div class="comprow"><span>' + ddmm(first.date) + '</span><span>след. замер</span></div>' +
        '<div class="legend"><span><i class="dotf"></i>жир ' + n(first.body_fat_kg, 1) +
        ' кг → ?</span><span><i class="sqm"></i>мышцы ' + n(first.skeletal_muscle_kg, 1) +
        ' кг → ?</span></div>';
    }
    var t0 = Date.parse(first.measured_at), t1 = Date.parse(scans[scans.length - 1].measured_at);
    var span = Math.max(t1 - t0, 1);
    var dFat = scans.map(function (x) { return x.body_fat_kg - first.body_fat_kg; });
    var dMus = scans.map(function (x) {
      return x.skeletal_muscle_kg != null && first.skeletal_muscle_kg != null
        ? x.skeletal_muscle_kg - first.skeletal_muscle_kg : null;
    });
    var lim = 1;
    dFat.concat(dMus).forEach(function (v) { if (v != null) lim = Math.max(lim, Math.abs(v)); });
    lim *= 1.15;
    var X = function (m) { return pad + (Date.parse(m) - t0) / span * (W - pad * 2); };
    var Y = function (v) { return H / 2 - v / lim * (H / 2 - pad); };
    var path = function (vals) {
      var d = '';
      scans.forEach(function (x, i) {
        if (vals[i] == null) return;
        d += (d ? 'L' : 'M') + X(x.measured_at).toFixed(1) + ' ' + Y(vals[i]).toFixed(1);
      });
      return d;
    };
    var marks = scans.map(function (x, i) {
      var out = '<circle cx="' + X(x.measured_at) + '" cy="' + Y(dFat[i]) + '" r="4" fill="var(--fat)"/>';
      if (dMus[i] != null) {
        out += '<rect x="' + (X(x.measured_at) - 4) + '" y="' + (Y(dMus[i]) - 4) +
          '" width="8" height="8" fill="var(--data)"/>';
      }
      return out;
    }).join('');
    var last = scans[scans.length - 1];
    return '<div class="mic">Изменение от первого замера, кг</div>' +
      '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' +
      esc('Жир ' + signed(dFat[dFat.length - 1], 1) + ' кг, мышцы ' +
          signed(dMus[dMus.length - 1], 1) + ' кг с ' + ddmm(first.date)) + '">' +
      '<rect x="0" y="' + Y(1) + '" width="' + W + '" height="' + (Y(-1) - Y(1)) +
        '" fill="var(--surface-2)"/>' +
      '<line x1="0" x2="' + W + '" y1="' + Y(0) + '" y2="' + Y(0) +
        '" stroke="var(--separator)" stroke-width="1"/>' +
      '<path d="' + path(dFat) + '" fill="none" stroke="var(--fat)" stroke-width="2"/>' +
      '<path d="' + path(dMus) + '" fill="none" stroke="var(--data)" stroke-width="2"/>' +
      marks + '</svg>' +
      '<div class="comprow"><span>' + ddmm(first.date) + '</span><span>' +
        ddmm(last.date) + '</span></div>' +
      '<div class="legend"><span><i class="dotf"></i>жир</span>' +
      '<span><i class="sqm"></i>мышцы</span>' +
      '<span><i style="background:var(--surface-3)"></i>±1 кг — шум прибора</span></div>';
  }

  function compRow(label, a, b, unit, digits) {
    if (a == null || b == null) return '';
    var d = b - a;
    return '<div class="field"><label>' + esc(label) + '</label><span class="num">' +
      n(a, digits) + ' → ' + n(b, digits) + ' · ' + signed(d, digits) +
      (unit ? ' ' + unit : '') + '</span></div>';
  }

  function diaryBlock(o) {
    var d = o.diary;
    if (!d) return '';
    var since = d.since ? ddmm(d.since).slice(0, 5) : '';
    var title = 'Сверка с дневником', color = 'var(--text)', text;
    if (d.verdict === 'waiting' && d.days < 7) {
      text = 'Сверка с дневником появится через неделю записей после замера ' + since + '.';
    } else if (d.verdict === 'waiting') {
      text = d.deficit_kcal >= 0
        ? 'С ' + since + ' по дневнику дефицит ≈ ' + n(d.deficit_kcal) + ' ккал — около ' +
          n(Math.abs(d.expected_kg), 1) + ' кг веса, в основном жир. Следующий замер ' +
          'покажет, сколько ушло на самом деле.'
        : 'С ' + since + ' записано больше расхода на ' + n(-d.deficit_kcal) +
          ' ккал — около +' + n(d.expected_kg, 1) + ' кг. Следующий замер покажет, так ли это.';
      if (d.days >= 14 && d.logged_days < 0.8 * d.days) {
        text += ' Записано ' + n(d.logged_days) + ' дн. из ' + n(d.days) +
          ' — сверка будет неточной.';
      }
    } else if (d.verdict === 'few_logs') {
      text = 'Записано ' + n(d.logged_days) + ' дн. из ' + n(d.days) +
        ' — сверять дневник с замерами не по чему.';
    } else {
      var exp = signed(d.expected_kg, 1), got = signed(d.actual_kg, 1);
      if (d.verdict === 'agree') {
        title = 'Сходится';
        text = 'Ждали ' + exp + ' кг, весы показали ' + got + ' кг. Цифрам дневника можно верить.';
      } else if (d.verdict === 'less_lost') {
        title = 'Ушло меньше, чем ждали'; color = 'var(--alert)';
        text = got + ' кг вместо ' + exp + '. Обычно так, если не всё записано ' +
          'или расход ниже расчётного.';
      } else {
        title = 'Ушло больше, чем ждали'; color = 'var(--alert)';
        text = got + ' кг вместо ' + exp + '. Расход, похоже, выше расчётного — ' +
          'или часть потери вода.';
      }
    }
    return '<div class="wk"><div class="l">' +
      '<div class="mic" style="color:' + color + '">' + esc(title) + '</div>' +
      '<div class="cap" style="margin-top:4px;line-height:1.5">' + esc(text) + '</div>' +
      '</div></div>';
  }

  function energyBlock(o) {
    var e = o.energy;
    if (!e) return '';
    if (e.verdict === 'few_logs') {
      return '<div class="mic note">Расход по дневнику: записано ' + n(e.logged_days) +
        ' полных дн. из ' + n(e.days) + ', для расчёта нужно от ' + n(e.need_days) + '.</div>';
    }
    if (e.verdict !== 'ok') return '';
    var lo = e.tdee_kcal - e.error_kcal, hi = e.tdee_kcal + e.error_kcal;
    return '<div class="field"><label>Твой расход по дневнику и замерам</label>' +
      '<span class="num">' + n(lo) + '–' + n(hi) + ' ккал</span></div>' +
      (e.proposable
        ? '<div class="opts"><button data-scanprop="1">Посмотреть новые цели</button></div>' +
          '<div id="propslot"></div>'
        : '<div class="mic note">Разброс пока широкий — ещё один замер через месяц, и можно ' +
          'будет предложить цель по расходу.</div>');
  }

  /* Балл прибора (MA-INV-62): число и разница, без стрелок и цвета. Формула у
     InBody не опубликована, и стрелка «стало лучше» была бы выдумкой. */
  function scoreLine(last, prev) {
    if (last.inbody_score == null) return '';
    var tail = 'первый замер';
    if (prev && prev.inbody_score != null) {
      var d = Math.round(last.inbody_score) - Math.round(prev.inbody_score);
      tail = d ? signed(d) + ' с прошлого раза' : 'как в прошлый раз';
    }
    return '<div class="score"><div class="num v">' + n(last.inbody_score) +
      '<span class="unit"> из 100</span></div>' +
      '<div class="num d">' + esc(tail) + '</div></div>' +
      (prev && prev.inbody_score != null ? '' :
        '<div class="mic note">Сводная оценка прибора: за каждый килограмм мышц сверх нормы ' +
        'балл растёт, за килограмм жира — падает. В расчёты не входит.</div>');
  }

  var FORECAST_WORD = {
    one_scan: 'Темп и срок появятся после второго замера.',
    too_soon: 'Между замерами меньше месяца — темп считаю от 28 дней: короче разница тонет ' +
              'в погрешности прибора.',
    no_target: 'Поставь цель по проценту жира в настройках — посчитаю срок.',
    reached: 'Цель по проценту жира уже достигнута.',
    noisy: 'Темп пока не отличить от погрешности прибора — срок посчитаю после следующего ' +
           'замера.',
    wrong_way: 'Жир прибавляется — срок до цели не считаю.',
    too_far: 'При нынешнем темпе до цели больше двух лет — срок не показываю, он всё равно ' +
             'сдвинется.'
  };

  function months(m) {
    var whole = Math.round(m);
    var word = whole % 10 === 1 && whole % 100 !== 11 ? 'месяц'
      : (whole % 10 > 1 && whole % 10 < 5 && (whole % 100 < 10 || whole % 100 > 20))
        ? 'месяца' : 'месяцев';
    return whole + ' ' + word;
  }

  /* Темп и срок (§6.24). Число темпа показываем всегда, срок — только когда
     темп крупнее собственной погрешности (MA-INV-61). */
  function paceBlock(o) {
    var p = o.pace, f = o.forecast;
    if (!p) {
      var why = f && FORECAST_WORD[f.verdict];
      return why ? '<div class="mic note">' + esc(why) + '</div>' : '';
    }
    var tile = function (label, v, tone) {
      var quiet = Math.abs(v) <= p.noise_kg_per_month;
      return '<div class="bwc"><div class="mic">' + label + '</div>' +
        '<div class="num v" style="color:' + tone + '">' + signed(v, 1) + '</div>' +
        '<div class="mic">кг в месяц</div>' +
        '<div class="mic faint">' + (quiet ? 'в пределах шума' : 'шум ±' +
          n(p.noise_kg_per_month, 1)) + '</div></div>';
    };
    var line = '';
    if (f && f.verdict === 'ok') {
      line = '<div class="cap note">До ' + n(f.target_pct) + '% жира при нынешнем темпе — ещё ' +
        'около <b>' + months(f.months) + '</b>.</div>' +
        '<div class="mic note">Оценка по последним двум замерам, без учёта замедления. ' +
        'Каждый новый замер её пересчитывает.</div>';
    } else if (f && FORECAST_WORD[f.verdict]) {
      line = '<div class="mic note">' + esc(FORECAST_WORD[f.verdict]) + '</div>';
    }
    return '<div class="mic note">Темп за ' + n(p.days) + ' дн.</div>' +
      '<div class="bw">' + tile('Жир', p.fat_kg_per_month, 'var(--fat)') +
      tile('Безжировая', p.lean_kg_per_month, 'var(--data)') + '</div>' + line;
  }

  var SEG_NAME = { arm_l: 'Л. рука', arm_r: 'П. рука', trunk: 'Туловище',
                   leg_l: 'Л. нога', leg_r: 'П. нога' };
  /* Ниже этого разница сторон — погрешность прибора, а не наблюдение:
     воспроизводимость сегмента 0.0–0.1 кг, естественная асимметрия 1.5–8%. */
  var SEG_EVEN = 0.05;

  /* Числа сегментов печатаем как на бланке: у конечностей две цифры после
     запятой, у туловища одна. Общий `n()` тут не годится — он срезает нули и
     превращает «32,6» в «32,60», а «12,0» в «12». */
  function segNum(v) {
    return String(v).replace('.', ',');
  }

  function segCell(key, seg, delta) {
    var lean = seg.lean, fat = seg.fat, total = lean + fat;
    var share = total > 0 ? Math.max(0, Math.min(100, 100 * fat / total)) : 0;
    return '<div class="seg' + (key === 'trunk' ? ' tr' : '') + '">' +
      '<div class="nm">' + esc(SEG_NAME[key]) + '</div>' +
      '<div class="num v">' + segNum(lean) + '</div>' +
      '<div class="num f">жир ' + segNum(fat.toFixed(1)) + '</div>' +
      '<div class="track split"><i style="width:' + (100 - share) + '%;background:var(--data)"></i>' +
      '<i style="width:' + share + '%;background:var(--fat)"></i></div>' +
      (delta ? '<div class="num f alert">' + esc(delta) + '</div>' : '') + '</div>';
  }

  /* Сегменты: на первом экране — одна строка-вердикт, десять чисел под тапом.
     Схема тела сеткой, а не SVG: viewBox с процентной шириной раздувается по
     высоте на широком окне (web/responsive.md), а смысл всё равно несёт текст. */
  function segBlock(last, prev) {
    var seg = last.segments;
    var smi = last.smi == null ? '' : 'Мышечный индекс <b class="num">' + n(last.smi, 1) + '</b>' +
      (prev && prev.smi != null
        ? ' <span class="num g">(' + signed(last.smi - prev.smi, 1) + ')</span>' : '');
    if (!seg) {
      return smi ? '<div class="mic note" style="color:var(--text)">' + smi + '</div>' +
        '<div class="mic">Мышцы рук и ног на рост, кг/м². Порог дефицита 7,0 — он для ' +
        'пожилых, смотрим на динамику.</div>' : '';
    }
    var arms = Math.abs(seg.arm_l.lean - seg.arm_r.lean) /
      Math.max(0.1, (seg.arm_l.lean + seg.arm_r.lean) / 2);
    var legs = Math.abs(seg.leg_l.lean - seg.leg_r.lean) /
      Math.max(0.1, (seg.leg_l.lean + seg.leg_r.lean) / 2);
    var worst = Math.max(arms, legs);
    var verdict = worst <= SEG_EVEN
      ? 'слева и справа ровно, разница до ' + Math.round(worst * 100) + '%'
      : (arms >= legs ? 'руки' : 'ноги') + ' расходятся на ' + Math.round(worst * 100) + '%';
    var moved = [];
    var cells = ['arm_l', 'trunk', 'arm_r', 'leg_l', 'leg_r'].map(function (k) {
      var d = '';
      if (prev && prev.segments && prev.segments[k]) {
        var df = seg[k].fat - prev.segments[k].fat;
        /* Сдвинулось заметнее шума: 5% сегмента, но не меньше 0.3 кг — одно
           правило и для руки в 4 кг, и для туловища в 33. */
        if (Math.abs(df) >= Math.max(0.3, 0.05 * (seg[k].lean + seg[k].fat))) {
          d = signed(df, 1) + ' кг жира';
          moved.push(SEG_NAME[k].toLowerCase() + ' ' + signed(df, 1) + ' кг жира');
        }
      }
      return segCell(k, seg[k], d);
    }).join('');
    return '<details class="scanlist segs-wrap"><summary class="mic">' +
      (smi ? smi + ' · ' : '') + 'Сегменты: ' + esc(verdict) + '</summary>' +
      (last.smi != null ? '<div class="mic note">Мышцы рук и ног на рост, кг/м². Порог ' +
        'дефицита 7,0 — он для пожилых, смотрим на динамику.</div>' : '') +
      '<div class="mic note">' + (moved.length
        ? 'С прошлого раза: ' + esc(moved.join(', ')) + '.'
        : prev && prev.segments
          ? 'С прошлого раза сегменты не сдвинулись заметнее шума.'
          : 'В прошлом замере сегментов не было.') + '</div>' +
      '<div class="mic">Безжировая масса, кг · под ней жир · полоса — доля жира в сегменте</div>' +
      '<div class="segs">' + cells + '</div>' +
      '<div class="mic note">Прибор меряет сопротивление тканей, а не силу: по этим числам ' +
      'нельзя сказать, что рука слабее или что сегмент потерял мышцу. Вода и гликоген ' +
      'считаются здесь безжировой массой.</div></details>';
  }

  function compCard(o) {
    if (!o || !o.scans || !o.scans.length) return '';
    var scans = o.scans, last = scans[scans.length - 1], prev = scans[scans.length - 2];
    var c = o.change;
    var body = scoreLine(last, prev) + paceBlock(o) + compChart(scans);
    if (prev) {
      body += '<div class="rows">' +
        compRow('Вес', prev.weight_kg, last.weight_kg, 'кг', 1) +
        compRow('Жир', prev.body_fat_kg, last.body_fat_kg, 'кг', 1) +
        compRow('Мышцы', prev.skeletal_muscle_kg, last.skeletal_muscle_kg, 'кг', 1) +
        compRow('% жира', prev.body_fat_pct, last.body_fat_pct, '', 1) +
        compRow('Висцеральный', prev.visceral_level, last.visceral_level, '', 0) +
        '</div>';
    } else {
      body += '<div class="rows">' +
        '<div class="field"><label>Вес</label><span class="num">' + n(last.weight_kg, 1) + ' кг</span></div>' +
        '<div class="field"><label>Жир</label><span class="num">' + n(last.body_fat_kg, 1) +
          ' кг · ' + n(last.body_fat_pct, 1) + '%</span></div>' +
        '<div class="field"><label>Безжировая масса</label><span class="num">' +
          n(last.fat_free_kg, 1) + ' кг</span></div>' +
        (last.visceral_level != null ? '<div class="field"><label>Висцеральный жир</label>' +
          '<span class="num">' + n(last.visceral_level) + ' · норма 1–9</span></div>' : '') +
        '</div>';
    }
    if (c && c.lean_share != null) {
      var fatPart = Math.max(0, Math.min(100, 100 * c.body_fat_kg / c.weight_kg));
      body += '<div class="mic" style="margin-top:var(--s-3)">Из ' + signed(c.weight_kg, 1) +
        ' кг: жир ' + signed(c.body_fat_kg, 1) + ', безжировая ' + signed(c.fat_free_kg, 1) + '</div>' +
        '<div class="track split"><i style="width:' + fatPart + '%;background:var(--fat)"></i>' +
        '<i style="width:' + (100 - fatPart) + '%;background:var(--data)"></i></div>';
      if (o.lean_warning) {
        body += '<div class="attn">Из ' + signed(c.weight_kg, 1) + ' кг на безжировую массу ' +
          'пришлось ' + n(Math.abs(c.fat_free_kg), 1) + ' — больше трети. Часть может быть ' +
          'водой; добирай белок и не бросай силовые, дефицит можно смягчить.</div>';
      }
    }
    body += segBlock(last, prev) + diaryBlock(o) + energyBlock(o);
    if (o.stale_days > 180) {
      body += '<div class="attn">Замер ' + n(Math.round(o.stale_days / 30)) +
        ' мес. назад — пора обновить. Пока цели считаются без него.</div>';
    }
    body += '<details class="scanlist"><summary class="mic">Замеры · ' + n(scans.length) +
      '</summary>' + scans.slice().reverse().map(function (x) {
        return '<div class="field"><label>' + ddmm(x.date) + ' · ' + n(x.weight_kg, 1) +
          ' кг · жир ' + n(x.body_fat_pct, 1) + '%</label>' +
          '<button class="linkbtn" data-scandel="' + x.id + '">Удалить</button></div>';
      }).join('') + '</details>';
    var ago = o.stale_days == null ? ''
      : o.stale_days === 0 ? ' · сегодня'
      : o.stale_days === 1 ? ' · вчера'
      : ' · ' + n(o.stale_days) + ' дн. назад';
    var head = ddmm(last.date) + ago + (last.device ? ' · ' + esc(last.device) : '');
    return '<section class="card comp" style="margin-top:var(--s-3)">' +
      '<div class="cap">Состав тела</div><div class="mic">' + head + '</div>' +
      '<div class="chart">' + body + '</div>' +
      '<div class="mic note">Сравнивай замеры, сделанные в одинаковых условиях: утром, ' +
      'натощак, без тренировки накануне.</div></section>';
  }

  /* «Было → станет» (MA-INV-52): цели меняются только этой кнопкой. */
  function proposalCard(p) {
    if (!p) return '';
    var cur = p.current, nw = p.proposed;
    var cell = function (label, a, b, unit) {
      var d = Math.round(b) - Math.round(a);
      return '<div class="bwc"><div class="mic">' + label + '</div>' +
        '<div class="num v">' + n(b) + (unit ? ' <span class="unit">' + unit + '</span>' : '') + '</div>' +
        '<div class="mic">' + (d ? 'было ' + n(a) + ' · ' + signed(d) : 'без изменений') + '</div></div>';
    };
    var formula = p.base === 'energy'
      ? n(p.tdee_kcal) + ' расход по замерам × ' + n(p.target_factor, 2) + ' ' +
        (TARGET_WORD[cur.target] || '')
      : n(p.bmr_kcal) + ' обмен × ' + n(p.activity_factor, 3) + ' активность × ' +
        n(p.target_factor, 2) + ' ' + (TARGET_WORD[cur.target] || '');
    return '<section class="card just" style="margin-top:var(--s-3)" id="propcard">' +
      '<div class="ttl">Цели по замеру</div>' +
      '<div class="mic" style="margin-top:4px">' + esc(formula) + '</div>' +
      (p.fat_free_kg ? '<div class="mic">белок — от ' + n(p.fat_free_kg, 1) +
        ' кг безжировой массы</div>' : '') +
      '<div class="bw">' +
        cell('Калории', cur.kcal, nw.kcal, '') +
        cell('Белок', cur.protein_g, nw.protein_g, 'г') +
        cell('Жир', cur.fat_g, nw.fat_g, 'г') +
        cell('Углеводы', cur.carb_g, nw.carb_g, 'г') +
      '</div>' +
      (p.changed
        ? '<div class="mic" style="margin-top:var(--s-3)">Прошедшие дни недели останутся ' +
          'посчитанными по старой цели.</div>' +
          '<button class="primary" data-applygoal="1">Применить цели</button>' +
          '<div class="opts" style="margin-top:var(--s-2)"><button data-propclose="1" ' +
          'style="border-color:var(--surface-3);background:var(--surface-2)">Не сейчас</button></div>'
        : '<div class="mic" style="margin-top:var(--s-3)">Цели уже совпадают с замером.</div>' +
          '<div class="opts"><button data-propclose="1">Понятно</button></div>') +
      '</section>';
  }

  /* Карточка «было → станет» рисуется там, где нажали: на «Дне» — как карточка
     записи, в «Итогах» и настройках — в своём слоте. В state она живёт только
     на «Дне», иначе всплывала бы там позже без контекста. */
  function openProposal() {
    api('/api/body-scans').then(function (o) {
      if (!o.proposal) { toast('Нужен свежий замер InBody.', true); return; }
      if (state.tab === 'day') { state.proposal = o.proposal; return repaintDay(); }
      var el = document.getElementById('propslot');
      if (el) {
        el.innerHTML = proposalCard(o.proposal);
        el.scrollIntoView({ block: 'nearest' });
      }
    }).catch(function (e) { toast(e.message, true); });
  }

  function applyGoal() {
    if (state.sending) return;
    state.sending = true;
    send('/api/body-scans/apply-goal', { headers: { 'Idempotency-Key': keyFor('applygoal') } })
      .then(function () {
        dropKey('applygoal');
        state.proposal = null;
        toast('Цели обновлены.');
        if (state.tab === 'set') return renderSettings();
        if (state.tab === 'tot') return renderTotals();
        return renderDay();
      })
      .catch(oops)
      .then(function () { state.sending = false; });
  }

  /* ── карточка бланка InBody (§6.24) ──────────────────────────────────── */

  var SCAN_ROWS = [
    ['weight_kg', 'Вес', 'кг'],
    ['skeletal_muscle_kg', 'Скелетные мышцы', 'кг'],
    ['body_fat_kg', 'Жир', 'кг'],
    ['body_fat_pct', 'Процент жира', '%'],
    ['fat_free_kg', 'Безжировая масса', 'кг'],
    ['visceral_level', 'Висцеральный жир', 'ур.'],
    ['bmr_kcal', 'Базовый обмен', 'ккал'],
    ['smi', 'Мышечный индекс', 'кг/м²'],
    ['water_l', 'Вода', 'л'],
    ['protein_kg', 'Белок', 'кг'],
    ['minerals_kg', 'Минералы', 'кг']
  ];
  var SCAN_ALWAYS = { weight_kg: 1, skeletal_muscle_kg: 1, body_fat_kg: 1, body_fat_pct: 1,
                      fat_free_kg: 1, visceral_level: 1, bmr_kcal: 1, water_l: 1, smi: 1 };

  function scanValue(sc, key) {
    return sc.edits[key] != null ? sc.edits[key] : sc.card.scan[key];
  }

  function scanWarn(sc) {
    var c = sc.card, bad = c.bad;
    var lines = [];
    if (bad.length) {
      var w = Number(String(scanValue(sc, 'weight_kg')).replace(',', '.'));
      var f = Number(String(scanValue(sc, 'body_fat_kg')).replace(',', '.'));
      var l = Number(String(scanValue(sc, 'fat_free_kg')).replace(',', '.'));
      var sum = Math.round((f + l) * 10) / 10;
      lines.push(bad.indexOf('weight_kg') > -1 && Math.abs(w - sum) > 0.3
        ? 'Вес не равен жир + безжировая: ' + n(w, 1) + ' ≠ ' + n(f, 1) + ' + ' + n(l, 1) + '.'
        : 'Отмеченные числа не сходятся с остальными — сверь с бланком.');
    }
    if (c.foreign) lines.push('Похоже, бланк не твой: рост, пол, возраст или вес не сходятся ' +
                              'с прошлыми данными.');
    if (c.date_ambiguous) lines.push('Дату на бланке можно прочитать двумя способами — проверь её.');
    if (c.date_problem === 'unread') lines.push('Дату на бланке не разобрал — поставил ' +
      'сегодняшнюю. Замер был в другой день — поправь дату.');
    if (c.date_problem === 'old') lines.push('Замеру больше двух лет — точно этот?');
    if (c.date_problem === 'future') lines.push('Дата замера в будущем — поправь дату.');
    if (!lines.length) return '';
    var confirmed = sc.confirmed;
    return '<div class="scanwarn' + (confirmed ? ' ok' : '') + '" id="scanwarn">' +
      (confirmed ? 'Сверено с бланком.' : lines.map(esc).join('<br>')) +
      (!confirmed && c.date_problem !== 'future'
        ? '<div class="opts" style="margin-top:var(--s-2)"><button data-scanok="1">Всё верно</button></div>'
        : '') + '</div>';
  }

  function scanCard(sc) {
    var c = sc.card, s = c.scan;
    var rows = SCAN_ROWS.filter(function (r) {
      return SCAN_ALWAYS[r[0]] || c.bad.indexOf(r[0]) > -1;
    }).map(function (r) {
      var v = scanValue(sc, r[0]);
      if (v == null && !SCAN_ALWAYS[r[0]]) return '';
      var bad = c.bad.indexOf(r[0]) > -1;
      var id = 'sf-' + r[0];
      return '<div class="sf' + (bad ? ' bad' : '') + '">' +
        '<label for="' + id + '">' + esc(r[1]) +
          (r[0] === 'visceral_level' ? ' <span class="g">· норма 1–9</span>' : '') + '</label>' +
        '<input id="' + id + '" data-sf="' + r[0] + '" type="text" inputmode="decimal" ' +
          'value="' + esc(v == null ? '' : String(v).replace('.', ',')) + '"' +
          (bad ? ' aria-invalid="true" aria-describedby="scanwarn"' : '') + '>' +
        '<span class="u">' + r[2] + '</span></div>';
    }).join('');
    var dateVal = sc.edits.date || s.date;
    var dateRow = '<div class="sf' + (c.date_ambiguous || c.date_problem ? ' bad' : '') + '">' +
      '<label for="sf-date">Дата замера</label>' +
      '<input id="sf-date" data-sf="date" type="date" value="' + esc(dateVal) + '">' +
      '<span class="u">' + esc(sc.edits.time || s.time) + '</span></div>';
    var blocked = c.date_problem === 'future' || (c.needs_confirm && !sc.confirmed);
    var sub = [s.height_cm ? n(s.height_cm) + ' см' : '', s.age ? n(s.age) + ' лет' : '']
      .filter(Boolean).join(' · ');
    return '<section class="card just scan" style="margin-top:var(--s-3)">' +
      '<div class="ttl">С фото: ' + esc(s.device || 'InBody') + ' · ' + ddmm(dateVal) + '</div>' +
      '<div class="mic" style="margin-top:3px">' + (sub ? esc(sub) + ' · ' : '') +
        'нажми на число, чтобы поправить</div>' +
      (c.existing ? '<div class="mic" style="margin-top:6px;color:var(--text)">Такой замер уже ' +
        'есть — сохраню поверх.</div>' : '') +
      '<div class="sfs">' + dateRow + rows + '</div>' +
      scanExtras(sc) +
      '<div class="mic" style="margin-top:var(--s-3);line-height:1.5">Остальное с бланка не ' +
        'сохраняю — на расчёты не влияет.</div>' +
      scanWarn(sc) +
      '<button class="primary" data-scansave="1"' + (blocked ? ' disabled' : '') + '>' +
        (c.existing ? 'Обновить замер' : 'Сохранить замер') + '</button>' +
      '<div class="opts" style="margin-top:var(--s-2)"><button data-scancancel="1" ' +
        'style="border-color:var(--surface-3);background:var(--surface-2)">Отмена</button></div>' +
      '<div class="mic" style="margin-top:var(--s-3);line-height:1.5">Сравнивай замеры, ' +
        'сделанные в одинаковых условиях: утром, натощак, без тренировки накануне.</div>' +
      '</section>';
  }

  /* Балл и сегменты на карточке бланка. Сегменты не правятся: десять полей
     ввода на телефоне ради чисел, от которых не зависит ни одна цель, не
     окупаются — при расхождении честнее переснять бланк.
     ponytail: понадобится правка — эти числа идут в SCAN_ROWS, а блок
     раскрывается отдельным экраном, а не внутри карточки. */
  function scanExtras(sc) {
    var c = sc.card, s = c.scan, out = '';
    if (s.inbody_score != null) {
      out += '<div class="mic note" style="color:var(--text)">Балл InBody <b class="num">' +
        n(s.inbody_score) + '</b> из 100</div>';
    }
    if (s.segments) {
      out += '<details class="scanlist"><summary class="mic">Сегменты · сходятся с ' +
        'безжировой</summary><div class="segs">' +
        ['arm_l', 'trunk', 'arm_r', 'leg_l', 'leg_r'].map(function (k) {
          return segCell(k, s.segments[k], '');
        }).join('') + '</div></details>';
    } else if (c.dropped_segments) {
      out += '<div class="mic note" style="color:var(--alert);line-height:1.5">Сегментный ' +
        'анализ не сошёлся с безжировой массой — сохраню без него. На цели он не влияет; ' +
        'нужен — пересними бланк целиком, при свете.</div>';
    }
    return out;
  }

  function recheckScan() {
    var sc = state.scan;
    if (!sc) return;
    /* Номер запроса: два быстрых ухода из полей дают два ответа, и старший не
       должен затереть младший. */
    var seq = sc.seq = (sc.seq || 0) + 1;
    send('/api/body-scans/check', {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ draft_id: sc.card.draft_id, fields: sc.edits })
    }).then(function (r) {
      if (state.scan !== sc || seq !== sc.seq) return;
      sc.card = r;
      if (!r.needs_confirm) sc.confirmed = false;
      repaintDay();
    }).catch(oops);
  }

  function saveScan() {
    var sc = state.scan;
    if (!sc || state.sending) return;
    state.sending = true;
    var id = 'scan:' + sc.card.draft_id;
    busy('Сохраняю замер…');
    send('/api/body-scans', {
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': keyFor(id) },
      body: JSON.stringify({ draft_id: sc.card.draft_id, fields: sc.edits,
                             confirmed: sc.confirmed })
    }).then(function (r) {
      dropKey(id);
      state.scan = null;
      state.proposal = r.proposal && r.proposal.changed ? r.proposal : null;
      toast(r.updated ? 'Замер обновлён.' : 'Замер сохранён. Он появится в «Итогах».');
      renderDay();
    }).catch(function (e) {
      if (e.code === 'draft_gone') state.scan = null;
      oops(e);
      renderDay();
    }).then(function () { state.sending = false; });
  }

  /* ── фото блюда (§6.22) ──────────────────────────────────────────────

     Фото говорит, ЧТО на тарелке, человек — СКОЛЬКО. Карточка правит только
     граммы: пересчёт общего веса — умножение граммов, а не калорий, поэтому
     INV-1 не задет. Калорий здесь нет вовсе: их посчитает разбор после
     «Записать», и цифра до записи разошлась бы с цифрой после. */

  function photoState(res) {
    return {
      draft_id: res.draft_id, dish: res.dish, weighed: !!res.weighed,
      liquid: !!res.liquid,
      items: (res.components || []).map(function (c) {
        return { i: c.i, name: c.name, grams: String(Math.round(c.grams)), off: false,
                 fixed: !!c.fixed, note: c.note || '', ratio: c.edible_ratio || 0 };
      }),
      base: null
    };
  }

  function photoTotal(p) {
    return p.items.reduce(function (a, it) {
      return a + (it.off ? 0 : (Number(it.grams) || 0));
    }, 0);
  }

  /* FR-34.2: «в панцире» — съедобную часть сервер запишет как round(граммы × доля), половина
     вверх; здесь та же арифметика, чтобы шторка и запись совпали до грамма. */
  function photoEdible(it) {
    var g = Number(it.grams) || 0;
    return it.ratio && g > 0 && !it.off ? ' → съедобно ≈' + Math.round(g * it.ratio) + ' г' : '';
  }

  function photoCard(p) {
    var rows = p.items.map(function (it, k) {
      return '<div class="prow' + (it.off ? ' off' : '') + '">' +
        /* D-MA-20: масло «на готовку» — строкой списка, без вопроса; лишнее убирается ✕. */
        '<span class="pname">' + esc(it.name) +
          (it.note ? ' <span class="g">· ' + esc(it.note) + '</span>' : '') +
          (it.ratio ? ' <span class="g" data-pe="' + k + '">' + esc(photoEdible(it)) +
            '</span>' : '') + '</span>' +
        (it.off
          ? '<button class="px" data-pback="' + k + '" aria-label="Вернуть ' + esc(it.name) +
            '">Вернуть</button>'
          : '<input class="pg" data-pg="' + k + '" type="number" inputmode="numeric" ' +
            'min="1" max="3000" value="' + esc(it.grams) + '" aria-label="Граммы: ' +
            esc(it.name) + '"><span class="g">г</span>' +
            '<button class="px" data-pdrop="' + k + '" aria-label="Убрать ' + esc(it.name) +
            '"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" ' +
            'stroke="currentColor" stroke-width="2.4" stroke-linecap="round" ' +
            'aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>') +
        '</div>';
    }).join('');
    return '<section class="card just photo" style="margin-top:var(--s-3)">' +
      '<div class="ttl">С фото: ' + esc(p.dish || 'блюдо') + '</div>' +
      '<div class="mic" id="pmark" style="margin-top:4px">' +
        (p.weighed ? 'взвешено' : 'вес на глаз — поправь, если знаешь') + '</div>' +
      '<div class="prow ptotal">' +
        '<span class="pname">' + (p.liquid ? 'Вся порция' : 'Вся тарелка') + '</span>' +
        '<input class="pg" id="ptotal" type="number" inputmode="numeric" min="1" max="5000" ' +
          'value="' + photoTotal(p) + '" aria-label="Вес всей тарелки, граммы">' +
        '<span class="g">г</span><span class="px" aria-hidden="true"></span>' +
      '</div>' +
      rows +
      (p.weighed ? '' :
        '<div class="mic" style="margin-top:var(--s-3);line-height:1.5">' +
        'Взвесь тарелку и впиши вес — так точнее.</div>') +
      '<button class="primary" data-pcommit="1">Записать</button>' +
      '<div class="opts" style="margin-top:var(--s-2)">' +
        '<button data-pcancel="1" style="border-color:var(--surface-3);' +
          'background:var(--surface-2)">Отмена</button>' +
      '</div></section>';
  }

  /* Поле веса правится на месте, без перерисовки: перерисовка на каждой цифре
     выбивала бы курсор из поля. */
  function photoInput(el) {
    var p = state.photo;
    if (!p) return;
    p.weighed = true;
    var mark = document.getElementById('pmark');
    if (mark) mark.textContent = 'взвешено';
    if (el.id === 'ptotal') {
      /* Масштаб — от граммов на момент начала правки, а не от текущих: набор
         «420» идёт через «4» и «42», и пересчёт от промежуточных значений
         округлял бы позиции до единиц и терял пропорции. */
      /* Вес позиции, названный подписью («курица 150»), не двигается — делится
         только остаток, как на сервере (app/domain/plate.py). */
      if (!p.base) p.base = p.items.map(function (it) { return Number(it.grams) || 0; });
      var pinned = p.items.reduce(function (a, it, idx) {
        return a + (!it.off && it.fixed ? p.base[idx] : 0);
      }, 0);
      var want = Number(el.value) - pinned;
      var have = p.items.reduce(function (a, it, idx) {
        return a + (it.off || it.fixed ? 0 : p.base[idx]);
      }, 0);
      if (!(want > 0) || !(have > 0)) return;
      p.items.forEach(function (it, idx) {
        if (it.off || it.fixed) return;
        it.grams = String(Math.round(p.base[idx] * want / have));
        var f = root.querySelector('[data-pg="' + idx + '"]');
        if (f) f.value = it.grams;
        photoEdibleAt(p, idx);
      });
      return;
    }
    p.base = null;
    p.items[Number(el.dataset.pg)].grams = el.value;
    photoEdibleAt(p, Number(el.dataset.pg));
    p.items[Number(el.dataset.pg)].fixed = true;  // правил руками — общий вес её не двигает
    var t = document.getElementById('ptotal');
    if (t) t.value = photoTotal(p);
  }

  function photoEdibleAt(p, idx) {
    var s = root.querySelector('[data-pe="' + idx + '"]');
    if (s) s.textContent = photoEdible(p.items[idx]);
  }

  function commitPhoto() {
    var p = state.photo;
    if (!p || state.sending) return;
    var kept = p.items.filter(function (it) { return !it.off; });
    if (!kept.length) { toast('Оставь хотя бы одну позицию.', true); return; }
    if (kept.some(function (it) {
      var g = Number(it.grams);
      return !(g >= 1 && g <= 3000);
    })) { toast('Граммы — от 1 до 3000 на позицию.', true); return; }
    state.sending = true;
    var id = 'photo:' + p.draft_id;
    busy('Записываю…');
    send('/api/meals/photo/commit', {
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': keyFor(id) },
      body: JSON.stringify({
        draft_id: p.draft_id,
        components: kept.map(function (it) { return { i: it.i, grams: Number(it.grams) }; }),
        weighed: p.weighed
      })
    }).then(function (r) { dropKey(id); return handle(r); })
      .catch(function (e) {
        if (e.code === 'draft_gone') state.photo = null;
        oops(e);
        renderDay();
      }).then(function () { state.sending = false; });
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
    state.photo = null;
    state.scan = null;
    if (res.kind === 'logged') {
      /* §6.27 (1Б): отдельной карточки «Записал» нет — она дублировала строку ленты и через
         5 с уходила, а поправить в ней было нечего. Запись раскрыта и подсвечена в ленте,
         правка — тапом по строке; «Отменить» — в тосте. */
      state.fresh = res.meal.id;
      state.freshScroll = true;
      state.open[res.meal.id] = true;
      toastUndo('Записал · ' + n(res.meal.kcal) + ' ккал', res.meal.id);
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
    } else if (res.kind === 'clarify') {
      state.clarify = res;
    } else if (res.kind === 'label') {
      state.label = res;
    } else if (res.kind === 'photo') {
      state.photo = photoState(res);
    } else if (res.kind === 'body_scan') {
      state.proposal = null;
      state.scan = { card: res, edits: {}, confirmed: false };
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
    } else if (res.kind === 'recipe') {
      /* FR-39.1: рецепт — продукт, а не съеденное: в день не пишем, говорим как звать дальше. */
      toast('Рецепт «' + res.name + '» сохранён: ' + n(res.kcal_per_100g) + ' ккал на 100 г. ' +
            (res.unit_weight_g
              ? 'Порция ' + n(res.unit_weight_g) + ' г — пиши «' + res.name + '».'
              : 'В день не записан — дальше пиши «' + res.name + ' 120 г».'));
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
    if (e && e.code === 'in_flight') return false;
    /* Словарь FR-47: сервер называет запись «приёмом» — в аппке это запись. */
    toast(e && e.code === 'meal_not_found' ? 'Этой записи уже нет.'
          : e && e.message === 'Failed to fetch' ? 'Нет связи — не сохранилось. Попробуй ещё раз.'
          : e.message, true);
    /* MA-INV-74: экран мог быть нарисован из сохранённого дня, а приём к тому
       времени удалили с другого устройства. Ошибки мало — день надо освежить,
       иначе человек тапает по тому, чего уже нет. Поверх экрана, без скелета. */
    if (e && (e.code === 'meal_not_found' || e.code === 'item_not_found') &&
        state.tab === 'day') {
      if (state.sheet) closeSheet();
      renderDay();
      return true;
    }
    return false;
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

  /* Один снимок — этикетка или блюдо, различает сервер (§6.22). Набранный в
     поле текст уходит подписью: «420» — вес тарелки, «курица 150» — вес
     позиции. Так вес можно указать до съёмки, не дожидаясь карточки. */
  function sendPhoto(file) {
    if (state.sending) return;
    state.sending = true;
    var caption = (state.text || '').trim().slice(0, 200);
    busy('Смотрю на фото…');
    shrink(file, function (blob) {
      var headers = {
        'Content-Type': blob.type || 'image/jpeg',
        'Idempotency-Key': keyFor('photo')
      };
      /* Подпись — заголовком, не в адресе: адрес попадает в журнал доступа. */
      if (caption) headers['X-Caption'] = encodeURIComponent(caption);
      send('/api/meals/photo?days_ago=' + writeDay(), {
        headers: headers,
        body: blob
      }).then(function (r) {
        dropKey('photo');
        if (caption) state.text = '';
        return handle(r);
      }).catch(function (e) {
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
          .catch(function (e) { if (!oops(e)) renderDay(); });
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
    state.fresh = null;
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

    /* §6.25: «↻ сегодня» живёт внутри строки позиции — ловим до строки, иначе тап по
       кнопке открыл бы правку веса. */
    var again = e.target.closest('button[data-again-i]');
    if (again) {
      if (state.sending) return;
      state.sending = true;
      var ak = 'again:' + again.dataset.againI;
      return send('/api/meals/' + again.dataset.againM + '/items/' + again.dataset.againI +
                  '/repeat', {
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': keyFor(ak) },
        body: JSON.stringify({ days_ago: 0 })
      }).then(function (r) { dropKey(ak); logged(r, 0); })
        .catch(function (err) { oops(err); })
        .then(function () { state.sending = false; });
    }

    var row = e.target.closest('.row[data-item]');
    if (row && !e.target.closest('.composer')) {
      /* §6.27: одна шторка записи на любой день — вес, «поправить словами», «ещё раз
         сегодня», «убрать». Раньше сегодня открывалось голое поле граммов, вчера — шторка. */
      return itemSheet(Number(row.dataset.meal), Number(row.dataset.item));
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
      /* §6.25: повтор приёма пишет в сегодня, с какого бы дня ни нажали — «съел то же
         ещё раз». С прошлого дня экран остаётся на месте, подтверждение тостом. */
      return send('/api/meals/' + b.dataset.repeat + '/repeat?days_ago=0',
                  { headers: { 'Idempotency-Key': keyFor(rid) } })
        .then(function (r) { dropKey(rid); return logged(r, 0); })
        .catch(function (err) { if (!oops(err)) renderDay(); })
        .then(function () { state.sending = false; });
    }
    if (b.dataset.drop) {
      /* MA-INV-69: без «Вы уверены?» — его прокликивают не читая. Удалено сразу, «Вернуть»
         в тосте возвращает приём целиком. */
      var did = b.dataset.drop;
      var dm = state.lastDay && (state.lastDay.meals || []).filter(function (x) {
        return String(x.id) === did;
      })[0];
      if (state.sending) return;
      state.sending = true;
      busy('Удаляю…');
      return send('/api/meals/' + did, { method: 'DELETE' })
        .then(function (r) {
          delete state.open[did];
          if (state.fresh === Number(did)) state.fresh = null;
          restoreToast('Удалил' + (dm ? ': ' + mealTitle(dm) : '.'), r.undo);
          renderDay();
        })
        .catch(function (err) { if (!oops(err)) renderDay(); })
        .then(function () { state.sending = false; });
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
        if (stay) return logged(r, 0);
        return handle(r);
      }).catch(function (err) {
        oops(err);
        return stay ? rerender() : renderDay();
      }).then(function () { state.sending = false; });
    }
    if (b.dataset.recipe) {
      var cardd = state.cat.card;
      if (cardd && String(cardd.product.id) === b.dataset.recipe) {
        openRecipeSheet(cardd.product, cardd.recipe);
      }
      return;
    }
    if (b.dataset.setsheet) {
      var setx = state.cat.sets && state.cat.sets[b.dataset.setsheet];
      if (setx) openSetSheet(setx);
      return;
    }
    if (b.dataset.setname) {
      state.naming = Number(b.dataset.setname);
      state.sessNaming = null;
      /* Раскладка, а не новые цифры (MA-INV-33): из полученного дня и сразу — иначе поле
         появлялось только с ответом сети и фокус до него не доходил. */
      repaintDay();
      var ni = document.getElementById('setname');
      if (ni) ni.focus();
      return;
    }
    if (b.dataset.setcancel) { state.naming = null; return repaintDay(); }
    /* FR-47, §6.28: кнопка группы сразу ставит на своё место форму имени. Имя другой группы
       сюда не переезжает: сменилась группа — поле пустое. */
    if (b.dataset.sessname) {
      if (state.sessNaming !== b.dataset.sessname) state.sessName = '';
      state.sessNaming = b.dataset.sessname;
      state.naming = null;
      repaintDay();
      var si = document.getElementById('sessname');
      if (si) si.focus();
      return;
    }
    if (b.dataset.sesscancel) {
      /* «Отмена» — имя забыто, на месте формы снова кнопка; фокус на неё, а не в начало страницы. */
      state.sessNaming = null; state.sessName = '';
      repaintDay();
      var back = document.querySelector('[data-sessname="' + b.dataset.sesscancel + '"]');
      if (back) back.focus();
      return;
    }
    if (b.dataset.sesssave) return saveSession(b.dataset.sesssave);
    if (b.dataset.setsave) {
      var mid = b.dataset.setsave;
      var nm = ((document.getElementById('setname') || {}).value || '').trim();
      if (!nm || state.sending) return;
      state.sending = true;
      busy('Сохраняю…');
      return send('/api/meals/' + mid + '/template', {
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: nm })
      }).then(function (r) {
        state.naming = null;
        toast('Набор «' + r.name + '» сохранён — он в «Продуктах» → «Моё».');
        renderDay();
        loadFrequent();  // чип нового набора — сразу, а не после перезапуска аппки
      }).catch(function (err) { if (!oops(err)) renderDay(); })
        .then(function () { state.sending = false; });
    }
    if (b.dataset.set) {
      if (state.sending) return;
      state.sending = true;
      busy('Записываю…');
      var sid = 'set:' + b.dataset.set;
      return send('/api/meals', {
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': keyFor(sid) },
        body: JSON.stringify({ text: b.dataset.set, days_ago: writeDay() })
      }).then(function (r) {
        dropKey(sid);
        /* FR-39.5: с «Продуктов» и AI не уводим (как «+» у продукта), на «Дне» — обычная запись
           в открытый день, тем же путём, что и чип продукта. */
        var stay = state.tab === 'ai' || state.tab === 'cat';
        return stay && r.kind === 'logged' ? logged(r, 0) : handle(r);
      }).catch(function (err) {
        oops(err);
        return rerender();
      }).then(function () { state.sending = false; });
    }
    if (b.dataset.say) {
      var inp = document.getElementById('say');
      inp.value = b.dataset.say;
      state.keepFocus = true;
      return submitText();
    }
    /* ── каталог ─────────────────────────────────────────────────────── */
    if (b.dataset.psheet) {
      var sp = (state.cat.items || {})[b.dataset.psheet] ||
               (state.cat.card && state.cat.card.product);
      if (sp && sp.stated) return statedSheet(sp);
      if (sp) productSheet(sp);
      if (sp && b.closest('.sh-chips')) { state.sheet.own = true; paintSheet(); }
      return;
    }
    if (b.dataset.sort) {
      state.cat.sort = b.dataset.sort; state.cat.offset = 0;
      return renderCatalog();
    }
    if (b.dataset.cg) { state.cat.pick = Number(b.dataset.cg); return paintProduct(); }
    if (b.dataset.cedit) { state.cat.editing = true; return paintProduct(); }
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
    if (b.id === 'saveGoal') return saveGoal();
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
    if (b.dataset.period) {
      state.period = Number(b.dataset.period);
      /* Кнопка отмечается сразу: экран перерисуется, когда придут данные, и
         до этого тап не должен выглядеть проигнорированным. */
      [].forEach.call(root.querySelectorAll('.periods button'), function (x) {
        x.setAttribute('aria-pressed', x === b ? 'true' : 'false');
      });
      return renderTotals();
    }
    if (b.dataset.ask) return askCoach(b.dataset.ask);
    if (b.id === 'send') {
      var el = document.getElementById('ask');
      var t = (el.value || '').trim(); el.value = '';
      return askCoach(t);
    }
    if (b.dataset.merge != null) return doMerge(Number(b.dataset.merge));

    if (b.dataset.grams) {
      var gi = document.getElementById('grams');
      var grams = (gi && gi.value || '').trim();
      if (!grams) { toast('Введи вес в граммах или выбери размер.', true); return; }
      return sendAnswer(grams);
    }

    if (b.dataset.wk) {
      state.weekAgo = Math.max(0, Math.min(4, (state.weekAgo || 0) + Number(b.dataset.wk)));
      return loadWeek();
    }
    if (b.dataset.scanok) { state.scan.confirmed = true; return repaintDay(); }
    if (b.dataset.scansave) return saveScan();
    if (b.dataset.scancancel) { state.scan = null; return repaintDay(); }
    if (b.dataset.scanprop) return openProposal();
    if (b.dataset.applygoal) return applyGoal();
    if (b.dataset.propclose) {
      state.proposal = null;
      var slot = document.getElementById('propslot');
      if (slot) slot.innerHTML = '';
      return state.tab === 'day' ? repaintDay() : null;
    }
    if (b.dataset.scandel) {
      var sid = b.dataset.scandel;
      return ask('Удалить этот замер? Вес в ленте останется.', function () {
        send('/api/body-scans/' + sid, { method: 'DELETE' })
          .then(function () { toast('Замер удалён.'); renderTotals(); })
          .catch(function (err) { toast(err.message, true); });
      });
    }
    if (b.dataset.pdrop) {
      state.photo.items[Number(b.dataset.pdrop)].off = true;
      state.photo.base = null;
      return repaintDay();
    }
    if (b.dataset.pback) {
      state.photo.items[Number(b.dataset.pback)].off = false;
      state.photo.base = null;
      return repaintDay();
    }
    if (b.dataset.pcommit) return commitPhoto();
    if (b.dataset.pcancel) { state.photo = null; return repaintDay(); }

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
    if (!pick || pick.getAttribute('aria-disabled') === 'true') return;
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
    if (pick.dataset.bmr) {
      return patch('/api/settings/prefs', { bmr_source: pick.dataset.bmr }, 'Источник обмена изменён.');
    }
    if (pick.dataset.activity) {
      return patch('/api/settings/prefs', { activity: pick.dataset.activity }, 'Активность изменена.');
    }
    if (pick.dataset.ailimit) return saveLimit(pick);
    if (pick.dataset.fatgoal != null) {
      /* Отметка, а не новые данные: переключаем на месте и не перерисовываем
         экран — тап по настройке не должен мигать (MA-INV-32). */
      [].forEach.call(root.querySelectorAll('.pick[data-fatgoal]'), function (x) {
        var on = x === pick;
        x.classList.toggle('on', on);
        x.setAttribute('aria-checked', on ? 'true' : 'false');
      });
      return patch('/api/settings/prefs',
        { target_fat_pct: pick.dataset.fatgoal === '' ? null : Number(pick.dataset.fatgoal) },
        'Цель по проценту жира изменена.', true);
    }
    if (pick.dataset.daystart) {
      state.lastDay = null;   // границы дня сменились — старый день показывать нельзя
      return patch('/api/settings/prefs',
        { day_start: Number(pick.dataset.daystart) }, 'Граница суток изменена.');
    }
    if (pick.dataset.defperiod) {
      state.period = null;
      return patch('/api/settings/prefs',
        { period: Number(pick.dataset.defperiod) }, 'Период изменён.');
    }
    if (pick.dataset.group == null) return;
    state.cat.dup = state.cat.dup || {};
    state.cat.dup[pick.dataset.group] = Number(pick.dataset.pick);
    /* Выбор — это отметка, а не новые данные: переключаем на месте, без
       перезагрузки экрана (раньше каждый тап мигал скелетом). */
    [].forEach.call(root.querySelectorAll('.pick[data-group="' + pick.dataset.group + '"]'),
      function (x) {
        var on = x === pick;
        x.classList.toggle('on', on);
        x.setAttribute('aria-checked', on ? 'true' : 'false');
      });
  });

  function saveProduct() {
    var p = state.cat.card && state.cat.card.product;
    var body = { name: document.getElementById('pname').value.trim() };
    /* КБЖУ шлём, только если их правда поменяли. Иначе переименование переписывало бы
       плотность округлёнными до 0,1 числами из полей (MA-INV-72). */
    var ids = { pk: 'kcal_per_100g', pp: 'protein_per_100g', pf: 'fat_per_100g', pc: 'carb_per_100g' };
    var changed = Object.keys(ids).some(function (id) {
      var shown = p ? Math.round(Number(p[ids[id]]) * 10) / 10 : NaN;
      return Number(document.getElementById(id).value) !== shown;
    });
    if (changed) {
      Object.keys(ids).forEach(function (id) {
        body[ids[id]] = Number(document.getElementById(id).value);
      });
    }
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
    if (e.key === 'Enter' && e.target.id === 'setname') {
      e.preventDefault();
      var sv = document.querySelector('[data-setsave]');
      if (sv) sv.click();
    }
    /* Escape — «Отмена» у поля имени набора (записи и приёма пищи). */
    if (e.key === 'Escape' && (e.target.id === 'sessname' || e.target.id === 'setname')) {
      e.preventDefault();
      var cx = document.querySelector(e.target.id === 'sessname' ? '[data-sesscancel]' : '[data-setcancel]');
      if (cx) cx.click();
      return;
    }
    if (e.key === 'Enter' && e.target.id === 'sessname') {
      e.preventDefault();
      var ss = document.querySelector('[data-sesssave]');
      if (ss) ss.click();
    }
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
    /* Имя набора приёма пищи переживает перерисовку дня (фоновую или после отказа). */
    if (e.target.id === 'sessname') { state.sessName = e.target.value; return; }
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
    if (e.target.id === 'ptotal' || (e.target.dataset && e.target.dataset.pg)) {
      return photoInput(e.target);
    }
    if (e.target.dataset && e.target.dataset.sf && state.scan) {
      state.scan.edits[e.target.dataset.sf] = e.target.value;
      return;
    }
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

  /* Перепроверка бланка — на уходе из поля, а не на каждой цифре: иначе
     перерисовка выбивала бы курсор посреди ввода. */
  root.addEventListener('change', function (e) {
    if (e.target.dataset && e.target.dataset.sf && state.scan) {
      state.scan.edits[e.target.dataset.sf] = e.target.value;
      recheckScan();
    }
  });

  /* Слушатель на скрытом поле, а не на кнопке: кнопка живёт внутри #screen и
     пересоздаётся на каждой перерисовке, поле — снаружи и переживает их. */
  document.getElementById('shot').addEventListener('change', function (e) {
    var f = e.target.files && e.target.files[0];
    if (f) sendPhoto(f);
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
  /* ── быстрый старт (§5.13) ──────────────────────────────────────────── */

  /* MA-INV-74: сохранённый день рисуется сразу, пока свежий в пути. Ключ — на
     человека (телефон бывает общим) и на версию фронта: данные, сохранённые
     старым кодом, новый не рисует — форма ответа могла смениться. Без
     localStorage (приватный режим) всё работает, просто без мгновенного кадра. */
  function bootKey() {
    var u = tg.initDataUnsafe && tg.initDataUnsafe.user;
    return u && u.id ? 'boot:' + V + ':' + u.id : null;
  }

  function readBoot() {
    var k = bootKey();
    if (!k) return null;
    try { return JSON.parse(localStorage.getItem(k) || 'null'); }
    catch (e) { return null; }
  }

  function saveBoot(d) {
    var k = bootKey();
    if (!k || !state.tz) return;   /* без пояса сохранённое не сверить с сегодня */
    var id = k.split(':').pop();
    try {
      /* Записи того же человека от прошлых версий — мусор: прочесть их уже нельзя. */
      for (var i = localStorage.length - 1; i >= 0; i--) {
        var o = localStorage.key(i);
        if (o && o !== k && o.indexOf('boot:') === 0 && o.split(':').pop() === id) {
          localStorage.removeItem(o);
        }
      }
      localStorage.setItem(k, JSON.stringify({
        at: Date.now(), day: d, frequent: state.frequent || null, sets: state.sets || null,
        settings: { prefs: state.prefs || {}, timezone: state.tz } }));
    } catch (e) { /* переполнено или запрещено — переживём */ }
  }

  /* Сохранённый день годится, только если он и сейчас «сегодня» человека: по его
     поясу и с его началом дня (§6.5, `day_start`). Вчерашний день под заголовком
     «Сегодня» — неверные цифры, а это хуже секунды ожидания. Пояс неизвестен или
     клиент его не понимает — не рисуем. Чистая функция: её проверяет
     `node web/check.mjs`. */
  function bootCache(saved, now) {
    if (!saved || !saved.day || !saved.settings || saved.day.days_ago !== 0) return null;
    var tz = saved.settings.timezone;
    if (!tz) return null;
    var start = Number((saved.settings.prefs || {}).day_start) || 0;
    var p = {};
    try {
      new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit',
                                         day: '2-digit' })
        .formatToParts(new Date(now - start * 3600000))
        .forEach(function (x) { p[x.type] = x.value; });
    } catch (e) { return null; }
    return saved.day.day === p.year + '-' + p.month + '-' + p.day ? saved : null;
  }

  /* Что делать со свежим днём, пришедшим в фоне. Чистая функция: каждую ветку
     проверяет `node web/check.mjs`.
     skip   — день уже перерисовало действие человека: наш ответ старше.
     store  — запомнить, не рисовать: человек на другом экране или дне, либо идёт
              запись — её собственная перерисовка и покажет (MA-INV-32).
     undim  — то же, что на экране: снять приглушение, разметку не трогать.
     around — человек печатает: перерисовать всё, кроме поля ввода.
     paint  — перерисовать поверх, без скелета. */
  function shouldRepaint(o) {
    if (!o.untouched) return 'skip';
    if (o.tab !== 'day' || o.daysAgo !== 0 || o.busy) return 'store';
    if (o.same) return 'undim';
    if (o.typing) return 'around';
    return 'paint';
  }

  function applySettings(s) {
    state.prefs = s.prefs || {};
    /* Настройка обязана иметь поведение за собой (MA-INV-31). Раньше здесь
       значение просто присваивалось и не читалось нигде — переключатель
       выглядел рабочим и не делал ничего. */
    state.micFirst = !!state.prefs.mic_first;
    state.tz = s.timezone;
  }

  /* Свежий день пришёл в фоне: решить и сделать. `base` — день, что был на
     экране, когда старт ушёл в сеть; `same` — свежий не отличается от видимого. */
  function settle(d, base, same) {
    var act = shouldRepaint({
      untouched: state.lastDay === base || state.lastDay === d,
      tab: state.tab, daysAgo: state.daysAgo,
      /* Поле имени набора в фокусе — тоже занятость: перерисовка закрыла бы клавиатуру iOS
         и сбила набор через IME. Свежий день запомним и покажем следующей перерисовкой. */
      busy: !!(state.sending || state.recording || naming()),
      same: same, typing: typing()
    });
    if (act === 'skip') return;
    saveBoot(d);
    if (act === 'store') {
      state.lastDay = d; state.shownDay = 0;
      /* Запись упала, не перерисовав, — приглушение не должно остаться навсегда:
         дождёмся конца записи и решим заново. */
      if (state.tab === 'day' && (root.classList.contains('stale') || root.querySelector('.skel'))) {
        setTimeout(function () { settle(d, base, same); }, 300);
      }
      return;
    }
    state.staleDay = null;
    if (act === 'undim') { state.lastDay = d; return markStale(); }
    paintDay(d, act === 'around');
  }

  /* MA-INV-78: экран входа по кнопке бота (`?s=`). Белый список явными ветками, без
     поиска по объекту: иначе `constructor` нашёлся бы в прототипе. Id только выбирает
     экран: каталог общий, данные те же, что в списке; в разметку id не попадает.
     Всё, что не совпало целиком, — «День».
     Чистая функция: ветки проверяет `node web/check.mjs`. */
  function landing(search) {
    var m = /(?:^\?|&)s=([^&]*)/.exec(search || '');
    var v = m ? m[1] : '';
    var id = /^(cat|recipe)-([1-9][0-9]{0,9})$/.exec(v);
    if (id) return { tab: 'cat', product: id[2], recipe: id[1] === 'recipe' };
    if (v === 'cat') return { tab: 'cat' };
    if (v === 'dupes') return { tab: 'cat', dups: true };
    if (v === 'tot') return { tab: 'tot' };
    if (v === 'tot-week') return { tab: 'tot', period: 7 };
    if (v === 'body') return { tab: 'tot', land: 'comp' };
    if (v === 'set') return { tab: 'set' };
    return { tab: 'day' };
  }

  /* Что делать на старте с экраном входа и сохранённым днём (MA-INV-74, MA-INV-78).
     paint    — сохранённый день, который рисуем сразу: только при входе на «День»,
                на другом экране он мигнул бы чужим кадром;
     enter    — открыть экран входа сразу, не дожидаясь дня;
     dayError — ошибка стартового дня показывается на экране: только на «Дне»,
                у экрана входа своя загрузка и свои ошибки.
     Чистая функция: ветки проверяет `node web/check.mjs`. */
  function bootPlan(to, saved) {
    var day = to.tab === 'day';
    return { paint: day && saved ? saved : null, enter: !day, dayError: day };
  }

  /* Открыть экран входа тем же состоянием, что оставили бы тапы по вкладке и по
     кнопке экрана: «назад» у карточки и дублей ведёт к списку, как обычно. Настройки
     живут под «Днём» (шестерёнка) — подсвечена его вкладка. */
  function enter(to) {
    state.tab = to.tab;
    if (to.tab === 'cat') {
      state.cat = { q: '', offset: 0, view: to.dups ? 'dups' : to.product ? 'product' : 'list',
                    product: to.product || null, dup: null, recipe: !!to.recipe };
    }
    if (to.period) state.period = to.period;
    state.land = to.land || null;
    var lit = to.tab === 'set' ? 'day' : to.tab;
    [].forEach.call(nav.querySelectorAll('button'), function (x) {
      if (x.dataset.tab === lit) x.setAttribute('aria-current', 'page');
      else x.removeAttribute('aria-current');
    });
    render();
  }

  /* MA-INV-75: настройки, день и частое уходят разом — раньше день ждал
     настроек, и холодный старт стоил два оборота подряд вместо одного. */
  function boot() {
    var to = landing(location.search);
    var plan = bootPlan(to, bootCache(readBoot(), Date.now()));
    var saved = plan.paint;
    /* Настройки применяются, как только пришли, а не вместе с днём: упавший день
       не должен выбросить удачные настройки — без пояса не сохранится и день. */
    var prefsWas = null;
    var settingsReq = api('/api/settings').then(function (s) {
      prefsWas = JSON.stringify(state.prefs);
      var period = activePeriod();
      if (!state.settingsEdited) applySettings(s);
      /* MA-INV-78: «Итоги», открытые до настроек, взяли период из сохранённого или
         30. Другой период по умолчанию — перерисовать, если явно его не выбирали. */
      if (state.tab === 'tot' && !state.period && activePeriod() !== period) {
        state.land = to.land || null;
        renderTotals();
      }
    }).catch(function () {
      if (prefsWas === null) prefsWas = JSON.stringify(state.prefs);
    }).then(function () {
      if (!state.prefs) state.prefs = {};   /* не пришли — вид по умолчанию */
    });
    var dayReq = api('/api/day?days_ago=0');
    loadFrequent();
    document.getElementById('gear').setAttribute('aria-pressed', false);
    if (saved) {
      try {
        applySettings(saved.settings);
        if (saved.frequent) state.frequent = saved.frequent;
        if (saved.sets) state.sets = saved.sets;
        state.staleDay = saved.day;
        paintDay(saved.day);
      } catch (e) { saved = null; state.lastDay = state.staleDay = null; }  /* как без кэша */
    }
    if (plan.enter) {
      /* MA-INV-78: вид — из сохранённого, если есть (период «Итогов» и прочее); день
         из него не рисуем, свежий в фоне только запомнится (shouldRepaint → store). */
      var raw = readBoot();
      try { if (raw && raw.settings) applySettings(raw.settings); } catch (e) { /* как без кэша */ }
      enter(to);
    }
    /* Без сохранённого — скелет, пока не придут И день, И настройки: иначе
       панель мигнёт видом по умолчанию и только потом переключится на выбранный. */
    else if (!saved) loadingDay();
    var base = state.lastDay;
    /* «День» без сохранённого по-прежнему рисуется, когда пришли оба (MA-INV-75). */
    Promise.all([settingsReq, dayReq]).then(function (r) {
      var d = r[1];
      settle(d, base, !!saved && JSON.stringify(state.prefs) === prefsWas &&
                      JSON.stringify(d) === JSON.stringify(saved.day));
    }).catch(function (e) {
      if (!plan.dayError) return;   /* экран входа грузит и ругается сам (MA-INV-78) */
      if (!saved || e.code === 'init_data_expired' || e.code === 'inactive') return failure(e);
      /* Сохранённый день остаётся, но подписан своим временем — до следующей
         удачной загрузки. Тост исчез бы, а цифры продолжили бы выглядеть текущими. */
      state.staleDay = null;
      state.offline = { day: saved.day, at: saved.at };
      if (state.tab === 'day' && state.daysAgo === 0 && state.lastDay === saved.day) {
        paintDay(saved.day, typing());
      }
    });
  }

  boot();
  if (tg.setHeaderColor) { try { tg.setHeaderColor('#000000'); } catch (e) { /* старый клиент */ } }
})();
