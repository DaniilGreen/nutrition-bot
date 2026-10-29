"""OpenAI vision-адаптер: фото этикетки → КБЖУ/100г (FR-27). Реализует VisionPort.

Читает таблицу пищевой ценности мультиязычно (рус/англ/вьетнам) и переводит вес «на порцию»
в «на 100 г». Финальные числа считает приложение (INV-1) — это лишь извлечение плотности.
"""
from __future__ import annotations

import base64
import logging
from typing import Literal, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.application.dto import ParsedItem, PlateReading, ScanReading, Unit
from app.domain.menu import MenuLine
from app.domain.plate import FAT_KINDS, MAX_COMPONENTS, Component
from app.infrastructure.openai_compat import temperature_for, token_counts

log = logging.getLogger(__name__)

_Out = TypeVar("_Out", bound=BaseModel)

# ponytail: потолок позиций со скриншота меню (FR-38). Больше — скорее страница каталога, чем
# заказ; обрезка лишь страхует от записи всего меню разом.
MAX_MENU = 8

SYSTEM_PROMPT = (
    "Ты читаешь таблицу пищевой ценности (КБЖУ) с фото этикетки продукта.\n"
    "Верни значения НА 100 Г: ккал, белки, жиры, углеводы (граммы).\n"
    "Этикетка может быть на любом языке. Вьетнам: Năng lượng/Calo = ккал, Chất đạm = белки,\n"
    "Chất béo = жиры, Carbohydrate/Tinh bột/Đường = углеводы. Англ.: energy/protein/fat/carb.\n"
    "Если значения даны НА ПОРЦИЮ (serving/khẩu phần), а не на 100 г — ПЕРЕСЧИТАЙ на 100 г.\n"
    "name — КОНКРЕТНО, на русском: бренд (если виден) + тип + признак жирности/вида.\n"
    "Напр.: «молоко коровье 3.6%», «йогурт питьевой 3.8%», «молоко миндальное».\n"
    "НЕ обобщай до «молоко»/«йогурт» — иначе разные продукты сольются в каталоге.\n"
    "package_grams — нетто-вес упаковки в г, если указан. serving_grams — вес ОДНОЙ порции в г,\n"
    "если на этикетке есть («в порции / serving / khẩu phần»). found=false, если таблицы КБЖУ нет."
)


