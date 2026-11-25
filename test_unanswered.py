"""Rules for the daily unanswered-ask report. No Slack calls."""

import sqlite3
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import email_reports
import storage
from unanswered import (
    TeamDirectory,
    candidates_from_message,
    collapse_threads,
    due_slot,
    is_action_ask,
    is_case_channel,
    parse_clock,
    rows_for_email,
    still_waiting,
    target_user_ids,
    window_for_slot,
    window_start_before,
)

TZ = ZoneInfo("America/Chicago")
DIRECTORY = TeamDirectory(
    team_ids=["A", "B", "C"],
    bot_ids=["BOT"],
    names={"A": "Ana", "B": "Bob", "C": "Cara", "BOT": "RJL-zap"},
    groups={"S1": ["B", "C"]},
)


def _ts(year, month, day, hour, minute=0):
    return str(datetime(year, month, day, hour, minute, tzinfo=TZ).timestamp())


WINDOW_START = datetime(2026, 11, 20, 16, 0, tzinfo=TZ)  # Friday
WINDOW_END = datetime(2026, 11, 23, 16, 0, tzinfo=TZ)    # Monday


class CaseChannelTests(unittest.TestCase):
    def test_case_names(self):
        self.assertTrue(is_case_channel("smith-1559"))
        self.assertTrue(is_case_channel("lacayoarauzjose-1559"))
        self.assertTrue(is_case_channel("pre-lit-2044"))
        self.assertTrue(is_case_channel("leadbetter-1559"))
        self.assertTrue(is_case_channel("#smith-1559"))

    def test_non_case_names(self):
        for name in (
            "daily-pulse", "leads", "phone", "general", "leads-12",
            "phone-queue-3", "general-1", "intake-99", "random", "",
        ):
            self.assertFalse(is_case_channel(name), name)


class AskTests(unittest.TestCase):
    def test_requests(self):
        asks = [
            "Requesting approval <@B> <@C>",
            "Can you please produce this?",
            "Please see my notes and provide guidance <@B>",
            "Are we working on the Medicare portion? <@B>",
            "Can you update assignment and reopen case? <@B>",
            "<@B> can you file the answer?",
            "please confirm the SOL <@B>",
            "FYI <@B> can you review this today?",
            "<@B> por favor manda el contrato",
            "<@B> let me know when it's calendared",
            "<@B> please review",
            "<@B>?",
            "<@B> asap",
            "just a follow up <@B>",
            "<@B> take a look when you can",
            "¿Puedes revisar esto <@B>?",
        ]
        for text in asks:
            self.assertTrue(is_action_ask(text), text)

    def test_not_requests(self):
        notes = [
            "Saved in correspondence <@B>",
            "3P Demand was faxed to Progressive <@B>",
            "I will follow up before EOW <@B>",
            "Nice. Off to a good start <@B>",
            "Calendared! <@B>",
            "I think we will get 20k next week <@B>",
            "Yes and yes <@B>",
            "FYI <@B> records came in",
            "<@B> <@C> for visibility",
            "Thanks <@B>",
            "got it <@B>",
            "sounds good <@B>",
            "please note <@B> the hearing moved",
            "looping you in <@B>",
            "follow up <@B> for visibility",
            "for your awareness <@B> https://example.com/file?id=1",
            "<@B> the check was deposited",
            "<@B> medical records are in the folder",
            "client called about the deposition <@B>",
            "please see the update <@B>",
            "",
            "<@B>",
            "moved <@B>",
            "Text message sent. <@B>",
            "Received this from State Farm. Already uploaded to DB <@B> <@C>",
            "<@B> <@C> for viz",
            "Calendared her for today at 1:00PM <@B>",
            "All sent via email to client, 1P insurance and chiro <@B> <@C>",
            "<@B> email sent to Holly",
            "Approved -ty <@B>",
            "Liz emailed him yesterday afternoon to schedule zoom signing <@B>",
        ]
        for text in notes:
            self.assertFalse(is_action_ask(text), text)

    def test_caret_depends_on_the_previous_message(self):
        pointer = "<@B> <@C> ^"
        self.assertFalse(is_action_ask(pointer))
        self.assertFalse(is_action_ask(pointer, "Saved in correspondence"))
        self.assertTrue(is_action_ask(pointer, "Can you file the answer?"))
        self.assertTrue(is_action_ask("<@B>", "Can you file the answer?"))
        self.assertFalse(is_action_ask("<@B>", "Saved in correspondence"))


