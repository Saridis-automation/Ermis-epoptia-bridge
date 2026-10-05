"""Station load model: calendar, size/station weights, partial completion, deadlines."""
import unittest
from datetime import date

import dashboard.load_model as m


def line(wol, delivery, steps, description="Τραπέζι εργασίας", dims="150x70x86 cm", qty=1, status="production"):
    routing, prev = [], []
    for i, (station, done) in enumerate(steps):
        element = wol * 100 + i
        routing.append(dict(elementId=element, previous=prev, next=[], workstationName=station,
                            status="completed" if done else "not_started"))
        prev = [element]
    return dict(workorderline_id=wol, target_day=delivery, production_status=status, quantity=qty,
                description=description, comments="", erp_routing=routing,
                customFields=[dict(id=12, name="ΔΙΑΣΤΑΣΕΙΣ", value=dims)])


class CalendarTests(unittest.TestCase):
    def test_orthodox_easter_and_holidays(self):
        self.assertEqual(m.orthodox_easter(2026), date(2026, 4, 12))
        self.assertEqual(m.orthodox_easter(2027), date(2027, 5, 2))
        self.assertIn(date(2026, 6, 1), m.greek_holidays(2026))        # Αγίου Πνεύματος
        self.assertFalse(m.is_workday(date(2026, 10, 28)))
        self.assertTrue(m.is_workday(date(2026, 10, 26)))              # Αγ. Δημητρίου: εργάσιμη

    def test_workdays_skip_weekends_and_holidays(self):
        self.assertEqual(m.workdays_between(date(2026, 10, 26), date(2026, 11, 1)), 4)


class ShiftTests(unittest.TestCase):
    def test_remaining_share_of_shift(self):
        from datetime import datetime
        self.assertEqual(m.remaining_share_of_today(datetime(2026, 10, 2, 6, 0)), 1.0)
        self.assertAlmostEqual(m.remaining_share_of_today(datetime(2026, 10, 2, 11, 45)), 0.5)
        self.assertEqual(m.remaining_share_of_today(datetime(2026, 10, 2, 17, 0)), 0.0)
        self.assertEqual(m.remaining_share_of_today(datetime(2026, 10, 3, 10, 0)), 0.0)   # Saturday
        self.assertEqual(m.remaining_share_of_today(date(2026, 10, 2)), 1.0)

    def test_evening_counts_less_than_morning(self):
        from datetime import datetime
        # Deadline beyond the minimum window, so the shift share still matters.
        lines = [line(i, "2026-10-14", [("ΜΟΝΤΑΖ 1", False)]) for i in range(1, 11)]
        morning = m.compute(lines, datetime(2026, 10, 2, 7, 0))["stations"]["ΜΟΝΤΑΖ 1"]["load_percent"]
        evening = m.compute(lines, datetime(2026, 10, 2, 20, 0))["stations"]["ΜΟΝΤΑΖ 1"]["load_percent"]
        self.assertGreater(evening, morning)


class SizeAndWeightTests(unittest.TestCase):
    def test_size_rules(self):
        self.assertEqual(m.size_class(line(1, "2026-11-01", [], "Ψυγείο βιτρίνα συντήρησης BXM72"))[0], "large")
        self.assertEqual(m.size_class(line(1, "2026-11-01", [], "Πλάτη ΙΝΟΧ Ειδικό", "220x40 cm"))[0], "small")
        self.assertEqual(m.size_class(line(1, "2026-11-01", [], "Τροχήλατο καρότσι", "46x62x77 cm"))[0], "normal")
        self.assertEqual(m.size_class(line(1, "2026-11-01", [], "Λάντζα διπλή", "270x70x86 cm"))[0], "large")
        bench = line(1, "2026-11-01", [], "Ψυγείο πάγκος PS62")
        self.assertEqual(m.size_class(bench)[0], "normal")
        bench["comments"] = "1 διπλή συρταριέρα"
        self.assertEqual(m.size_class(bench)[0], "large")

    def test_station_specific_weights(self):
        shelf = line(1, "2026-11-01", [], "Επιτοίχιο ράφι ΡΤ121", "120x30 cm")
        showcase = line(2, "2026-11-01", [], "Ψυγείο βιτρίνα συντήρησης BXM72")
        bench = line(3, "2026-11-01", [], "Ψυγείο πάγκος συντήρησης PSM74")
        bench_drawers = dict(bench, comments="1 διπλή συρταριέρα")
        freezer_bench = line(4, "2026-11-01", [], "Ψυγείο πάγκος κατάψυξης PK62")
        cabinet2 = dict(line(5, "2026-11-01", [], "Ψυγείο θάλαμος συντήρησης"),
                        comments="2 ανοιγόμενες πόρτες βαρέως τύπου")
        glass = line(6, "2026-11-01", [], "Ψυγείο θάλαμος συντήρησης S72G GLASS")
        self_service = line(7, "2026-11-01", [], "Ψυγείο SELF SERVICE SS150M70")
        self.assertEqual(m.station_weight("ΜΟΝΤΑΖ ΤΖΑΜΙΑ", showcase), 1.0)
        self.assertEqual(m.station_weight("ΚΟΠΗ ΨΑΛΙΔΙ", shelf), 0.2)
        self.assertEqual(m.station_weight("ΚΟΠΗ ΨΑΛΙΔΙ", showcase), 1.0)
        self.assertEqual(m.station_weight("ΣΤΡΑΝΤΖΑ", shelf), 0.5)
        self.assertEqual(m.station_weight("LASER", showcase), 1.5)
        self.assertEqual(m.station_weight("LASER", bench_drawers), 2.0)
        self.assertEqual(m.station_weight("LASER", bench), 1.0)
        self.assertEqual(m.station_weight("ΨΥΚΤΙΚΑ", showcase), 1.5)
        self.assertEqual(m.station_weight("ΨΥΚΤΙΚΑ", freezer_bench), 1.5)
        self.assertEqual(m.station_weight("ΜΟΝΤΑΖ 2", bench), 1.0)
        self.assertEqual(m.station_weight("ΜΟΝΤΑΖ 2", cabinet2), 1.5)
        self.assertEqual(m.station_weight("ΜΟΝΤΑΖ 2", glass), 1.5)
        self.assertEqual(m.station_weight("ΜΟΝΤΑΖ 2", self_service), 2.0)
        self.assertEqual(m.station_weight("ΜΟΝΤΑΖ 2", bench_drawers), 2.0)
        self.assertEqual(m.station_weight("ΜΟΝΤΑΖ 1", showcase), 2.0)
        self.assertEqual(m.station_weight("ΜΟΝΤΑΖ 2", showcase), 1.0)
        heated = line(8, "2026-11-01", [], "Θερμή βιτρίνα BTH72 Ειδικό")
        self.assertEqual(m.station_weight("ΨΥΚΤΙΚΑ", heated), 0.3)
        self.assertEqual(m.station_weight("ΨΥΚΤΙΚΑ", line(9, "2026-11-01", [], "Θερμοθάλαμος Ειδικός")), 0.3)