PHOTO_PROMPT = (
    "Ты помогаешь вести дневник питания. На входе — снимок и, может быть, подпись человека.\n"
    "1) kind: 'label' — на снимке упаковка/этикетка с таблицей пищевой ценности; 'dish' — еда "
    "(блюдо на тарелке, в миске, в контейнере, продукт в руке); 'body_scan' — распечатка "
    "анализа состава тела (InBody, Tanita и подобные: вес, мышцы, жир, силуэт человека); "
    "'menu' — скриншот или фото меню, карточки блюда в приложении доставки или кафе, где у "
    "блюда НАПЕЧАТАНЫ калории на порцию («410 Kcal • Carbs 32.2G • Fat 11.4G • Protein "
    "45.6G»). Фото блюда рядом с напечатанными калориями — это 'menu', а не 'dish'. Таблица "
    "пищевой ценности на упаковке (на 100 г) — это 'label', а не 'menu'; "
    "'none' — ничего из этого.\n"
    "Для 'label', 'body_scan' и 'none' остальные поля оставь пустыми; для 'menu' заполни "
    "только menu (правило 8).\n"
    "2) dish — короткое название по-русски: «паста с курицей в сливочном соусе».\n"
    "3) components — съедобные составляющие (не больше 8), как их записал бы человек в "
    "дневник: КОНКРЕТНО и со способом приготовления («макароны отварные», «куриное филе "
    "жареное», «рис отварной», «огурцы свежие», «соус сливочный»).\n"
    "   - Всё видимое, что меняет калорийность, пиши в название: кожа («бёдра с кожей» / "
    "«без кожи»), панировка, кляр, сыр сверху, жирный соус. Приложение после карточки "
    "ничего не переспрашивает — чего нет в названии, того нет в расчёте.\n"
    "   - У каждой позиции ОДНО название, без «или» и пояснений в скобках: сомневаешься — "
    "выбери вероятное («шампиньоны обжаренные», а не «свежие или слегка обжаренные»).\n"
    "   - Суп, каша, рагу, плов, оливье и другие смеси, которые на тарелке не разделить, — "
    "ОДНОЙ позицией с конкретным названием («борщ с мясом», «плов с курицей»); видимые "
    "отдельно сметана, хлеб, зелень — отдельными позициями.\n"
    "   - Кость в порции (нога, крыло, рёбра) — в названии «с костью», grams — вместе с костью.\n"
    "   - Масло, на котором готовили, в components НЕ включай и в названии не упоминай "
    "(«на масле») — о нём спросит приложение, иначе оно посчитается дважды. Видимый соус, "
    "сметану, майонез, заправку — ВКЛЮЧАЙ.\n"
    "   - Способ приготовления — только видимый: «жареный» — при явной корочке или "
    "панировке, иначе «запечённый», «отварной», «тушёный». Батат (оранжевая мякоть) — не "
    "картофель.\n"
    "   - share — доля позиции в массе всей еды на снимке (сумма около 1).\n"
    "   - grams — твоя оценка массы позиции. Ориентиры: обычная тарелка 24–26 см, глубокая "
    "тарелка супа 300–350 г, столовая ложка, вилка, ладонь.\n"
    "   - Калорийное и плотное весит МЕНЬШЕ, чем кажется по площади: ломтик бекона ≈8 г, "
    "кружок колбасы ≈5 г, ломтик сыра ≈15 г, треугольник пиццы ≈100 г, горсть орехов ≈30 г. "
    "Овощи, фрукты, листья и крупа, наоборот, лёгкие на вид, но тяжелее, чем кажется. "
    "Замер на взвешенных блюдах: бекон и орехи мы завышали вдвое-втрое.\n"
    "   - Штучное и мелкое (орехи, ягоды, виноград, печенье, конфеты, дольки, кусочки колбасы) "
    "считай ПО ШТУКАМ: число штук × вес одной (миндаль ≈1.2 г, грецкий орех половинка ≈2.5 г, "
    "виноградина ≈5 г, клубника ≈12 г, черри ≈15 г). Плотное занимает мало места и весит "
    "много — не оценивай его по площади на тарелке.\n"
    "   - Кусок целиком (яблоко, котлета, бедро) — по размеру относительно тарелки: среднее "
    "яблоко ≈180 г, куриное бедро с костью ≈150 г.\n"
    "4) Подпись. Если в ней вес ВСЕЙ порции или тарелки («420», «тарелка 420 г») — "
    "caption_total_g. Если вес конкретной позиции («курица 150») — поставь его в grams этой "
    "позиции и fixed=true. Чисел, которых нет в подписи, не выдумывай: нет подписи или "
    "веса в ней — caption_total_g=null и fixed=false везде.\n"
    "5) cooking_fat — сколько масла, по-твоему, ушло на готовку всей тарелки. На фото его не "
    "видно, человека НЕ переспрашиваем — решай сам по способу приготовления и типичному "
    "рецепту: none — сырое, варёное, на пару, гриль без масла, суп, выпечка, фрукты, "
    "напитки; little (~5 г) — запечённое, слегка обжаренное, тушёное; spoon (~10 г) — "
    "жареное на сковороде: яичница, жареное мясо или курица, сэндвич или хот-дог с жареной "
    "сосиской, жареный рис или лапша; lots (~20 г) — во фритюре или обильно: картофель фри, "
    "панировка, кляр, темпура, чебурек. fat_kind — какое масло вероятнее.\n"
    "6) dressing — заправка, которой НЕ видно: oil — салат из свежих овощей без видимой "
    "заправки; sour_cream — борщ или вареники, где сметаны не видно, но так их едят; mayo — "
    "редко, только если блюдо без него не бывает; none — во всех остальных случаях и когда "
    "заправка видна и уже есть в components.\n"
    "7) liquid — true для супа и напитков.\n"
    "8) menu — только для kind='menu': каждая позиция, у которой напечатаны калории. name — "
    "название ровно как напечатано (латиницей — так латиницей). kcal, protein, fat, carb — "
    "ЧИСЛА РОВНО КАК НАПЕЧАТАНЫ на порцию, ничего не пересчитывай и не оценивай; не "
    "напечатано — null. portion — объём или вес порции, если напечатан («360 мл»), иначе null. "
    "count — число в счётчике заказа или корзины рядом с позицией («1», «2»), если он виден, "
    "иначе null."
)


class PlateComponentOut(BaseModel):
    name: str
    share: float
    grams: float
    fixed: bool


class MenuLineOut(BaseModel):
    name: str
    kcal: float
    protein: float | None
    fat: float | None
    carb: float | None
    portion: str | None
    count: int | None