class TargetTests(unittest.TestCase):
    def test_filters_author_bot_and_guests(self):
        text = "<@A> <@B> <@BOT> <@GUEST> can you file this?"
        self.assertEqual(target_user_ids(text, "A", DIRECTORY), ["B"])

    def test_fyi_mention_is_not_an_owner(self):
        text = ("<@B>, can you check in with the client on this? "
                "I can be on the call too, if needed. FYI <@C>")
        self.assertEqual(target_user_ids(text, "A", DIRECTORY), ["B"])

    def test_informed_mention_is_not_an_owner(self):
        text = ("<@C> client confirmed <@B> can you pls calendar "
                "Ryan and Client call 11/30/26 12:00PM")
        self.assertEqual(target_user_ids(text, "A", DIRECTORY), ["B"])

    def test_approval_tags_each_person_asked(self):
        text = "Requesting approval <@B> <@C>"
        self.assertEqual(target_user_ids(text, "A", DIRECTORY), ["B", "C"])

    def test_question_mark_asks_the_tagged_person(self):
        self.assertEqual(target_user_ids("<@B>?", "A", DIRECTORY), ["B"])

    def test_question_in_a_later_sentence_keeps_the_tag(self):
        text = ("<@B> spoke w client - he wanted updates. "
                "What would be the next steps?")
        self.assertEqual(target_user_ids(text, "A", DIRECTORY), ["B"])
        self.assertTrue(is_action_ask(text))

    def test_calendar_subject_is_not_the_owner(self):
        text = ("<@B> can you please assist me with adding holds to "
                "<@C> calendar until we have a confirmed date.")
        self.assertEqual(target_user_ids(text, "A", DIRECTORY), ["B"])

    def test_shared_request_is_one_item(self):
        from unanswered import share_requests
        ts = _ts(2026, 11, 21, 12)
        text = "Requesting a callback <@B> <@C>"
        rows = candidates_from_message(
            {"user": "A", "ts": ts, "text": text},
            DIRECTORY, WINDOW_START, WINDOW_END, "C1", "smith-1559",
        )
        shared = share_requests(rows)
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["target_ids"], ["B", "C"])
        self.assertTrue(shared[0]["action"])

    def test_expands_user_groups_once(self):
        text = "<!subteam^S1> <@B> please confirm"
        self.assertEqual(target_user_ids(text, "A", DIRECTORY), ["B", "C"])


