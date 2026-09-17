"""Use-case: замеры состава тела InBody (MINIAPP-SPEC §6.24, FR-36).

Приём (проверка бланка, «чей бланк», дата), сохранение, предложение целей и
сводка для «Итогов». Считает домен (`body_scan`, `energy`), здесь — только
сбор данных и порядок шагов. Цели меняются только явным `apply` (MA-INV-52).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.application.dto import ScanReading
from app.application.entities import User, WeightLog
from app.application.ports import BodyScanRepo, UserRepo, WeightRepo
from app.application.stats_service import StatsService, _require_id
from app.clock import Clock, utcnow
from app.domain import energy
from app.domain.body_scan import (
    LEAN_SHARE_WARN,
    BodyScan,
    Change,
    change,
    cunningham,
    foreign,
    mifflin,
    mismatches,
    parse_blank_date,
    protein_target,
)
from app.domain.goals import ACTIVITY_FACTORS, TARGET_FACTORS, Goal, rebalance, set_macro
from app.domain.prefs import day_start, sanitize
from app.domain.timeframe import local_date

FRESH_DAYS = 180
OLD_DAYS = 730


@dataclass
class ScanCheck:
    """Что показать на карточке до сохранения."""

    scan: BodyScan
    bad: set[str]
    foreign: bool
    date_ambiguous: bool
    date_problem: str | None      # future | old | None
    existing: BodyScan | None

    @property
    def needs_confirm(self) -> bool:
        return bool(self.bad) or self.foreign or self.date_ambiguous or self.date_problem == "old"


@dataclass
class GoalProposal:
    current: Goal
    proposed: Goal
    bmr_kcal: float
    bmr_source: str               # inbody | formula
    activity: str
    burn_kcal: float              # обмен × активность
    base: str                     # estimate | energy
    tdee_kcal: float | None = None
    fat_free_kg: float | None = None

    @property
    def changed(self) -> bool:
        a, b = self.current, self.proposed
        return (round(a.daily_kcal), round(a.protein_g), round(a.fat_g), round(a.carb_g)) != (
            round(b.daily_kcal), round(b.protein_g), round(b.fat_g), round(b.carb_g))


@dataclass
class Overview:
    scans: list[BodyScan]
    last_change: Change | None = None
    lean_warning: bool = False
    diary: energy.DiaryCheck | None = None
    diary_since: date | None = None
    spend: energy.Expenditure | None = None
    proposal: GoalProposal | None = None
    stale_days: int | None = None
    formula_bmr: float | None = None
    inbody_bmr: float | None = None
    burn_kcal: float | None = None


def from_reading(reading: ScanReading, tz: str) -> tuple[BodyScan | None, bool, str | None]:
    """Прочитанное с бланка → замер. Третье — причина отказа: `not_inbody` (другие
    приборы считают обмен иначе), `no_numbers` (нет веса или жира), `no_date`."""
    if "inbody" not in f"{reading.brand} {reading.device or ''}".lower():
        return None, False, "not_inbody"
    if not reading.weight_kg or not reading.body_fat_kg:
        return None, False, "no_numbers"
    ymd, ambiguous = parse_blank_date(reading.date_text, reading.language or "")
    if ymd is None:
        return None, False, "no_date"
    hm = re.search(r"(\d{1,2}):(\d{2})", reading.date_text or "")
    hh, mm = (int(hm.group(1)), int(hm.group(2))) if hm else (12, 0)
    if not (0 <= hh < 24 and 0 <= mm < 60):
        hh, mm = 12, 0
    fat_free = reading.fat_free_kg or round(reading.weight_kg - reading.body_fat_kg, 1)
    scan = BodyScan(
        measured_at=datetime(*ymd, hh, mm, tzinfo=ZoneInfo(tz)),
        weight_kg=reading.weight_kg,
        body_fat_kg=reading.body_fat_kg,
        fat_free_kg=fat_free,
        body_fat_pct=reading.body_fat_pct,
        skeletal_muscle_kg=reading.skeletal_muscle_kg,
        water_l=reading.water_l,
        protein_kg=reading.protein_kg,
        minerals_kg=reading.minerals_kg,
        visceral_level=reading.visceral_level,
        bmr_kcal=reading.bmr_kcal,
        height_cm=reading.height_cm,
        age=reading.age,
        sex=reading.sex,
        device=(reading.device or reading.brand or "")[:64] or None,
    )
    return scan, ambiguous, None


class BodyScanService:
    def __init__(
        self,
        scans: BodyScanRepo,
        users: UserRepo,
        weights: WeightRepo,
        stats: StatsService,
        clock: Clock = utcnow,
    ) -> None:
        self._scans = scans
        self._users = users
        self._weights = weights
        self._stats = stats
        self._clock = clock

    # --- приём ------------------------------------------------------------

    async def check(
        self, user: User, scan: BodyScan, *, date_ambiguous: bool = False
    ) -> ScanCheck:
        uid = _require_id(user)
        scan = replace(scan, user_id=uid)
        now = self._clock()
        problem = None
        if scan.measured_at > now + timedelta(hours=1):
            problem = "future"
        elif (now - scan.measured_at).days > OLD_DAYS:
            problem = "old"
        history = [s for s in await self._scans.list_for(uid)
                   if s.measured_at != scan.measured_at]
        prev = max((s for s in history if s.measured_at < scan.measured_at),
                   key=lambda s: s.measured_at, default=None)
        last = await self._weights.latest(uid, before=scan.measured_at + timedelta(days=45))
        last_weight = (last.measured_at, last.weight_kg) if last is not None else None
        return ScanCheck(
            scan=scan,
            bad=mismatches(scan),
            foreign=foreign(scan, prev, last_weight),
            date_ambiguous=date_ambiguous,
            date_problem=problem,
            existing=await self._scans.get_at(uid, scan.measured_at),
        )

    async def save(
        self, user: User, scan: BodyScan, *, confirmed: bool,
        corrected: list[str] | None = None, date_ambiguous: bool = False,
    ) -> tuple[BodyScan, bool]:
        """MA-INV-50: несошедшееся сохраняется только с подтверждением."""
        chk = await self.check(user, scan, date_ambiguous=date_ambiguous)
        if chk.date_problem == "future":
            raise ValueError("future")
        if chk.needs_confirm and not confirmed:
            raise ValueError("mismatch")
        saved, updated = await self._scans.upsert(chk.scan, corrected)
        await self._weight_from_scan(user, saved)
        return saved, updated

    async def _weight_from_scan(self, user: User, scan: BodyScan) -> None:
        """Вес замера — в ленту веса, если в тот день веса ещё нет."""
        uid = _require_id(user)
        start_h = day_start(user.ui_prefs)
        day = local_date(scan.measured_at, user.timezone, start_h)
        history = await self._weights.history(uid, 400)
        if any(local_date(w.measured_at, user.timezone, start_h) == day for w in history):
            return
        await self._weights.add(
            WeightLog(user_id=uid, measured_at=scan.measured_at, weight_kg=scan.weight_kg)
        )

    async def delete(self, user: User, scan_id: int) -> bool:
        return await self._scans.delete(_require_id(user), scan_id)

    # --- цели ---------------------------------------------------------------

    async def fresh_scan(self, user: User) -> BodyScan | None:
        scans = await self._scans.list_for(_require_id(user))
        if not scans:
            return None
        last = scans[-1]
        return last if (self._clock() - last.measured_at).days <= FRESH_DAYS else None

    async def bmr(self, user: User) -> float | None:
        """Обмен для пола недели (§6.23): только по свежему замеру."""
        scan = await self.fresh_scan(user)
        if scan is None:
            return None
        return self._bmr_pair(user, scan, await self._latest_weight(user, scan))[0]

    async def _latest_weight(self, user: User, scan: BodyScan) -> float:
        last = await self._weights.latest(_require_id(user))
        if last is not None and last.measured_at >= scan.measured_at:
            return last.weight_kg
        return scan.weight_kg

    def _bmr_pair(self, user: User, scan: BodyScan, weight: float) -> tuple[float, str]:
        formula = self._formula(scan, weight)
        if sanitize(user.ui_prefs)["bmr_source"] == "formula" and formula is not None:
            return formula, "formula"
        return cunningham(scan.fat_free_kg), "inbody"

    def _formula(self, scan: BodyScan, weight: float) -> float | None:
        if not (scan.height_cm and scan.age is not None and scan.sex):
            return None
        years = (self._clock() - scan.measured_at).days // 365
        return mifflin(weight, scan.height_cm, scan.age + years, scan.sex)

    async def proposal(
        self, user: User, spend: energy.Expenditure | None = None
    ) -> GoalProposal | None:
        scan = await self.fresh_scan(user)
        if scan is None:
            return None
        current = await self._stats.current_goal(user)
        weight = await self._latest_weight(user, scan)
        bmr, source = self._bmr_pair(user, scan, weight)
        activity = sanitize(user.ui_prefs)["activity"]
        burn = round(bmr * ACTIVITY_FACTORS[activity])
        target = current.target or "maintain"
        tdee = spend.tdee_kcal if spend is not None and spend.proposable else None
        kcal = round((tdee or burn) * TARGET_FACTORS.get(target, 1.0))
        goal = rebalance(kcal, weight, target)
        goal = set_macro(goal, "protein", protein_target(scan.fat_free_kg, target, weight))
        return GoalProposal(
            current=current, proposed=goal, bmr_kcal=bmr, bmr_source=source,
            activity=activity, burn_kcal=burn,
            base="energy" if tdee else "estimate",
            tdee_kcal=spend.tdee_kcal if spend else None,
            fat_free_kg=scan.fat_free_kg,
        )

    async def apply(self, user: User) -> Goal:
        prop = (await self.overview(user)).proposal
        if prop is None:
            raise ValueError("no_scan")
        await self._users.set_goal(
            _require_id(user), prop.proposed, "energy" if prop.base == "energy" else "scan"
        )
        return prop.proposed

    # --- сводка -------------------------------------------------------------

    async def overview(self, user: User) -> Overview:
        uid = _require_id(user)
        scans = await self._scans.list_for(uid)
        if not scans:
            return Overview(scans=[])
        last = scans[-1]
        zone = user.timezone
        stale = (self._clock() - last.measured_at).days
        last_change = change(scans[-2], last) if len(scans) >= 2 else None
        lean_warn = bool(
            last_change and last_change.lean_share is not None
            and last_change.lean_share > LEAN_SHARE_WARN
        )
        weight = await self._latest_weight(user, last)
        bmr, _ = self._bmr_pair(user, last, weight)
        burn = round(bmr * ACTIVITY_FACTORS[sanitize(user.ui_prefs)["activity"]])

        spend = diary = since = None
        start_h = day_start(user.ui_prefs)

        def span(a, b):
            d0, d1 = local_date(a, zone, start_h), local_date(b, zone, start_h)
            return d0, [d0 + timedelta(days=i) for i in range((d1 - d0).days)]

        win = energy.window([s.measured_at for s in scans])
        if win is not None:
            first = next(s for s in scans if s.measured_at == win[0])
            _, days = span(win[0], win[1])
            eaten = await self._stats.kcal_by_day(user, days[0], days[-1])
            goals = await self._stats.goals_by_day(user, days)
            full = {d: k for d, k in eaten.items()
                    if k >= energy.FULL_DAY_SHARE * goals.get(d, 0)}
            spend = energy.expenditure(
                days=len(days), logged_days=len(full), logged_kcal_total=sum(full.values()),
                d_fat_kg=last.body_fat_kg - first.body_fat_kg,
                d_lean_kg=last.fat_free_kg - first.fat_free_kg,
            )
        # Сверка «ждали — показали» — по двум последним замерам при любом зазоре:
        # окно расхода (28–120 дней) здесь ни при чём.
        if len(scans) >= 2:
            first = scans[-2]
            d0, days = span(first.measured_at, last.measured_at)
            if days:
                eaten = await self._stats.kcal_by_day(user, days[0], days[-1])
                logged = {d: k for d, k in eaten.items() if k > 0}
                diary = energy.diary_check(
                    days=len(days), logged_days=len(logged),
                    logged_kcal_total=sum(logged.values()), burn_kcal=burn,
                    actual_weight_kg=round(last.weight_kg - first.weight_kg, 1),
                )
                since = d0
        if diary is None:
            d0 = local_date(last.measured_at, zone, start_h)
            today = local_date(self._clock(), zone, start_h)
            if today > d0:
                eaten = await self._stats.kcal_by_day(user, d0 + timedelta(days=1), today)
                logged = {d: k for d, k in eaten.items() if k > 0}
                diary = energy.diary_check(
                    days=(today - d0).days, logged_days=len(logged),
                    logged_kcal_total=sum(logged.values()), burn_kcal=burn,
                    actual_weight_kg=None,
                )
                since = d0

        return Overview(
            scans=scans, last_change=last_change, lean_warning=lean_warn,
            diary=diary, diary_since=since, spend=spend,
            proposal=await self.proposal(user, spend) if stale <= FRESH_DAYS else None,
            stale_days=stale, formula_bmr=self._formula(last, weight),
            inbody_bmr=cunningham(last.fat_free_kg), burn_kcal=burn,
        )
