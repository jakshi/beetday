#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# ///
"""Unit tests for the pure (offline) parts of beetday.

Run: `python3 -m unittest test_beetday -v`  (or `uv run test_beetday.py`).
The network layer is exercised separately against a live session.

Every fixture below is invented. Names are famous-dead-computer-scientist
placeholders, addresses use the reserved `example.com` domain, and the tenant is
`acme` on `wd999.myworkday.com` — no real person, tenant or host appears here.
"""

import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

import beetday as bd


class ManagerFromOrganization(unittest.TestCase):
    def test_extracts_trailing_parenthesised_name(self):
        self.assertEqual(
            bd.manager_from_organization("Engineering : Platform II (Grace Hopper)"),
            "Grace Hopper",
        )

    def test_last_group_wins_and_handles_missing(self):
        self.assertEqual(bd.manager_from_organization("A (Bar) (Baz Qux)"), "Baz Qux")
        self.assertIsNone(bd.manager_from_organization("No parentheses here"))
        self.assertIsNone(bd.manager_from_organization(None))


class SearchNdjson(unittest.TestCase):
    NDJSON = (
        '{"type":"UserTypes","manager":{"meetsCriteria":false}}\n'
        '{"type":"SearchResultSet","source":"workday_search:workday_staffing","results":[{"instanceID":"247$101","description":"Alan Turing","subtitle1":"Senior Tech Lead","subtitle2":"Engineering : Platform II (Grace Hopper)","subtitle3":"London"}]}\n'
        # same person from another source -> must de-duplicate; an org result -> must be ignored
        '{"type":"SearchResultSet","source":"workday_search:workday_people","results":[{"instanceID":"247$101","description":"Alan Turing","subtitle1":"Senior Tech Lead"}]}\n'
        '{"type":"SearchResultSet","source":"workday_search:workday_organizations","results":[{"instanceID":"2500$11","description":"Engineering : Platform II (Grace Hopper)"}]}\n'
    )

    def test_parses_dedupes_and_filters_to_people(self):
        people = bd.people_from_search_ndjson(self.NDJSON)
        self.assertEqual(len(people), 1)
        person = people[0]
        self.assertEqual(person.name, "Alan Turing")
        self.assertEqual(person.worker_id, "247$101")
        self.assertEqual(person.title, "Senior Tech Lead")
        self.assertEqual(person.location, "London")
        self.assertEqual(person.manager, "Grace Hopper")


def _detail(instance_id, instance_text, owner_id, owner_name, title, location, has_children):
    return {
        "hasChildren": has_children,
        "navigatorInstance": {"instances": [{"instanceId": instance_id, "text": instance_text}]},
        "navigatorItems": [
            {
                "detailOne": title,
                "instanceTwo": {"instances": [{"text": location}]},
                "owner": {"instances": [{"instanceId": owner_id, "text": owner_name}]},
            }
        ],
    }


class NavigatorParsing(unittest.TestCase):
    PAYLOAD: ClassVar[dict] = {
        "body": {
            "navigatorContainers": [
                {
                    "navigatorContainerNodeType": "SELF",
                    "navigatorDetails": [
                        _detail(
                            "2500$11",
                            "Engineering : Platform II (Grace Hopper)",
                            "247$102",
                            "Grace Hopper",
                            "Senior Engineering Manager",
                            "London",
                            True,
                        )
                    ],
                },
                {
                    "navigatorContainerNodeType": "CHILD_NODES_AND_LEAVES",
                    "count": 2,
                    "navigatorDetails": [
                        _detail("247$101", "Alan Turing", "247$101", "Alan Turing", "Senior Tech Lead", "London", False),
                        _detail("2500$12", "Sub Team (Katherine Johnson)", "247$103", "Katherine Johnson", "Engineering Manager", "Cambridge", True),
                    ],
                },
            ]
        }
    }

    def test_self_child_and_kinds(self):
        response = bd.parse_navigator_response(self.PAYLOAD)
        self.assertEqual(response.self_node.owner_name, "Grace Hopper")
        self.assertEqual(response.self_node.instance_id, "2500$11")
        self.assertEqual(response.child_count, 2)
        self.assertEqual(len(response.children), 2)

        leaf, suborg = response.children
        self.assertEqual(leaf.owner_name, "Alan Turing")
        self.assertEqual(leaf.title, "Senior Tech Lead")
        self.assertEqual(leaf.location, "London")
        self.assertFalse(leaf.has_children)
        self.assertFalse(leaf.is_organization)  # a person, not a sub-org

        self.assertTrue(suborg.is_organization)
        self.assertTrue(suborg.has_children)
        self.assertEqual(suborg.instance_id, "2500$12")


