"""Общий кит виджетов (D-83, D-84) — одно место для фона и движения, как `engine` finance_bot.

Шаблоны рукописные, кит подставляется метками при сборке (`widgets.template`):
- `{{sheen}}` — отлив: зелёный свет в правом верхнем углу поверх градиента фона. Оттенок —
  `--ok` аппки (#34C759, единственный зелёный токен; акцент аппки синий, иконки бота в репо нет),
  сила как у finance_bot: 14 % в светлой теме, 18 % в тёмной (24/2E в альфе).
- `{{boot}}` — пролог: анимация один раз, когда виджет впервые на экране (`isActive`/`activated`),
  класс `an` на `<body>`; бегущий счёт чисел из `data-n` (вопрос владельцу OQ-W9); хук `AR` —
  дуги колец по кадрам. Любой тап снимает `an` (листание и раскрытие строки не проигрывают
  появление заново), а когда все CSS-анимации кончились (`animationend`), класс снимается сам:
  на экране ровно то, что без анимации (в Telegram на iOS незавершённая анимация застывала).
"""
from __future__ import annotations

from app.bot.widgets.icons import inline

SHEEN = "radial-gradient(130% 80% at 100% 0,light-dark(#34C75924,#34C7592E),#0000 62%),"

BOOT = (
    "let A1=1,AK=0;const TW=window.TelegramWidget,BD=document.body,"
    "NU=v=>'<span class=cu data-n='+v+'>'+v+'</span>',A0=()=>{A1=0;BD.className=''},"
    "AN=()=>{const v=!TW||TW.isActive!==!1;BD.className=A1&&v?'an':'';if(A1&&v){A1=0;"
    "try{requestAnimationFrame(t0=>{const e=document.querySelectorAll('.cu'),f=t=>{"
    "const k=Math.min(1,(t-t0)/900),q=1-(1-k)**3;"
    "e.forEach(x=>x.textContent=Math.round(x.dataset.n*q));"
    "window.AR&&AR(q);k<1&&requestAnimationFrame(f)};f(t0)})}catch(e){}}};"
    "try{TW.onEvent('activated',()=>{A1&&(window.RD||draw)()});TW.setResizeAnchor('bottom')}catch(e){}"
    "addEventListener('click',A0,!0);addEventListener('animationstart',()=>AK++);"
    "addEventListener('animationend',()=>{--AK<1&&(AK=0,A0())});\n"
)


def build(html: str) -> str:
    """Метки кита → код; затем иконки (`{{ic:…}}`)."""
    return inline(html.replace("{{sheen}}", SHEEN).replace("{{boot}}", BOOT))