class WindowTests(unittest.TestCase):
    def test_monday_includes_weekend(self):
        slot = datetime(2026, 11, 23, 16, 0, tzinfo=TZ)
        start, end = window_for_slot(slot)
        self.assertEqual(start, datetime(2026, 11, 20, 16, 0, tzinfo=TZ))
        self.assertEqual(end, slot)

    def test_tuesday_starts_monday(self):
        slot = datetime(2026, 11, 24, 16, 0, tzinfo=TZ)
        start, _end = window_for_slot(slot)
        self.assertEqual(start.date().isoformat(), "2026-11-23")

    def test_test_window_starts_at_previous_send_time(self):
        morning = datetime(2026, 11, 25, 9, 54, tzinfo=TZ)  # Wednesday
        self.assertEqual(
            window_start_before(morning, 16, 0),
            datetime(2026, 11, 24, 16, 0, tzinfo=TZ),
        )
        evening = datetime(2026, 11, 25, 17, 0, tzinfo=TZ)
        self.assertEqual(
            window_start_before(evening, 16, 0),
            datetime(2026, 11, 25, 16, 0, tzinfo=TZ),
        )
        monday = datetime(2026, 11, 23, 10, 0, tzinfo=TZ)
        self.assertEqual(
            window_start_before(monday, 16, 0),
            datetime(2026, 11, 20, 16, 0, tzinfo=TZ),
        )

    def test_friday_starts_thursday(self):
        slot = datetime(2026, 11, 20, 16, 0, tzinfo=TZ)
        start, _end = window_for_slot(slot)
        self.assertEqual(start.date().isoformat(), "2026-11-19")

    def test_due_and_catchup(self):
        monday_430 = datetime(2026, 11, 23, 16, 30, tzinfo=TZ)
        self.assertEqual(
            due_slot(monday_430, 16, 0, 0)[0], "send")
        self.assertEqual(
            due_slot(monday_430, 16, 0, monday_430.timestamp()), None)

        # Before today's send, a missed Friday slot is too old to mail.
        monday_3 = datetime(2026, 11, 23, 15, 0, tzinfo=TZ)
        self.assertEqual(due_slot(monday_3, 16, 0, 0)[0], "skip")

        # Tuesday morning still owes Monday's report.
        tuesday_9 = datetime(2026, 11, 24, 9, 0, tzinfo=TZ)
        monday_slot = datetime(2026, 11, 23, 16, 0, tzinfo=TZ).timestamp()
        self.assertEqual(due_slot(tuesday_9, 16, 0, 0)[0], "send")
        self.assertIsNone(due_slot(tuesday_9, 16, 0, monday_slot + 60))

    def test_recent_request_stays_listed_until_it_is_overdue(self):
        from unanswered import default_thresholds, mark_priority
        created = datetime(2026, 11, 25, 10, 20, tzinfo=TZ)
        now = datetime(2026, 11, 25, 10, 29, tzinfo=TZ)
        row = {
            "created_at": created.timestamp(),
            "kind": "action",
            "status": "open",
            "text": "Can you file the answer?",
        }
        mark_priority(row, now, default_thresholds())
        self.assertFalse(row["overdue"])
        self.assertEqual(row["status_label"], "Awaiting action")
        later = datetime(2026, 11, 25, 15, 0, tzinfo=TZ)
        mark_priority(row, later, default_thresholds())
        self.assertTrue(row["overdue"])
        self.assertEqual(row["status_label"], "Overdue")

    def test_explicit_deadline_is_not_overdue_early(self):
        from unanswered import default_thresholds, mark_priority
        created = datetime(2026, 11, 25, 10, 20, tzinfo=TZ)
        row = {
            "created_at": created.timestamp(),
            "kind": "action",
            "status": "open",
            "text": "<@B> can you file this by Friday?",
        }
        same_afternoon = datetime(2026, 11, 25, 15, 0, tzinfo=TZ)
        mark_priority(row, same_afternoon, default_thresholds())
        self.assertFalse(row["overdue"])
        after = datetime(2026, 11, 30, 10, 0, tzinfo=TZ)
        mark_priority(row, after, default_thresholds())
        self.assertTrue(row["overdue"])

    def test_parse_clock(self):
        self.assertEqual(parse_clock("4:00 PM"), (16, 0))
        self.assertEqual(parse_clock("16:00"), (16, 0))
        self.assertEqual(parse_clock("4pm"), (16, 0))
        self.assertEqual(parse_clock(""), (16, 0))