class PhotoReadingOut(BaseModel):
    kind: Literal["label", "dish", "body_scan", "menu", "none"]
    dish: str
    components: list[PlateComponentOut]
    caption_total_g: float | None
    cooking_fat: Literal["none", "little", "spoon", "lots"]
    fat_kind: Literal["масло подсолнечное", "масло сливочное", "масло оливковое"]
    dressing: Literal["none", "sour_cream", "mayo", "oil"]
    liquid: bool
    menu: list[MenuLineOut]


SCAN_PROMPT = (
    "На фото — распечатка анализа состава тела. Перепиши числа ТОЧНО как напечатаны, "
    "ничего не пересчитывай и не округляй. Бланк может быть на вьетнамском, русском, "
    "английском, корейском.\n"
    "СНАЧАЛА заполни rows: выпиши по строке на каждое число — подпись как на бланке и "
    "значение («Chất khoáng (kg) 4.75»). Возьми первую таблицу состава, раздел ожирения "
    "(BMI, PBF), исследовательские параметры и оба силуэта сегментного анализа. Поля ниже "
    "заполняй ПЕРЕНОСОМ из rows.\n"
    "Числа силуэтов выписывай С ПОДПИСЬЮ УЧАСТКА, по пять на каждый силуэт, в одном и том же "
    "порядке — иначе они перепутаются местами:\n"
    "  мышцы: рука левая … / рука правая … / ТУЛОВИЩЕ … / нога левая … / нога правая …\n"
    "  жир:   рука левая … / рука правая … / ТУЛОВИЩЕ … / нога левая … / нога правая …\n"
    "Туловище напечатано В ЦЕНТРЕ силуэта и его легче всего пропустить. Нашёл на силуэте "
    "только четыре числа — ищи пятое в центре; не нашёл — весь этот силуэт null, а "
    "оставшиеся числа НЕ сдвигай в чужие поля.\n"
    "НИЧЕГО НЕ ВЫВОДИ. Каждое поле — число, напечатанное на бланке. Нет такой строки — null. "
    "Пустое поле безопаснее вычисленного: приложение сверяет числа между собой, и выведенное "
    "вычитанием пройдёт сверку насквозь вместе с ошибкой чтения.\n"
    "- brand: производитель (InBody, Tanita, …); device: модель, если напечатана (InBody270).\n"
    "- date_text: дата и время обследования сверху бланка ровно как напечатано, порядок чисел "
    "не меняй («09.22.2026 15:26»); language: язык бланка (vi, ru, en, ko, …).\n"
    "- other_dates: все ОСТАЛЬНЫЕ даты на бланке — история замеров внизу, подписи под "
    "графиком («01.13.26 13:15»), тоже как напечатаны. Нет — пустой список.\n"
    "- height_cm, age, sex (male/female — «Nam»/«Nam giới»/«М» = male).\n"
    "- Таблица «анализ состава тела» (Body Composition Analysis / Phân tích thành phần cơ thể): "
    "water_l — общая вода (Total Body Water / Tổng lượng nước), protein_kg — белок, "
    "minerals_kg — минералы (Chất khoáng), body_fat_kg — масса жира (Body Fat Mass / Khối "
    "lượng mỡ), weight_kg — вес (Weight / Trọng lượng).\n"
    "- skeletal_muscle_kg — скелетная мышечная масса (SMM / Khối lượng cơ xương).\n"
    "- body_fat_pct — процент жира (PBF) из раздела ожирения (Phân tích béo phì / Obesity "
    "analysis), в ПРОЦЕНТАХ. Не путай с массой жира в кг из первой таблицы: это разные "
    "строки в разных местах бланка, и рядом с PBF стоит BMI.\n"
    "- fat_free_kg — безжировая масса (Fat Free Mass / Lượng cơ không mỡ), обычно в "
    "исследовательских параметрах. Отдельной строки нет — null, вычитанием не считай.\n"
    "- visceral_level — уровень висцерального жира (Visceral Fat Level / Mức mỡ nội tạng): "
    "бери из исследовательских параметров (Thông số nghiên cứu) или из строки «Mức độ»/"
    "«Level» — целое число 1–20. Не подписи шкалы («Thấp 10 Cao» — это «низкий/высокий») "
    "и не килограммы из силуэтов (сегменты рук, ног, туловища).\n"
    "- bmr_kcal — базовый обмен (Basal Metabolic Rate / Tỷ lệ trao đổi chất cơ bản), только "
    "число, без диапазона в скобках.\n"
    "- smi — мышечный индекс (SMI, Skeletal Muscle Index, кг/м²): НАПЕЧАТАННОЕ число из "
    "исследовательских параметров (Thông số nghiên cứu), обычно 5–12 у мужчин. Не вычисляй "
    "его сам: мышцы, делённые на рост в квадрате, дают ДРУГОЕ число. Не путай с BMI.\n"
    "- inbody_score — балл прибора (InBody Score / Điểm số InBody), целое 0–100, обычно "
    "крупно рядом с «/100».\n"
    "- Сегментный анализ — ДВА РАЗНЫХ силуэта рядом, и путать их нельзя. ЛЕВЫЙ силуэт — "
    "безжировая (мышечная) масса (Segmental Lean Analysis / Phân tích phân đoạn mô không mỡ) "
    "→ поля lean_*. ПРАВЫЙ силуэт — жир (Segmental Fat Analysis / Phân tích phân đoạn chất "
    "béo) → поля fat_*. У каждого силуэта пять чисел: левая рука, правая рука, туловище, "
    "левая нога, правая нога.\n"
    "   - Бери ТОЛЬКО килограммы — число с «kg» над процентом. Проценты (122.3%, 265.5%) и "
    "подписи «Trên / Bình thường / Dưới» — это доля от нормы, их НЕ бери.\n"
    "   - Порядок величин помогает не перепутать силуэты: в ЛЕВОМ туловище самое тяжёлое "
    "(обычно 15–45 кг), ноги 5–18, руки 1.5–6. В ПРАВОМ все числа меньше: туловище 2–25, "
    "ноги 0.5–8, руки 0.2–4. Если «мышцы туловища» вышли меньше 15 кг — ты смотришь не на "
    "тот силуэт, перечитай.\n"
    "   - ПРОВЕРЬ СЕБЯ, прежде чем ответить: сумма пяти lean_* близка к безжировой массе "
    "(обычно 85–95% от неё — голова в сегменты не входит), сумма пяти fat_* близка к массе "
    "жира (85–100%). Не сходится — перечитай числа с бланка, НЕ подгоняй их.\n"
    "   - Лево и право — как подписано на бланке (Trái — левая, Phải — правая), не зеркаль.\n"
    "   - Чисел в силуэте не видно, смазано или обрезано — оставь ВСЕ сегменты null. "
    "Наполовину прочитанный силуэт хуже непрочитанного.\n"
    "Числа в скобках рядом со значением — это нормы; их НЕ бери. Деления и подписи шкал "
    "(полоски «Дưới / Bình thường / Trên», ряды 55 70 85 100 115…) — тоже не значения. "
    "Историю замеров внизу бланка НЕ бери в числа — только в other_dates. Поля, которых нет "
    "на бланке, — null. Не выдумывай."
)