class SessionExtraction(unittest.TestCase):
    def test_from_curl(self):
        command = "curl 'https://wd999.myworkday.com/acme/inst/a/b.htmld' -H 'User-Agent: TestUA' -H 'Cookie: JSESSIONID=abc; foo=bar' -H 'Session-Secure-Token: tok-123' -H 'X-Workday-Client: 2026.29.36'"
        session = bd.session_from_curl(command)
        self.assertEqual(session.base_url, "https://wd999.myworkday.com")
        self.assertEqual(session.tenant, "acme")
        self.assertIn("JSESSIONID=abc", session.cookie)
        self.assertEqual(session.session_secure_token, "tok-123")
        self.assertEqual(session.workday_client, "2026.29.36")
        self.assertEqual(session.user_agent, "TestUA")

    def test_from_har(self):
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "url": "https://wd999.myworkday.com/acme/boot-config",
                            "headers": [
                                {"name": "Cookie", "value": "JSESSIONID=xyz"},
                                {"name": "Session-Secure-Token", "value": "tok-xyz"},
                                {"name": "User-Agent", "value": "HarUA"},
                            ],
                        }
                    }
                ]
            }
        }
        with tempfile.NamedTemporaryFile("w", suffix=".har", delete=False) as handle:
            json.dump(har, handle)
            path = Path(handle.name)
        try:
            session = bd.session_from_har(path)
        finally:
            path.unlink()
        self.assertEqual(session.tenant, "acme")
        self.assertEqual(session.session_secure_token, "tok-xyz")
        self.assertEqual(session.user_agent, "HarUA")