class ResponseTests(unittest.TestCase):
    def _row(self, ts, target="B", thread=None):
        return {
            "channel_id": "C1",
            "message_ts": ts,
            "thread_ts": thread or ts,
            "target_id": target,
            "author_id": "A",
            "text": "<@B> can you file this?",
            "created_at": float(ts),
            "channel_name": "smith-1559",
        }

    def test_reply_reaction_and_someone_else(self):
        ask_ts = _ts(2026, 11, 21, 12)
        ask = {"ts": ask_ts, "user": "A", "text": "<@B> can you file this?"}
        reply = {"ts": _ts(2026, 11, 21, 13), "user": "B", "text": "on it"}
        row = self._row(ask_ts)
        key = ("C1", ask_ts)
        held = still_waiting([row], {key: [ask, reply]})
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["status"], "acknowledged")

        other = {"ts": _ts(2026, 11, 21, 13), "user": "C", "text": "I can"}
        self.assertEqual(still_waiting([row], {key: [ask, other]}), [row])

        reacted = dict(ask)
        reacted["reactions"] = [{"name": "white_check_mark", "users": ["B"]}]
        self.assertEqual(still_waiting([row], {key: [reacted]}), [])

        # Couldn't read the thread: keep it so a Slack error doesn't hide it.
        self.assertEqual(still_waiting([row], {}), [row])
        # Thread loaded and the message is gone.
        self.assertEqual(still_waiting([row], {key: [other]}), [])

    def test_unrelated_channel_reply_stays_open(self):
        ask_ts = _ts(2026, 11, 21, 12)
        later = _ts(2026, 11, 21, 14)
        earlier = _ts(2026, 11, 21, 11)
        ask = {"ts": ask_ts, "user": "A", "text": "<@B> can you file the answer?"}
        row = self._row(ask_ts)
        row["text"] = ask["text"]
        key = ("C1", ask_ts)
        channel_reply = {"ts": later, "user": "B", "text": "I'll take a look at the depo"}
        self.assertEqual(len(still_waiting(
            [row], {key: [ask]}, {"C1": [ask, channel_reply]})), 1)
        called = {"ts": later, "user": "C", "text": "Called the client back at 9:29"}
        callback = dict(row)
        callback["text"] = "Requesting a callback about the signing"
        callback["kind"] = "callback"
        callback["action"] = "Callback about the signing"
        self.assertEqual(
            still_waiting([callback], {key: [ask]}, {"C1": [ask, called]}),
            [],
        )
        finished = {"ts": later, "user": "C", "text": "Filed the answer this morning"}
        self.assertEqual(
            still_waiting([row], {key: [ask]}, {"C1": [ask, finished]}),
            [],
        )
        # A later message from someone else leaves Bob on the list.
        someone_else = {"ts": later, "user": "C", "text": "I can grab it"}
        self.assertEqual(
            still_waiting([row], {key: [ask]}, {"C1": [ask, someone_else]}),
            [row],
        )
        # A channel message from Bob before the ask is not a response.
        before = {"ts": earlier, "user": "B", "text": "morning"}
        self.assertEqual(
            still_waiting([row], {key: [ask]}, {"C1": [before, ask]}),
            [row],
        )

    def test_earlier_reply_does_not_clear_a_later_ask(self):
        first = _ts(2026, 11, 21, 12)
        second = _ts(2026, 11, 21, 15)
        reply = _ts(2026, 11, 21, 13)
        messages = [
            {"ts": first, "user": "A", "text": "<@B> can you file this?"},
            {"ts": reply, "user": "B", "text": "done"},
            {"ts": second, "user": "A", "text": "<@B> can you also send the bill?"},
        ]
        rows = [self._row(first), self._row(second, thread=first)]
        kept = still_waiting(rows, {("C1", first): messages})
        self.assertEqual([r["message_ts"] for r in kept], [second])

    def test_collapse_same_thread(self):
        first = _ts(2026, 11, 21, 12)
        second = _ts(2026, 11, 21, 15)
        rows = [self._row(first), self._row(second, thread=first)]
        collapsed = collapse_threads(rows)
        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0]["message_ts"], second)

    def test_same_subject_collapses_to_one_item(self):
        from unanswered import consolidate_requests
        first = _ts(2026, 11, 21, 12)
        second = _ts(2026, 11, 21, 15)
        rows = [
            self._row(first),
            {
                **self._row(second),
                "thread_ts": second,
                "text": "Please send the disengagement letter",
                "kind": "action",
            },
            {
                **self._row(second),
                "message_ts": _ts(2026, 11, 21, 16),
                "thread_ts": _ts(2026, 11, 21, 16),
                "text": "Need approval to disengage",
                "kind": "approval",
                "created_at": float(_ts(2026, 11, 21, 16)),
            },
        ]
        rows[0]["text"] = "Send the disengagement letter today"
        merged = consolidate_requests(rows)
        self.assertEqual(len(merged), 1)


