"""Station load in weighted products vs daily capacity and delivery dates.

Model agreed with the user on 2026-10-01 (docs/dashboard_load_model.md):
- one active work order line counts once per station it still has open steps at
  (several steps of the same product at a station = 1), times its quantity,
  times a size weight (small 0.5 / normal 1 / large 2);
- capacity per station = "normal" products per working day (Mon-Fri, no Greek
  public holidays);
- each product gets a per-station due date by scheduling BACKWARDS from its
  delivery date through the stations that follow it, as late as each
  downstream station's capacity allows (so its queue is included);
- load = worst ratio over due dates of (weight due by d) / (capacity x working
  days from today to d). Overdue work counts as due today.

Pure functions over Epoptia work-order-line records (API / backup format).
`python -m dashboard.load_model` prints a report from the nightly backup.
"""
from collections import defaultdict
from datetime import date, timedelta
import json
import re
import unicodedata

# Products per working day AS THEY USUALLY ARRIVE at that station (user, 2026-10-01):
# e.g. ΜΟΝΤΑΖ ΤΖΑΜΙΑ finishes half a showcase per day. PUNCHING is ignored (to be removed).
PRODUCTS_PER_DAY = {
    "LASER": 5.0, "ΚΟΠΗ ΨΑΛΙΔΙ": 5.0, "ΣΤΡΑΝΤΖΑ": 4.0,
    "ΜΟΝΤΑΖ 1": 4.0, "ΜΟΝΤΑΖ 2": 2.0, "ΜΟΝΤΑΖ ΤΖΑΜΙΑ": 0.8, "ΨΥΚΤΙΚΑ": 3.0,
}   # revised by the user 2026-10-01
# Average size weight of the products that pass each station (history), so that the
# user's "typical products/day" converts to weighted units. Provisional values from
# 1,248 recent lines; recompute with `--reference` from the full backup and review.
REFERENCE_WEIGHT = {
    "LASER": 1.0, "ΚΟΠΗ ΨΑΛΙΔΙ": 0.95, "ΣΤΡΑΝΤΖΑ": 1.34,
    "ΜΟΝΤΑΖ 1": 1.32, "ΜΟΝΤΑΖ 2": 1.72, "ΜΟΝΤΑΖ ΤΖΑΜΙΑ": 1.0, "ΨΥΚΤΙΚΑ": 1.0,
}
CAPACITY_PER_DAY = {s: PRODUCTS_PER_DAY[s] * REFERENCE_WEIGHT[s] for s in PRODUCTS_PER_DAY}
WEIGHTS = {"small": 0.5, "normal": 1.0, "large": 2.0}
ACTIVE_STATUSES = ("production", "standby")
LARGE_LENGTH_CM = 250


# -- calendar -----------------------------------------------------------------
def orthodox_easter(year):
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day = divmod(d + e + 114, 31)
    return date(year, month, day + 1) + timedelta(days=13)   # Julian -> Gregorian (1900-2099)


def greek_holidays(year):
    easter = orthodox_easter(year)
    fixed = [(1, 1), (1, 6), (3, 25), (5, 1), (8, 15), (10, 28), (12, 25), (12, 26)]
    moving = [easter - timedelta(days=48), easter - timedelta(days=2),
              easter + timedelta(days=1), easter + timedelta(days=50)]
    return {date(year, m, d) for m, d in fixed} | set(moving)


def is_workday(day):
    return day.weekday() < 5 and day not in greek_holidays(day.year)


def workdays_between(start, end):
    """Working days in [start, end] inclusive; 0 if end < start."""
    count, day = 0, start
    while day <= end:
        count += is_workday(day)
        day += timedelta(days=1)
    return count


_EPOCH = date(2024, 1, 1)
_workdays = []          # ordinal k (1-based) -> date of the k-th workday since _EPOCH
_ordinal = {}


def _extend(until):
    day = _workdays[-1] + timedelta(days=1) if _workdays else _EPOCH
    while day <= until:
        if is_workday(day):
            _workdays.append(day)
            _ordinal[day] = len(_workdays)
        day += timedelta(days=1)


def workday_end(day):
    """Time index of the END of the last workday on or before `day` (1 unit = 1 workday)."""
    _extend(day + timedelta(days=7))
    while day not in _ordinal:
        day -= timedelta(days=1)
    return float(_ordinal[day])


