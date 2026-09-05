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
