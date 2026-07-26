from __future__ import annotations

import unittest

from ccb import CCBWorkflow, discover_services, normalize_roster, normalize_service_plan


class CCBNormalizationTests(unittest.TestCase):
    def test_discovers_events_as_individual_services(self):
        services = discover_services(
            [
                {
                    "id": 50,
                    "name": "Sunday Services",
                    "events": [
                        {"id": 801, "name": "8am", "start": "2026-08-02T08:00:00+10:00", "end": "2026-08-02T09:30:00+10:00", "service_plan_id": 9},
                        {"id": 802, "name": "10am", "start": "2026-08-02T10:00:00+10:00", "end": "2026-08-02T11:30:00+10:00", "service_plan_id": 10},
                    ],
                }
            ],
            7,
        )

        self.assertEqual([item.event_id for item in services], [801, 802])
        self.assertEqual(services[1].schedule_id, 50)
        self.assertEqual(services[1].category_id, 7)
        self.assertEqual(services[1].service_plan_id, 10)

    def test_roster_normalization_keeps_status_and_candidate_pool(self):
        event = {
            "id": 801,
            "event_teams": [
                {
                    "team": {"id": 2, "name": "Worship", "positions": [{"id": 3, "name": "Frontline"}]},
                    "event_positions": [
                        {
                            "id": 99,
                            "position_id": 3,
                            "position": {"id": 3, "name": "Frontline"},
                            "assignments": [
                                {"id": 1, "status": "PENDING", "volunteer": {"individual": {"id": 11, "name": "Alex Singer", "email": "alex@example.test"}}},
                                {"id": 2, "status": "DECLINED", "volunteer": {"individual": {"id": 12, "name": "Declined Singer"}}},
                            ],
                        }
                    ],
                }
            ],
        }

        roster = normalize_roster(
            event,
            candidate_loader=lambda _event_position_id: [
                {"individual": {"id": 13, "name": "Eligible Singer", "email": "eligible@example.test"}}
            ],
            candidate_position_names={"Frontline"},
        )

        assignments = roster["positions"][0]["assignments"]
        self.assertTrue(assignments[0]["grants_access"])
        self.assertFalse(assignments[1]["grants_access"])
        self.assertEqual(roster["eligible_people"][0]["id"], 13)

    def test_runsheet_calculates_item_start_and_end_from_offset(self):
        event = {"id": 801, "start": "2026-08-02T08:00:00+10:00"}
        plan = {
            "id": 9,
            "name": "Morning Service",
            "event_starttime_offset": -600,
            "duration": 420,
            "items": [
                {"id": 2, "order_by": 2, "item_type": "ITEM", "name": "Song", "duration": 300},
                {"id": 1, "order_by": 1, "item_type": "ITEM", "name": "Welcome", "duration": 120},
            ],
        }

        result = normalize_service_plan(plan, event)

        self.assertEqual([item["name"] for item in result["items"]], ["Welcome", "Song"])
        self.assertEqual(result["items"][0]["start"], "2026-08-02T07:50:00+10:00")
        self.assertEqual(result["items"][0]["end"], "2026-08-02T07:52:00+10:00")
        self.assertEqual(result["items"][1]["end"], "2026-08-02T07:57:00+10:00")
        self.assertEqual(result["calculated_duration_seconds"], 420)

    def test_pull_all_reuses_full_event_for_branches_with_same_source(self):
        class FakeClient:
            def __init__(self):
                self.event_calls = 0

            def event(self, **_kwargs):
                self.event_calls += 1
                return (
                    {"id": 50},
                    {"id": 801, "start": "2026-08-02T08:00:00+10:00", "service_plan_id": 9, "event_teams": []},
                )

            def service_plans(self, _category_id):
                return [{"id": 9, "event_starttime_offset": 0, "duration": 0, "items": []}]

            def event_position_candidates(self, _event_position_id):
                return []

        client = FakeClient()
        workflow = CCBWorkflow(client)  # type: ignore[arg-type]
        service = {"event_id": 801, "schedule_id": 50, "category_id": 7}

        result = workflow.pull(selected_service=service, sources={"roster": service, "runsheet": service})

        self.assertTrue(result["ok"])
        self.assertEqual(client.event_calls, 1)


if __name__ == "__main__":
    unittest.main()