def index_to_date(index):
    """Workday that contains time index `index` (k-1 < index <= k -> k-th workday)."""
    k = max(1, int(-(-index // 1)))
    while len(_workdays) < k:
        _extend((_workdays[-1] if _workdays else _EPOCH) + timedelta(days=60))
    return _workdays[k - 1]


# -- product size ---------------------------------------------------------------
def _norm(text):
    text = "".join(c for c in unicodedata.normalize("NFD", (text or "").lower())
                   if unicodedata.category(c) != "Mn")
    return " ".join(text.split())


def station_name(value):
    name = " ".join(_norm(value).upper().split())
    return "ΣΤΡΑΝΤΖΑ" if name == "ΣΤΡΑΤΖΑ" else name


def length_cm(dimensions):
    match = re.match(r"\s*(\d+(?:[.,]\d+)?)", dimensions or "")
    return float(match.group(1).replace(",", ".")) if match else None


def size_class(line):
    """('small'|'normal'|'large', reason) from description, comments and ΔΙΑΣΤΑΣΕΙΣ."""
    name = _norm(f"{line.get('description', '')} {(line.get('product') or {}).get('name', '')}")
    comments = _norm(line.get("comments"))
    dims = next((c.get("value") for c in line.get("customFields") or []
                 if _norm(c.get("name")) == "διαστασεις"), None)
    length = length_cm(dims)
    drawers = "συρταρ" in comments or "συρταρ" in name
    if re.search(r"βιτριν|coffee|cocktail|συρταριερ", name):
        return "large", "βιτρίνα/coffee station/συρταριέρα"
    if re.search(r"θαλαμος", name) and re.search(r"ψυγ|συντηρ|καταψ", name):
        return "large", "θάλαμος ψυγείο"
    if re.search(r"παγκος|ερμαρι", name) and drawers:
        return "large", "πάγκος/ερμάριο με συρτάρια"
    if re.search(r"πλατη|ραφι|πορτα|plexi|μπρατσ|μπαρα", name):
        return "small", "πλάτη/ράφι/πόρτα"
    if length is not None and length > LARGE_LENGTH_CM:
        return "large", f"μήκος {length:g} cm > 2,5 μ."
    return "normal", "κανονικό" + ("" if length is not None else " (χωρίς μήκος)")


# -- routing ----------------------------------------------------------------------
def station_weight(station, size):
    """How much one product of this size costs at this station (user rules 2026-10-01).

    Size matters only where the user said so: ΜΟΝΤΑΖ 1 uses all three classes,
    ΚΟΠΗ ΨΑΛΙΔΙ only small (0.5) vs the rest (1), ΜΟΝΤΑΖ ΤΖΑΜΙΑ is the same for every size.
    PROVISIONAL until the user gives rules: ΣΤΡΑΝΤΖΑ, ΜΟΝΤΑΖ 2 (three classes),
    LASER and ΨΥΚΤΙΚΑ (1 for all).
    """
    if station in ("ΜΟΝΤΑΖ 1", "ΣΤΡΑΝΤΖΑ", "ΜΟΝΤΑΖ 2"):
        return WEIGHTS[size]
    if station == "ΚΟΠΗ ΨΑΛΙΔΙ":
        return 0.5 if size == "small" else 1.0
    return 1.0


def open_stations(line):
    """{station: (depth of its LAST open step, open steps / all steps there)} in routing order."""
    steps = {s["elementId"]: s for s in line.get("erp_routing") or [] if "elementId" in s}
    depth = {}

    def walk(element, seen=()):
        if element in depth:
            return depth[element]
        prev = [p for p in steps[element].get("previous") or [] if p in steps and p not in seen]
        depth[element] = 1 + max((walk(p, seen + (element,)) for p in prev), default=0)
        return depth[element]

    depths, total, still_open = {}, defaultdict(int), defaultdict(int)
    for element, step in steps.items():
        station = station_name(step.get("workstationName"))
        if station not in CAPACITY_PER_DAY:
            continue
        total[station] += 1
        if step.get("status") != "completed":
            still_open[station] += 1
            depths[station] = max(depths.get(station, 0), walk(element))
    return {s: (depths[s], still_open[s] / total[s]) for s in depths}


# -- model ------------------------------------------------------------------------
def reference_weights(lines):
    """Average station weight of every product that passes each station (any status)."""
    sums = defaultdict(list)
    for line in lines:
        size = size_class(line)[0]
        for step in line.get("erp_routing") or []:
            station = station_name(step.get("workstationName"))
            if station in PRODUCTS_PER_DAY:
                sums[(station, line.get("workorderline_id"))] = station_weight(station, size)
    out = defaultdict(list)
    for (station, _), weight in sums.items():
        out[station].append(weight)
    return {s: round(sum(v) / len(v), 2) for s, v in out.items()}


def build_jobs(lines):
    jobs, skipped = [], defaultdict(int)
    for line in lines:
        if line.get("production_status") not in ACTIVE_STATUSES:
            continue
        stations = open_stations(line)
        if not stations:
            skipped["no_open_tracked_station"] += 1
            continue
        try:
            delivery = date.fromisoformat(line.get("target_day"))
        except (TypeError, ValueError):
            skipped["no_delivery_date"] += 1
            continue
        size, reason = size_class(line)
        quantity = line.get("quantity") or 1
        work = {s: station_weight(s, size) * quantity * remaining for s, (_, remaining) in stations.items()}
        jobs.append(dict(wol=line["workorderline_id"], description=line.get("description"),
                         client=(line.get("client") or {}).get("name"), delivery=delivery,
                         size=size, size_reason=reason, work=work,
                         stations={s: depth for s, (depth, _) in stations.items()},
                         remaining={s: round(r, 2) for s, (_, r) in stations.items()}))
    return jobs, dict(skipped)


def backward_schedule(jobs):
    """Per job and station: time index by which it must LEAVE that station.

    Stations are handled downstream-first. At each station, jobs are placed as
    late as possible, latest due first; a job cannot finish after the start of
    the next job already placed (capacity is shared), so queues propagate
    upstream. Delivery = must be done by the end of the workday before it.
    """
    order = sorted(CAPACITY_PER_DAY, key=lambda s: -sum(j["stations"].get(s, 0) for j in jobs)
                   / max(1, sum(s in j["stations"] for j in jobs)))
    starts = {}                                                   # (wol, station) -> start index
    for job in jobs:
        job["due_index"] = {}
    for station in order:
        capacity = CAPACITY_PER_DAY[station]
        todo = []
        for job in jobs:
            if station not in job["stations"]:
                continue
            depth = job["stations"][station]
            later = [starts[(job["wol"], s)] for s, d in job["stations"].items()
                     if d > depth and (job["wol"], s) in starts]
            job["due_index"][station] = min(later) if later else workday_end(job["delivery"] - timedelta(days=1))
            todo.append(job)
        free = float("inf")
        for job in sorted(todo, key=lambda j: j["due_index"][station], reverse=True):
            finish = min(job["due_index"][station], free)
            free = finish - job["work"][station] / capacity
            starts[(job["wol"], station)] = free
    for job in jobs:
        job["due"] = {s: index_to_date(i) for s, i in job["due_index"].items()}
    return jobs


def station_load(jobs, today):
    now = workday_end(today) - (1.0 if is_workday(today) else 0.0)   # start of today's work
    report = {}
    for station, capacity in CAPACITY_PER_DAY.items():
        mine = sorted((j for j in jobs if station in j.get("due_index", {})),
                      key=lambda j: j["due_index"][station])
        worst, cumulative = None, 0.0
        for job in mine:
            cumulative += job["work"][station]
            available = max(1.0, job["due_index"][station] - now)      # overdue -> due today
            ratio = cumulative / (capacity * available)
            if worst is None or ratio > worst["ratio"]:
                worst = dict(ratio=ratio, by=index_to_date(max(job["due_index"][station], now + 1)).isoformat(),
                             needed=round(cumulative, 1), fits=round(capacity * available, 1),
                             workdays=round(available, 1))
        total = sum(j["work"][station] for j in mine)
        report[station] = dict(
            load_percent=round(worst["ratio"] * 100) if worst else 0,
            products=len(mine), weighted=round(total, 1), capacity_per_day=capacity,
            days_of_work=round(total / capacity, 1),
            overdue_products=sum(1 for j in mine if j["due_index"][station] <= now),
            tightest=worst)
    return report


def compute(lines, today):
    jobs, skipped = build_jobs(lines)
    backward_schedule(jobs)
    return dict(today=today.isoformat(), stations=station_load(jobs, today),
                jobs=len(jobs), skipped=skipped,
                sizes={k: sum(j["size"] == k for j in jobs) for k in WEIGHTS})


def _lines_from_backup(path):
    import sqlite3
    with sqlite3.connect(path) as db:
        return [json.loads(r[0]) for r in db.execute(
            "SELECT data FROM records WHERE kind='workorderline' AND gone_since IS NULL")]


def main(argv=None):
    import argparse
    from pathlib import Path
    from zoneinfo import ZoneInfo
    from datetime import datetime
    parser = argparse.ArgumentParser(description="Station load report from the nightly backup.")
    parser.add_argument("--db", default=str(Path.home() / "epoptia-backup" / "epoptia.sqlite"))
    parser.add_argument("--sizes", action="store_true", help="list every active product with its size class")
    parser.add_argument("--reference", action="store_true", help="print average size weight per station")
    args = parser.parse_args(argv)
    lines = _lines_from_backup(args.db)
    today = datetime.now(ZoneInfo("Europe/Athens")).date()
    if args.reference:
        print(json.dumps(reference_weights(lines), ensure_ascii=False))
        return 0
    if args.sizes:
        jobs, _ = build_jobs(lines)
        for job in sorted(jobs, key=lambda j: (j["size"], j["description"] or "")):
            print(f"{job['size']:6} {job['wol']:5} {job['description']}  [{job['size_reason']}]")
        return 0
    print(json.dumps(compute(lines, today), ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
