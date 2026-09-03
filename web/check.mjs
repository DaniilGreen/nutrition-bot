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

if (missing.length) {
  console.error('Необъявленные идентификаторы во фронте:', missing.join(', '));
  console.error('В браузере это ReferenceError на том экране, где они используются.');
  process.exit(1);
}
console.log('web/app.js: необъявленных идентификаторов нет');