class ScanReadingOut(BaseModel):
    rows: str = Field(description="строки бланка как напечатаны: подпись и число, по одной")
    brand: str
    device: str | None
    date_text: str
    other_dates: list[str]
    language: str
    height_cm: float | None
    age: int | None
    sex: Literal["male", "female"] | None
    weight_kg: float | None
    skeletal_muscle_kg: float | None
    body_fat_kg: float | None
    body_fat_pct: float | None
    fat_free_kg: float | None
    water_l: float | None
    protein_kg: float | None
    minerals_kg: float | None
    visceral_level: float | None
    bmr_kcal: float | None
    smi: float | None
    inbody_score: float | None
    lean_arm_l_kg: float | None
    lean_arm_r_kg: float | None
    lean_trunk_kg: float | None
    lean_leg_l_kg: float | None
    lean_leg_r_kg: float | None
    fat_arm_l_kg: float | None
    fat_arm_r_kg: float | None
    fat_trunk_kg: float | None
    fat_leg_l_kg: float | None
    fat_leg_r_kg: float | None


class LabelReading(BaseModel):
    found: bool = Field(description="распознана ли таблица КБЖУ на фото")
    name: str = Field(description="название продукта на русском")
    kcal_per_100g: float
    protein_per_100g: float
    fat_per_100g: float
    carb_per_100g: float
    package_grams: float | None = Field(default=None, description="нетто-вес упаковки, г")
    serving_grams: float | None = Field(default=None, description="вес одной порции, г")