class OrganizationChartCache(unittest.TestCase):
    def test_round_trip_and_search(self):
        people = [
            bd.Person("Ada Lovelace", "247$1", title="Engineer", organization="Eng (Boss)", location="London"),
            bd.Person("Bob Stone", "247$2", title="Designer", organization="Design (Boss)", location="Berlin"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orgchart.json"
            bd.save_organization_chart(people, path)
            loaded = bd.load_organization_chart(path)
            self.assertEqual(loaded, people)
            self.assertEqual([p.name for p in bd.search_organization_chart(loaded, "engineer")], ["Ada Lovelace"])
            self.assertEqual([p.name for p in bd.search_organization_chart(loaded, "berlin")], ["Bob Stone"])
            self.assertEqual(bd.search_organization_chart(loaded, "nobody"), [])


class SessionEndDetection(unittest.TestCase):
    # The real transient fault that aborted the first full crawl — must NOT read as expiry.
    OTEL_SERVER_ERROR = '<wml:Application_Error Type="Server Error"><wml:Message>class java.lang.String cannot be cast to class io.opentelemetry.api.common.AttributeKey</wml:Message></wml:Application_Error>'

    def test_transient_server_error_is_not_session_end(self):
        self.assertFalse(bd.looks_like_session_end(self.OTEL_SERVER_ERROR))

    def test_login_or_expiry_message_is_session_end(self):
        self.assertTrue(bd.looks_like_session_end("Your session has expired, please log in again."))
        self.assertTrue(bd.looks_like_session_end("You must sign in to continue."))


class LoginPageDetection(unittest.TestCase):
    # What an expired session really gets back: HTTP 200, text/html, six blank lines then a JS redirect.
    LOGIN_PAGE = "\n\n\n\n\n\n<html>\n<body>\n<script type=\"text/javascript\">\n\tvar redirectUrl = 'https://wd999.myworkday.com/wday/authgwy/acme/login.htmld?returnTo=%2facme%2fboot-config';\n\twindow.location = redirectUrl;\n</script>\n</body>\n</html>"

    def test_login_html_is_recognised(self):
        self.assertTrue(bd.looks_like_login_page(self.LOGIN_PAGE))

    def test_json_body_is_never_a_login_page(self):
        # A real search hit whose title trips the session-end regex must still be data.
        self.assertFalse(bd.looks_like_login_page('{"results":[{"subtitle1":"Authentication Platform Engineer"}]}'))
        self.assertFalse(bd.looks_like_login_page('[{"text":"please sign in"}]'))


class JsonFileErrors(unittest.TestCase):
    def test_decode_error_names_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orgchart.json"
            path.write_text("{not json")
            with self.assertRaises(json.JSONDecodeError) as caught:
                bd.read_json_file(path)
        self.assertIn(str(path), "\n".join(caught.exception.__notes__))


class EmailFromProfile(unittest.TestCase):
    def test_extracts_primary_email(self):
        payload = {"body": {"compositeViewHeader": {"contactInfo": {"primaryEmail": "alan@example.com"}}}}
        self.assertEqual(bd.email_from_profile(payload), "alan@example.com")

    def test_missing_returns_none(self):
        self.assertIsNone(bd.email_from_profile({}))
        self.assertIsNone(bd.email_from_profile({"body": {"compositeViewHeader": {"contactInfo": {}}}}))


class FormatPerson(unittest.TestCase):
    def test_shows_person_email_and_manager_email(self):
        turing = bd.Person(
            "Alan Turing",
            "247$101",
            title="Senior Tech Lead",
            organization="Engineering : Platform II (Grace Hopper)",
            location="London",
            manager="Grace Hopper",
            manager_id="247$102",
            email="alan@example.com",
        )
        hopper = bd.Person("Grace Hopper", "247$102", email="grace@example.com")
        line = bd._format_person(turing, {p.worker_id: p for p in (turing, hopper)})
        self.assertIn("Alan Turing <alan@example.com>", line)
        self.assertIn("Grace Hopper <grace@example.com>", line)

    def test_falls_back_to_names_without_emails(self):
        line = bd._format_person(bd.Person("A", "247$1", manager="B", manager_id="247$2"))
        self.assertIn("A", line)
        self.assertIn("B", line)
        self.assertNotIn("<", line)


class DiacriticSearch(unittest.TestCase):
    PEOPLE: ClassVar[list] = [
        bd.Person(
            "Nguyễn Văn Bảo",
            "247$401",
            title="Technical Lead",
            location="Ha Noi",
            organization="Engineering : Infra & Security III.I (Katherine Johnson)",
            email="baonv1@example.com",
        ),
        bd.Person("Chee Bao Lim", "247$402", title="Senior Software Engineer", email="lim@example.com"),
    ]

    def test_fold_strips_vietnamese_diacritics(self):
        self.assertEqual(bd._fold("Nguyễn Văn Bảo"), "nguyen van bao")

    def test_ascii_query_matches_diacritic_given_name(self):
        names = [p.name for p in bd.search_organization_chart(self.PEOPLE, "bao")]
        self.assertIn("Nguyễn Văn Bảo", names)
        self.assertIn("Chee Bao Lim", names)

    def test_ascii_query_matches_diacritic_family_name(self):
        self.assertEqual([p.name for p in bd.search_organization_chart(self.PEOPLE, "nguyen")], ["Nguyễn Văn Bảo"])

    def test_matches_by_email_username(self):
        self.assertEqual([p.name for p in bd.search_organization_chart(self.PEOPLE, "baonv1")], ["Nguyễn Văn Bảo"])


class TenantRelativePath(unittest.TestCase):
    """`chunkingUrl` is server-supplied; httpx would attach our session cookie to it."""

    def test_relative_path_passes_through(self):
        self.assertEqual(bd.tenant_relative_path("/acme/chunk/123"), "/acme/chunk/123")

    def test_absolute_url_is_refused(self):
        for url in ("https://evil.example.com/chunk", "http://evil.example.com/chunk", "evil.example.com/chunk"):
            with self.subTest(url=url), self.assertRaises(bd.WorkdayError):
                bd.tenant_relative_path(url)


class SessionLoadErrors(unittest.TestCase):
    def test_missing_file_asks_for_reauth(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(bd.SessionExpired):
                bd.Session.load(Path(directory) / "absent.json")

    def test_unknown_field_asks_for_reauth_not_typeerror(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text(json.dumps({"base_url": "https://wd999.myworkday.com", "tenant": "acme", "cookie": "c", "surprise": 1}))
            with self.assertRaises(bd.SessionExpired):
                bd.Session.load(path)


class CacheSchemaDrift(unittest.TestCase):
    def test_unknown_person_field_degrades_to_empty_index(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orgchart.json"
            path.write_text(json.dumps({"people": [{"name": "A", "worker_id": "247$1", "phone": "123"}]}))
            with mock.patch.object(bd, "ORGANIZATION_CHART_FILE", path):
                self.assertEqual(bd._load_cache_index(), {})

    def test_missing_people_key_degrades_to_empty_index(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orgchart.json"
            path.write_text(json.dumps({"count": 0}))
            with mock.patch.object(bd, "ORGANIZATION_CHART_FILE", path):
                self.assertEqual(bd._load_cache_index(), {})


if __name__ == "__main__":
    unittest.main()