class CandidateTests(unittest.TestCase):
    def test_in_window_ask_and_skips(self):
        ts = _ts(2026, 11, 21, 12)
        rows = candidates_from_message(
            {"user": "A", "ts": ts, "text": "<@B> can you file this?"},
            DIRECTORY, WINDOW_START, WINDOW_END, "C1", "smith-1559",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target_id"], "B")
        self.assertEqual(rows[0]["thread_ts"], ts)

        self.assertEqual(candidates_from_message(
            {"user": "A", "ts": ts, "text": "FYI <@B> records are in"},
            DIRECTORY, WINDOW_START, WINDOW_END, "C1", "smith-1559",
        ), [])
        self.assertEqual(candidates_from_message(
            {"user": "BOT", "bot_id": "B1", "ts": ts,
             "text": "<@B> please confirm"},
            DIRECTORY, WINDOW_START, WINDOW_END, "C1", "smith-1559",
        ), [])
        self.assertEqual(candidates_from_message(
            {"user": "BOT", "app_id": "A1", "ts": ts,
             "text": "<@B> please confirm"},
            DIRECTORY, WINDOW_START, WINDOW_END, "C1", "smith-1559",
        ), [])
        self.assertEqual(candidates_from_message(
            {"user": "A", "bot_profile": {"name": "Zap"}, "ts": ts,
             "text": "<@B> please confirm"},
            DIRECTORY, WINDOW_START, WINDOW_END, "C1", "smith-1559",
        ), [])
        self.assertEqual(candidates_from_message(
            {"user": "A", "ts": ts, "text": "<@BOT> please confirm"},
            DIRECTORY, WINDOW_START, WINDOW_END, "C1", "smith-1559",
        ), [])
        old = _ts(2026, 11, 19, 12)
        self.assertEqual(candidates_from_message(
            {"user": "A", "ts": old, "text": "<@B> can you file this?"},
            DIRECTORY, WINDOW_START, WINDOW_END, "C1", "smith-1559",
        ), [])

    def test_user_group_and_thread_reply(self):
        parent = _ts(2026, 11, 18, 12)  # before the window
        reply_ts = _ts(2026, 11, 21, 12)
        rows = candidates_from_message(
            {"user": "A", "ts": reply_ts, "thread_ts": parent,
             "text": "<!subteam^S1> please confirm the hearing"},
            DIRECTORY, WINDOW_START, WINDOW_END, "C1", "smith-1559",
        )
        self.assertEqual(sorted(r["target_id"] for r in rows), ["B", "C"])
        self.assertTrue(all(r["thread_ts"] == parent for r in rows))

    def test_pointer_keeps_the_original_tags(self):
        ts = _ts(2026, 11, 21, 12)
        rows = candidates_from_message(
            {"user": "A", "ts": ts, "text": "<@B> <@C> ^"},
            DIRECTORY, WINDOW_START, WINDOW_END, "C1", "smith-1559",
            "Can you file the answer?",
        )
        self.assertEqual(sorted(r["target_id"] for r in rows), ["B", "C"])
        self.assertTrue(all(r["text"] == "<@B> <@C> ^" for r in rows))
        self.assertTrue(all("file the answer" in r["context"] for r in rows))


class EmailTests(unittest.TestCase):
    def test_html_summary_then_person_with_most_open(self):
        html = email_reports.build_unanswered_email_html(
            "Fri Nov 20 4:00 PM – Mon Nov 23 4:00 PM CT",
            [{
                "waiting_id": "B",
                "waiting_name": "Bob",
                "asked_by": "Ana",
                "channel_id": "C2",
                "channel_name": "smith-1559",
                "channel_link": "https://ramosjames.slack.com/archives/C2",
                "when": "Sat 12:00 PM",
                "when_ts": 2,
                "snippet": "can you file this?",
                "link": "https://ramosjames.slack.com/archives/C2/p100",
            }, {
                "waiting_id": "B",
                "waiting_name": "Bob",
                "asked_by": "Ana",
                "channel_id": "C2",
                "channel_name": "smith-1559",
                "channel_link": "https://ramosjames.slack.com/archives/C2",
                "when": "Sat 1:00 PM",
                "when_ts": 3,
                "snippet": "and this follow up",
                "link": "https://ramosjames.slack.com/archives/C2/p101",
            }, {
                "waiting_id": "B",
                "waiting_name": "Bob",
                "asked_by": "Jon",
                "channel_id": "C1",
                "channel_name": "jones-100",
                "channel_link": "https://ramosjames.slack.com/archives/C1",
                "when": "Fri 5:00 PM",
                "when_ts": 1,
                "snippet": "please calendar this",
                "link": "https://ramosjames.slack.com/archives/C1/p200",
            }, {
                "waiting_ids": ["C"],
                "waiting_names": ["Cara"],
                "asked_by": "Jon",
                "channel_id": "C9",
                "channel_name": "zeta-9",
                "when": "Sat 2:00 PM",
                "when_ts": 4,
                "snippet": "one for cara",
                "link": "https://ramosjames.slack.com/archives/C9/p300",
            }],
            channels_scanned=12,
        )
        summary, detail = html.split("2px solid #1f2937", 1)
        self.assertLess(summary.index(">Bob<"), summary.index(">Cara<"))
        self.assertIn(">3<", summary)
        self.assertIn(">1<", summary)
        self.assertLess(detail.index("#jones-100"), detail.index("#smith-1559"))
        self.assertLess(detail.index("can you file this?"), detail.index("and this follow up"))
        self.assertLess(detail.index("and this follow up"), detail.index("#zeta-9"))
        self.assertIn(
            '<a href="https://ramosjames.slack.com/archives/C2/p100" '
            'style="color:#1f2937;text-decoration:none">can you file this?</a>',
            html,
        )
        self.assertNotIn("#e8385a", html)
        self.assertIn("Scanned 12 case channels", html)
        self.assertEqual(email_reports.unique_open_count([
            {"request_key": "a"}, {"request_key": "a"}, {"request_key": "b"},
        ]), 2)

    def test_shared_comment_is_listed_for_each_person(self):
        html = email_reports.build_unanswered_email_html("window", [{
            "request_key": "C1:1",
            "waiting_ids": ["B", "C"],
            "waiting_names": ["Bob", "Cara"],
            "asked_by": "Ana",
            "channel_id": "C1",
            "channel_name": "smith-1559",
            "when": "Sat 12:00 PM",
            "when_ts": 1,
            "snippet": "both of you",
            "link": "https://ramosjames.slack.com/archives/C1/p100",
        }])
        self.assertEqual(html.count("both of you"), 2)
        self.assertNotIn("Bob or Cara", html)
        self.assertIn(">Bob<", html)
        self.assertIn(">Cara<", html)
        self.assertIn(">1<", html)
        self.assertEqual(email_reports.unique_open_count([{
            "request_key": "C1:1",
        }, {
            "request_key": "C1:1",
        }]), 1)

    def test_slack_mentions_phones_and_links_stay_readable(self):
        html = email_reports.slack_message_html(
            "Call <tel:+18442928615|+18442928615> <@B|Bob> "
            "<https://example.com/file|the file>",
            DIRECTORY,
        )
        self.assertIn("@Bob", html)
        self.assertIn("background-color:#e8f5fa", html)
        self.assertIn("tel:+18442928615", html)
        self.assertIn("+18442928615", html)
        self.assertIn("https://example.com/file", html)
        self.assertIn("the file", html)
        self.assertNotIn("<tel:", html)

    def test_rows_merge_one_comment_and_skip_bots(self):
        ts = _ts(2026, 11, 21, 12)
        parent = _ts(2026, 11, 18, 12)
        waiting = [
            {
                "channel_id": "C1", "channel_name": "smith-1559",
                "message_ts": ts, "thread_ts": parent,
                "author_id": "A", "target_id": "B",
                "text": "<@B> <@C> can you file this?",
                "created_at": float(ts),
            },
            {
                "channel_id": "C1", "channel_name": "smith-1559",
                "message_ts": ts, "thread_ts": parent,
                "author_id": "A", "target_id": "C",
                "text": "<@B> <@C> can you file this?",
                "created_at": float(ts),
            },
            {
                "channel_id": "C1", "channel_name": "smith-1559",
                "message_ts": parent, "thread_ts": parent,
                "author_id": "BOT", "target_id": "B",
                "text": "<@B> please confirm",
                "created_at": float(parent),
            },
        ]
        rows = rows_for_email(
            waiting, DIRECTORY, "https://ramosjames.slack.com", TZ)
        self.assertEqual(sorted(r["waiting_name"] for r in rows), ["Bob", "Cara"])
        self.assertEqual(rows[0]["asked_by"], "Ana")
        self.assertEqual({r["request_key"] for r in rows}, {f"C1:{ts}"})
        self.assertIn("@Bob", rows[0]["message_html"])
        self.assertIn("@Cara", rows[0]["message_html"])
        self.assertIn("can you file this?", rows[0]["message_html"])
        self.assertIn("thread_ts=" + parent, rows[0]["link"])
        self.assertIn("/archives/C1/p", rows[0]["link"])
        html = email_reports.build_unanswered_email_html("window", rows)
        self.assertEqual(html.count("can you file this?"), 2)
        self.assertEqual(email_reports.unique_open_count(rows), 1)
        self.assertEqual(
            email_reports.unanswered_person_counts(rows),
            [("Bob", 1), ("Cara", 1)],
        )

    def test_all_clear(self):
        html = email_reports.build_unanswered_email_html("window", [])
        self.assertIn("No unresolved requests", html)

    def test_escapes_snippet(self):
        html = email_reports.build_unanswered_email_html("window", [{
            "waiting_id": "B",
            "waiting_name": "Bob",
            "asked_by": "Ana",
            "channel_name": "smith-1559",
            "when": "Sat 12:00 PM",
            "snippet": "<script>",
            "link": "",
        }])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class SchemaTests(unittest.TestCase):
    def test_unanswered_table_creates(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(storage.SCHEMA)
        cols = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(unanswered_asks)"
            )
        }
        self.assertIn("target_id", cols)
        self.assertIn("message_ts", cols)
        conn.execute(
            "INSERT INTO unanswered_asks "
            "(channel_id, message_ts, thread_ts, author_id, target_id, text, "
            " channel_name, created_at) VALUES ('C', '1.2', '1.2', 'A', 'B', "
            "'please', 'smith-1', 1.2)"
        )
        conn.execute(
            "INSERT INTO unanswered_asks "
            "(channel_id, message_ts, thread_ts, author_id, target_id, text, "
            " channel_name, created_at) VALUES ('C', '1.2', '1.2', 'A', 'B', "
            "'please again', 'smith-1', 1.2) "
            "ON CONFLICT(channel_id, message_ts, target_id) DO UPDATE SET "
            "text = excluded.text"
        )
        text = conn.execute(
            "SELECT text FROM unanswered_asks"
        ).fetchone()[0]
        self.assertEqual(text, "please again")


if __name__ == "__main__":
    unittest.main()