class LoadTests(unittest.TestCase):
    def test_partial_completion_counts_remaining_share(self):
        steps = [("ΜΟΝΤΑΖ 1", True), ("ΜΟΝΤΑΖ 1", True), ("ΜΟΝΤΑΖ 1", True), ("ΜΟΝΤΑΖ 1", False)]
        jobs, _ = m.build_jobs([line(1, "2026-11-30", steps)])
        self.assertAlmostEqual(jobs[0]["work"]["ΜΟΝΤΑΖ 1"], 0.25)

    def test_same_work_tighter_deadline_is_higher_load(self):
        today = date(2026, 10, 5)
        def load(delivery):
            lines = [line(i, delivery, [("ΜΟΝΤΑΖ 1", False)]) for i in range(1, 61)]
            return m.compute(lines, today)["stations"]["ΜΟΝΤΑΖ 1"]["load_percent"]
        self.assertGreater(load("2026-10-16"), 100)       # 60 products in ~9 workdays
        self.assertLess(load("2026-11-30"), 100)          # same 60 in ~40 workdays

    def test_overdue_work_is_judged_against_the_minimum_window(self):
        # 10 overdue normal products at ΜΟΝΤΑΖ 1 (4.55 weighted/day): one day would read 220%.
        lines = [line(i, "2026-09-01", [("ΜΟΝΤΑΖ 1", False)]) for i in range(1, 11)]
        report = m.compute(lines, date(2026, 10, 5))["stations"]["ΜΟΝΤΑΖ 1"]
        capacity = m.CAPACITY_PER_DAY["ΜΟΝΤΑΖ 1"] * m.MIN_WINDOW_WORKDAYS
        self.assertEqual(m.MIN_WINDOW_WORKDAYS, 5.0)
        self.assertEqual(report["load_percent"], round(10 / capacity * 100))
        self.assertEqual(report["tightest"]["workdays"], 5.0)
        self.assertEqual(report["overdue_products"], 10)

    def test_downstream_station_pulls_upstream_deadline_earlier(self):
        jobs, _ = m.build_jobs([line(1, "2026-11-30", [("LASER", False), ("ΜΟΝΤΑΖ ΤΖΑΜΙΑ", False)])])
        m.backward_schedule(jobs)
        self.assertLess(jobs[0]["due"]["LASER"], jobs[0]["due"]["ΜΟΝΤΑΖ ΤΖΑΜΙΑ"])

    def test_products_to_produce_counts_started_and_unstarted_not_trade(self):
        lines = [line(1, "2026-11-30", [("ΜΟΝΤΑΖ 1", True), ("ΜΟΝΤΑΖ 1", False)]),        # half done
                 line(2, None, [("LASER", False)], qty=2),                              # not started, no date
                 line(3, "2026-11-30", [("ΜΟΝΤΑΖ 1", True)]),                            # finished, not archived
                 line(4, "2026-11-30", [("ΕΙΣΑΓΩΓΗ ΠΑΡΑΓΓΕΛΙΑΣ", False)]),                # trade
                 line(5, "2026-11-30", [("ΜΟΝΤΑΖ 1", False)], status="archive")]
        self.assertEqual(m.products_to_produce(lines), 3)

    def test_archived_and_trade_lines_ignored(self):
        lines = [line(1, "2026-11-30", [("ΜΟΝΤΑΖ 1", False)], status="archive"),
                 line(2, "2026-11-30", [("ΕΙΣΑΓΩΓΗ ΠΑΡΑΓΓΕΛΙΑΣ", False)])]
        self.assertEqual(m.compute(lines, date(2026, 10, 5))["jobs"], 0)


if __name__ == "__main__":
    unittest.main()
