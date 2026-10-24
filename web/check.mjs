/* Ловит обращения к необъявленным идентификаторам во фронте.
 *
 * `node --check` проверяет только синтаксис: `HEROES.map(...)` для него валиден,
 * а в браузере это ReferenceError. Ошибка живёт до тех пор, пока человек не
 * откроет конкретный экран — так экран настроек уехал в прод мёртвым.
 *
 * ponytail: грубый разбор регулярками вместо AST и eslint. Потолок известен —
 * ловит только идентификаторы В_ВЕРХНЕМ_РЕГИСТРЕ и `function имя`, строки и
 * комментарии вырезает приблизительно. Понадобится больше — eslint с no-undef.
 */
import { readFileSync } from 'node:fs';

const src = readFileSync(new URL('./app.js', import.meta.url), 'utf8');

// Убираем строки, шаблоны, регулярки и комментарии: внутри них «слова» не код.
const code = src
  .replace(/\/\*[\s\S]*?\*\//g, ' ')
  .replace(/(^|[^:])\/\/[^\n]*/g, '$1 ')
  .replace(/'(?:[^'\\\n]|\\.)*'/g, "''")
  .replace(/"(?:[^"\\\n]|\\.)*"/g, '""')
  .replace(/`(?:[^`\\]|\\.)*`/g, '``');

const BUILTIN = new Set([
  'JSON', 'NaN', 'Infinity', 'URLSearchParams', 'DOMParser', 'FormData',
  'Promise', 'Math', 'Date', 'Number', 'String', 'Array', 'Object', 'Blob',
  'MediaRecorder', 'AbortController', 'Error', 'RegExp', 'Map', 'Set',
  'URL',
]);

const declared = new Set([
  ...[...code.matchAll(/\bvar\s+([A-Za-z_$][\w$]*)/g)].map((m) => m[1]),
  ...[...code.matchAll(/\bfunction\s+([A-Za-z_$][\w$]*)/g)].map((m) => m[1]),
  ...[...code.matchAll(/\b(?:let|const)\s+([A-Za-z_$][\w$]*)/g)].map((m) => m[1]),
  // параметры функций: грубо, но достаточно
  ...[...code.matchAll(/\bfunction\s*[\w$]*\s*\(([^)]*)\)/g)]
    .flatMap((m) => m[1].split(',').map((s) => s.trim()).filter(Boolean)),
]);

// Только КОНСТАНТЫ_МОДУЛЯ: у них нет шансов оказаться свойством или параметром.
const used = new Set([...code.matchAll(/\b([A-Z][A-Z0-9_]{2,})\b(?!\s*:)/g)].map((m) => m[1]));

const missing = [...used].filter((n) => !declared.has(n) && !BUILTIN.has(n));

const problems = [];
if (missing.length) {
  problems.push(
    'Необъявленные идентификаторы: ' + missing.join(', ') +
    '\n  В браузере это ReferenceError на том экране, где они используются.'
  );
}

/* ── Вёрстка: ловушки, которые сходятся ровно при одной ширине ────────────
 *
 * Все три уже случались в этом проекте, и каждый раз баг доживал до прода,
 * потому что я смотрел на одном размере окна. Обещание «проверять обе
 * крайности» не работает — проверка работает.
 */
const css = readFileSync(new URL('./app.css', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, ' ');       // комментарии не правила

const rules = [
  {
    // transform не меняет место элемента в потоке, поэтому его сочетают с
    // процентными width/margin — и подгоняют под текущую ширину окна.
    re: /\{[^}]*transform:\s*scale\([^)]*\)[^}]*\}/g,
    bad: /(width|margin[a-z-]*)\s*:\s*-?\d+(\.\d+)?%/,
    why: 'transform:scale вместе с процентной width/margin. Эта арифметика ' +
         'сходится при одной ширине окна, на другой блок наползает на соседей. ' +
         'Задай размер в пикселях или откажись от масштабирования.',
  },
  {
    // Отрицательный процентный отступ растёт вместе с шириной родителя.
    re: /\{[^}]*\}/g,
    bad: /margin[a-z-]*\s*:\s*[^;}]*-\d+(\.\d+)?%/,
    why: 'отрицательный отступ в процентах: он растёт вместе с шириной ' +
         'родителя и утаскивает блок тем сильнее, чем шире окно.',
  },
  {
    // Аппка должна занимать окно, как у соседнего проекта.
    re: /(^|\})\s*body\s*\{[^}]*\}/g,
    bad: /max-width\s*:\s*(?!none)/,
    why: 'max-width на body: аппка перестаёт занимать окно и на десктопе ' +
         'превращается в узкую полоску среди пустоты.',
  },
];

/* ── Ключ записи выдаётся на действие, а не на клик (§7.5) ────────────────
 *
 * Спека предупреждает об этом дословно: «ключ, созданный на клике, у двух
 * кликов будет разный, и защита не сработает». Ровно так и было сделано — и
 * двойной тап по чипу «частого» писал два приёма с удвоенными калориями.
 * Ключ обязан приходить из `keyFor(...)`, который помнит его до успеха.
 */
const noComments = src.replace(/\/\*[\s\S]*?\*\//g, ' ');
for (const m of noComments.matchAll(/'Idempotency-Key':\s*([^,\n}]+)/g)) {
  const value = m[1].trim();
  if (!/^(keyFor\(|key\b)/.test(value)) {
    problems.push(
      'Ключ записи создаётся на отправке: `Idempotency-Key: ' + value + '`. ' +
      'У двух кликов он будет разный, и двойной тап запишет дважды — бери из keyFor(id).'
    );
  }
}

/* ── Верхний отступ: молчание клиента ≠ его ноль ──────────────────────────
 *
 * Ноль от клиента значит «отступ не нужен, контент уже под моей шапкой» — это
 * обычный, не полноэкранный режим. Отсутствие величин значит «клиент старый».
 * 05.09.2026 ноль приняли за молчание, сверху лёг env(safe-area-inset-top), и
 * под шапкой Telegram выросла вторая пустая полоса. Ловим поведением, а не
 * присутствием строк: правильные слова в комментарии ничего не гарантируют.
 */
const fn = src.match(/function topInset\([\s\S]*?\n  \}/);
if (!fn) {
  problems.push('Не нашёл function topInset — на нём держится верхний отступ.');
} else {
  const topInset = new Function('return ' + fn[0])();
  const cases = [
    [[undefined, undefined], null, 'клиент не знает величин → остаёмся на env()'],
    [[{ top: 0 }, { top: 0 }], 0, 'клиент говорит «не нужен» → ровно ноль, без env()'],
    [[{ top: 48 }, { top: 56 }], 104, 'полный экран → вырез плюс шапка Telegram'],
    [[{ top: 0 }, undefined], 0, 'знает про вырез, не знает про шапку → 0'],
    [[undefined, { top: 56 }], 56, 'знает только про шапку'],
  ];
  for (const [args, want, why] of cases) {
    const got = topInset(...args);
    if (got !== want) {
      problems.push(`Верхний отступ: ${why} — ожидалось ${want}, получено ${got}.`);
    }
  }
}

/* ── Сохранённый день — только сегодняшний (MA-INV-74) ───────────────────
 *
 * Аппка рисует последний день из localStorage до ответа сервера. Вчерашний
 * день под заголовком «Сегодня» — это неверные цифры, а точность учёта тут
 * главное. «Сегодня» считается как на сервере: по поясу человека и с его
 * началом дня (`local_date` в app/domain/timeframe.py).
 */
const bc = src.match(/function bootCache\([\s\S]*?\n  \}/);
if (!bc) {
  problems.push('Не нашёл function bootCache — на нём держится мгновенный старт.');
} else {
  const bootCache = new Function('return ' + bc[0])();
  const saved = (day, tz, dayStart, ago = 0) =>
    ({ day: { day, days_ago: ago }, settings: { timezone: tz, prefs: { day_start: dayStart } } });
  const at = Date.parse('2026-10-24T20:30:00Z');   // в Москве 23:30, в GMT+7 уже 03:30 25-го
  const cases = [
    [saved('2026-10-24', 'Europe/Moscow', 0), true, 'тот же день по поясу человека'],
    [saved('2026-10-23', 'Europe/Moscow', 0), false, 'вчерашний день не рисуется как сегодня'],
    [saved('2026-10-24', 'Etc/GMT-7', 0), false, 'у человека в GMT+7 уже наступило 25-е'],
    [saved('2026-10-24', 'Etc/GMT-7', 4), true, 'день с 04:00: в 03:30 ещё 24-е'],
    [saved('2026-10-25', 'Etc/GMT-7', 0), true, 'сегодня по его поясу, а не по нашему'],
    [saved('2026-10-24', 'Europe/Moscow', 0, 1), false, 'прошлый день из ленты — не стартовый'],
    [saved('2026-10-24', undefined, 0), false, 'пояс неизвестен — не сверить, не рисуем'],
    [saved('2026-10-24', 'Нет/Такого', 0), false, 'пояс клиенту непонятен — не рисуем'],
    [null, false, 'сохранённого нет'],
  ];
  for (const [arg, want, why] of cases) {
    const got = !!bootCache(arg, at);
    if (got !== want) problems.push(`Сохранённый день: ${why} — ожидалось ${want}, получено ${got}.`);
  }
}

/* ── Фоновый свежий день: рисовать или нет (MA-INV-74, MA-INV-32) ─────────
 *
 * Свежий день приходит, пока человек уже что-то делает. Каждая ветка — чей-то
 * реальный сбой: старый ответ поверх записи вернул бы прошлое, перерисовка во
 * время записи стёрла бы экран, пересозданное поле ввода сбило бы набор.
 */
const sr = src.match(/function shouldRepaint\([\s\S]*?\n  \}/);
if (!sr) {
  problems.push('Не нашёл function shouldRepaint — на нём держится фоновое обновление.');
} else {
  const shouldRepaint = new Function('return ' + sr[0])();
  const base = { untouched: true, tab: 'day', daysAgo: 0, busy: false, same: false, typing: false };
  const cases = [
    [{}, 'paint', 'ничего не мешает → перерисовать поверх'],
    [{ untouched: false }, 'skip', 'день уже перерисовало действие → старый ответ не рисуем'],
    [{ untouched: false, tab: 'set' }, 'skip', 'перерисованное действием не трогаем и с другой вкладки'],
    [{ tab: 'set' }, 'store', 'человек в настройках → только запомнить'],
    [{ daysAgo: 1 }, 'store', 'человек листает вчера → только запомнить'],
    [{ busy: true }, 'store', 'идёт запись → рисует она сама (MA-INV-32)'],
    [{ busy: true, typing: true }, 'store', 'запись важнее набора'],
    [{ same: true }, 'undim', 'то же, что на экране → снять приглушение, разметку не трогать'],
    [{ same: true, typing: true }, 'undim', 'то же и печатает → тоже только снять приглушение'],
    [{ typing: true }, 'around', 'печатает → всё, кроме поля ввода'],
  ];
  for (const [over, want, why] of cases) {
    const got = shouldRepaint({ ...base, ...over });
    if (got !== want) problems.push(`Фоновый день: ${why} — ожидалось ${want}, получено ${got}.`);
  }
}

/* ── Вход по кнопке бота: только белый список (MA-INV-78) ────────────────
 *
 * Бот открывает аппку на нужном экране адресом `?s=…`. Адрес — чужой ввод:
 * его можно переписать руками. Всё, что не совпало целиком, — «День», а id
 * берётся только цифрами без ведущих нулей.
 */
const ld = src.match(/function landing\([\s\S]*?\n  \}/);
if (!ld) {
  problems.push('Не нашёл function landing — на нём держится вход из бота на экран.');
} else {
  const landing = new Function('return ' + ld[0])();
  const day = { tab: 'day' };
  const cases = [
    ['', day, 'без параметра — «День»'],
    ['?v=abc', day, 'только отпечаток — «День»'],
    ['?v=abc&s=day', day, '«День» явно'],
    ['?v=abc&s=cat', { tab: 'cat' }, 'каталог'],
    ['?s=cat-7&v=abc', { tab: 'cat', product: '7', recipe: false }, 'карточка продукта, s первым'],
    ['?v=abc&s=recipe-42', { tab: 'cat', product: '42', recipe: true }, 'состав рецепта'],
    ['?v=abc&s=dupes', { tab: 'cat', dups: true }, 'дубли'],
    ['?v=abc&s=tot', { tab: 'tot' }, 'итоги'],
    ['?v=abc&s=tot-week', { tab: 'tot', period: 7 }, 'итоги за неделю'],
    ['?v=abc&s=body', { tab: 'tot', land: 'comp' }, 'состав тела'],
    ['?v=abc&s=set', { tab: 'set' }, 'настройки'],
    ['?s=foo', day, 'неизвестный экран'],
    ['?s=ai', day, 'вкладка, которой нет в списке входа'],
    ['?s=CAT', day, 'регистр не угадываем'],
    ['?s=constructor', day, 'имя из прототипа объекта'],
    ['?s=__proto__', day, 'имя из прототипа объекта'],
    ['?s=cat-0', day, 'id ноль'],
    ['?s=cat-01', day, 'ведущий ноль'],
    ['?s=cat-1e3', day, 'id не число'],
    ['?s=cat-7x', day, 'хвост за id'],
    ['?s=cat-12345678901', day, 'id длиннее 10 цифр'],
    ['?s=recipe', day, 'рецепт без id'],
    ['?s=tot-x', day, 'неизвестный раздел итогов'],
    ['?s=cat%2D7', day, 'закодированное не раскодируем'],
    ['?s=cat-7%3Cb%3E', day, 'разметка в id'],
    ['?xs=cat', day, 'чужой параметр с тем же хвостом'],
  ];
  for (const [arg, want, why] of cases) {
    const got = JSON.stringify(landing(arg));
    if (got !== JSON.stringify(want)) {
      problems.push(`Вход из бота: ${why} (${arg || 'пусто'}) — ожидалось ` +
                    `${JSON.stringify(want)}, получено ${got}.`);
    }
  }
}

/* ── Старт с экрана входа не трогает «День» (MA-INV-74, MA-INV-78) ───────
 *
 * Сохранённый день рисуется только при входе на «День»: на каталоге он мигнул
 * бы чужим кадром (AC-173). Ошибка стартового дня на экране входа не
 * показывается — она затёрла бы открытый каталог (AC-174).
 */
const bp = src.match(/function bootPlan\([\s\S]*?\n  \}/);
if (!bp) {
  problems.push('Не нашёл function bootPlan — на нём держится старт с экрана входа.');
} else {
  const bootPlan = new Function('return ' + bp[0])();
  const S = { day: { day: '2026-10-24', days_ago: 0 } };
  const cases = [
    [{ tab: 'day' }, S, { paint: S, enter: false, dayError: true }, '«День» с сохранённым — рисуем сразу'],
    [{ tab: 'day' }, null, { paint: null, enter: false, dayError: true }, '«День» без сохранённого — скелет'],
    [{ tab: 'cat' }, S, { paint: null, enter: true, dayError: false }, 'каталог: сохранённый день не рисуем (AC-173)'],
    [{ tab: 'cat', product: '7', recipe: true }, S, { paint: null, enter: true, dayError: false }, 'рецепт: то же'],
    [{ tab: 'tot', land: 'comp' }, null, { paint: null, enter: true, dayError: false }, 'итоги: ошибка дня не затирает экран (AC-174)'],
    [{ tab: 'set' }, S, { paint: null, enter: true, dayError: false }, 'настройки: то же'],
  ];
  for (const [to, saved, want, why] of cases) {
    const got = bootPlan(to, saved);
    const ok = got.paint === want.paint && got.enter === want.enter && got.dayError === want.dayError;
    if (!ok) {
      problems.push(`Старт: ${why} — ожидалось ${JSON.stringify(want)}, получено ${JSON.stringify(got)}.`);
    }
  }
}

/* ── Верхний вырез ────────────────────────────────────────────────────────
 *
 * Нижний `env(safe-area-inset-bottom)` был в четырёх местах, верхнего не было
 * нигде — и шапка уехала под часы и статус-бар (05.09.2026, скриншот с телефона).
 * Правило простое: у body обязан быть верхний отступ с учётом выреза.
 */
const bodyRule = (css.match(/(^|\})\s*body\s*\{[^}]*\}/g) || []).join(' ');
if (!/padding-top/.test(bodyRule) ||
    !/(--top-inset|safe-area-inset-top)/.test(bodyRule)) {
  problems.push(
    'Нет верхнего отступа под вырез: у body должен быть padding-top с ' +
    '--top-inset / env(safe-area-inset-top). Без него шапка лезет под часы ' +
    'и под собственную панель Telegram.'
  );
}

/* ── hidden обязан прятать ────────────────────────────────────────────────
 *
 * Атрибут hidden — это встроенный display:none, и любой display из своих стилей
 * его перебивает. Тост с кнопкой «Отменить» получил display:flex и перестал
 * исчезать (14.10.2026). Держим глобальное правило и не даём его потерять.
 */
if (!/\[hidden\]\s*\{\s*display\s*:\s*none\s*!important/.test(css)) {
  problems.push('Нет [hidden]{display:none!important}: элемент со своим display ' +
                'не прячется по hidden (тост «Отменить» висел вечно).');
}

/* ── Мутация не стирает экран ─────────────────────────────────────────────
 *
 * `loading()` заменяет весь #screen скелетом. На чтении это уместно, на записи
 * — нет: высота схлопывается с ленты до одной карточки, страница прыгает, и
 * человек видит два разных скелета подряд (свой и от следующей перерисовки).
 * Занятость при записи показывает `busy()` — на месте, без потери разметки.
 */
const bare = src.replace(/\/\*[\s\S]*?\*\//g, ' ');
for (const m of bare.matchAll(/loading\(\)\s*;/g)) {
  const after = bare.slice(m.index, m.index + 400);
  if (/\bsend\s*\(/.test(after)) {
    problems.push(
      'Мутация стирает экран: loading() перед send(). Скелет схлопывает ' +
      'высоту и подбрасывает прокрутку — покажи занятость через busy().' +
      '\n  ' + after.replace(/\s+/g, ' ').slice(0, 110)
    );
  }
}

for (const { re, bad, why } of rules) {
  for (const block of css.match(re) || []) {
    if (bad.test(block)) {
      problems.push('Вёрстка: ' + why + '\n  ' + block.replace(/\s+/g, ' ').slice(0, 110));
    }
  }
}

if (problems.length) {
  problems.forEach((p) => console.error('• ' + p));
  process.exit(1);
}
console.log('web: идентификаторы объявлены, ловушек вёрстки нет');
