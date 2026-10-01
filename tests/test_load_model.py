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
        self.assertEqual(m.station_weight("ΜΟΝΤΑΖ ΤΖΑΜΙΑ", "large"), 1.0)
        self.assertEqual(m.station_weight("ΚΟΠΗ ΨΑΛΙΔΙ", "large"), 1.0)
        self.assertEqual(m.station_weight("ΚΟΠΗ ΨΑΛΙΔΙ", "small"), 0.5)
        self.assertEqual(m.station_weight("ΜΟΝΤΑΖ 1", "large"), 2.0)


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

    def test_downstream_station_pulls_upstream_deadline_earlier(self):
        jobs, _ = m.build_jobs([line(1, "2026-11-30", [("LASER", False), ("ΜΟΝΤΑΖ ΤΖΑΜΙΑ", False)])])
        m.backward_schedule(jobs)
        self.assertLess(jobs[0]["due"]["LASER"], jobs[0]["due"]["ΜΟΝΤΑΖ ΤΖΑΜΙΑ"])

    def test_archived_and_trade_lines_ignored(self):
        lines = [line(1, "2026-11-30", [("ΜΟΝΤΑΖ 1", False)], status="archive"),
                 line(2, "2026-11-30", [("ΕΙΣΑΓΩΓΗ ΠΑΡΑΓΓΕΛΙΑΣ", False)])]
        self.assertEqual(m.compute(lines, date(2026, 10, 5))["jobs"], 0)


if __name__ == "__main__":
    unittest.main()