class OpenAiVision:
    def __init__(self, client: AsyncOpenAI, model: str, meter: object | None = None) -> None:
        self._client = client
        self._model = model
        # До 16.09.2026 фото не считались вовсе — самый дорогой вызов шёл мимо
        # лимита (AI-PRINCIPLES §2).
        self._meter = meter

    async def _meter_usage(self, completion: object) -> None:
        if self._meter is None:
            return
        try:
            tokens_in, cached, tokens_out = token_counts(completion.usage)
            await self._meter.note(
                model=self._model, input_tokens=tokens_in,
                output_tokens=tokens_out, cached_tokens=cached,
            )
        except Exception:  # noqa: BLE001 — недосчитать цент дешевле, чем потерять фото
            log.warning("не удалось записать расход фото", exc_info=True)

    async def _parse(
        self, system: str, text: str, image: bytes, schema: type[_Out],
        *, detail: str | None = None,
    ) -> _Out | None:
        """Один вызов чтения: промпт, подпись, снимок → объект схемы или None («не прочитал»).

        Единственное место, где живёт транспорт: Claude подменяет только его (D-41), а
        перевод ответа в доменные объекты остаётся общим."""
        b64 = base64.b64encode(image).decode()
        picture: dict[str, str] = {"url": f"data:image/jpeg;base64,{b64}"}
        if detail:
            picture["detail"] = detail
        completion = await self._client.beta.chat.completions.parse(
            model=self._model,
            # gpt-5.x отвергает явную temperature (400) — там параметр не шлём вовсе
            temperature=temperature_for(self._model, 0.0),
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text},
                        {"type": "image_url", "image_url": picture},
                    ],
                },
            ],
            response_format=schema,
        )
        await self._meter_usage(completion)
        return completion.choices[0].message.parsed

    async def read_photo(self, image: bytes, caption: str = "") -> PlateReading:
        """FR-34: что на снимке и из чего блюдо. Считает приложение, модель только
        раскладывает тарелку (INV-1)."""
        text = f"Подпись человека: «{caption.strip()[:200]}»" if caption.strip() else "Подписи нет."
        # high: оценке порции нужны детали — ложка рядом, край тарелки.
        r = await self._parse(PHOTO_PROMPT, text, image, PhotoReadingOut, detail="high")
        if r is None:
            return PlateReading(kind="none")
        return PlateReading(
            kind=r.kind,
            dish=r.dish.strip()[:120],
            components=[
                Component(c.name.strip()[:80], c.share, c.grams, c.fixed)
                for c in r.components[:MAX_COMPONENTS]
                if c.name.strip()
            ],
            caption_total_g=r.caption_total_g,
            cooking_fat=r.cooking_fat,
            fat_kind=r.fat_kind if r.fat_kind in FAT_KINDS else FAT_KINDS[0],
            dressing=r.dressing,
            liquid=r.liquid,
            menu=[
                MenuLine(m.name.strip()[:120], m.kcal, m.protein, m.fat, m.carb,
                         (m.portion or "").strip()[:30] or None, m.count)
                for m in r.menu[:MAX_MENU]
            ],
        )

    async def read_scan(self, image: bytes) -> ScanReading | None:
        """FR-36: числа с бланка состава тела как напечатаны. Сверку и выводы делает
        домен (`app/domain/body_scan.py`)."""
        r = await self._parse(
            SCAN_PROMPT, "Перепиши числа с этого бланка.", image, ScanReadingOut, detail="high"
        )
        if r is None:
            return None
        data = r.model_dump()
        # rows — черновик чтения: он нужен модели, чтобы не выводить числа, а не нам.
        data.pop("rows", None)
        data["other_dates"] = tuple(data.get("other_dates") or ())
        return ScanReading(**data)

    async def read_label(self, image: bytes) -> ParsedItem | None:
        r = await self._parse(
            SYSTEM_PROMPT, "Прочитай таблицу КБЖУ с этой этикетки.", image, LabelReading
        )
        if r is None or not r.found:
            return None
        if r.kcal_per_100g == 0 and r.protein_per_100g == 0 and r.fat_per_100g == 0:
            return None  # пустое чтение — этикетку не разобрали
        return ParsedItem(
            name=r.name.strip() or "продукт с этикетки",
            canonical_key="",  # meal_service сам сделает slug из имени (INV-4)
            unit=Unit.GRAM,
            grams=None,
            kcal_per_100g=r.kcal_per_100g,
            protein_per_100g=r.protein_per_100g,
            fat_per_100g=r.fat_per_100g,
            carb_per_100g=r.carb_per_100g,
            unit_weight_g=r.package_grams,
            serving_g=r.serving_grams,
            from_label=True,
            confidence=1.0,
        )
