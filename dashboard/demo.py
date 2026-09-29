"""Explicitly fictional fixtures, replaced entirely when a provider succeeds."""


def demo_snapshot(now):
    return {
        "observed_at": now.isoformat(), "overall_load_percent": 72,
        "workstations": [dict(name=f"Σταθμός επίδειξης {i + 1}", load_percent=load,
                              pending_count=pending, priority=priority)
                         for i, (load, pending, priority) in enumerate(
                             [(82, 14, True), (64, 9, False), (91, 18, True),
                              (48, 6, False), (75, 11, False), (69, 8, True)])],
        "urgent_orders": [dict(entity_type="workorder", id=i, code=f"DEMO-00{i}",
                               customer=f"Πελάτης επίδειξης {i}", completion_percent=progress,
                               deadline=now.date().isoformat())
                          for i, progress in [(1, 78), (2, 46), (3, 62)]],
        "active_production": {"active_workorders_total": 42,
                              "native_active_production_progress_source": {"complete": True}},
        "today": {"overdue_work": 5, "completed_today": 12},
    }
