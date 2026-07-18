import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import research_desk.arxiv as arxiv
import research_desk.storage as storage
import server
from research_desk.reports import write_arxiv_report


SAMPLE_ARXIV_RESPONSE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <opensearch:totalResults>3</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2501.12345v1</id>
    <published>2025-01-20T10:00:00Z</published>
    <updated>2025-02-01T10:00:00Z</updated>
    <title>  A Recent Paper\nAbout Efficient AI  </title>
    <summary>A useful abstract.</summary>
    <author><name>Researcher One</name></author>
    <author><name>Researcher Two</name></author>
    <category term="cs.AI" />
    <arxiv:primary_category term="cs.AI" />
  </entry>
  <entry>
    <id>https://arxiv.org/abs/2412.99999</id>
    <published>2024-12-31T23:59:00Z</published>
    <updated>2025-01-02T10:00:00Z</updated>
    <title>An old paper updated in 2025</title>
    <summary>This must be excluded by its original publication date.</summary>
    <author><name>Researcher Three</name></author>
  </entry>
  <entry>
    <id>https://example.com/abs/2502.11111</id>
    <published>2025-02-05T10:00:00Z</published>
    <updated>2025-02-05T10:00:00Z</updated>
    <title>A non-arXiv link</title>
    <summary>This must be excluded because the host is not arxiv.org.</summary>
    <author><name>Researcher Four</name></author>
  </entry>
</feed>
"""


SAMPLE_IDEA = {
    "id": 1,
    "title": "Energy-Efficient AI Systems",
    "problem": "Reduce GPU energy use.",
    "tags": ["AI", "energy efficiency"],
    "priority": 5,
    "status": "exploring",
}


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        return False

    def read(self) -> bytes:
        return SAMPLE_ARXIV_RESPONSE


class ArxivSearchTests(unittest.TestCase):
    def tearDown(self) -> None:
        arxiv.clear_arxiv_cache()

    def test_query_contains_topic_and_fixed_2025_start_date(self) -> None:
        query = arxiv.build_arxiv_search_query(
            SAMPLE_IDEA,
            now=datetime(2026, 7, 18, 12, 30, tzinfo=timezone.utc),
        )

        self.assertIn('ti:"Energy-Efficient AI Systems"', query)
        self.assertIn('all:"energy efficiency"', query)
        self.assertIn("submittedDate:[202501010000 TO 202607181230]", query)

    def test_parser_keeps_only_recent_arxiv_links(self) -> None:
        total_results, papers = arxiv.parse_arxiv_response(SAMPLE_ARXIV_RESPONSE)

        self.assertEqual(total_results, 3)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]["published"], "2025-01-20")
        self.assertEqual(papers[0]["primary_category"], "cs.AI")
        self.assertEqual(
            papers[0]["arxiv_url"],
            "https://arxiv.org/abs/2501.12345v1",
        )
        self.assertEqual(
            papers[0]["pdf_url"],
            "https://arxiv.org/pdf/2501.12345v1",
        )

    def test_repeated_search_uses_daily_memory_cache(self) -> None:
        arxiv.clear_arxiv_cache()

        with patch.object(arxiv, "urlopen", return_value=FakeResponse()) as open_mock:
            first_total, first_papers = arxiv.request_arxiv_papers(SAMPLE_IDEA, 5)
            first_papers[0]["title"] = "Changed by caller"
            second_total, second_papers = arxiv.request_arxiv_papers(SAMPLE_IDEA, 5)

        self.assertEqual(first_total, second_total)
        self.assertEqual(open_mock.call_count, 1)
        self.assertEqual(second_papers[0]["title"], "A Recent Paper About Efficient AI")

    def test_unknown_idea_id_is_rejected_before_network_request(self) -> None:
        with (
            patch.object(server, "get_idea_by_id", return_value=None),
            patch.object(server, "request_arxiv_papers") as request_mock,
        ):
            result = server.find_arxiv_papers(999_999, 5)

        self.assertFalse(result["success"])
        self.assertEqual(result["papers"], [])
        self.assertFalse(result["report_created"])
        request_mock.assert_not_called()

    def test_result_limit_is_bounded(self) -> None:
        with patch.object(server, "get_idea_by_id") as idea_mock:
            result = server.find_arxiv_papers(1, 26)

        self.assertFalse(result["success"])
        self.assertEqual(result["papers"], [])
        idea_mock.assert_not_called()

    def test_paper_search_automatically_writes_markdown_report(self) -> None:
        _, papers = arxiv.parse_arxiv_response(SAMPLE_ARXIV_RESPONSE)

        with TemporaryDirectory() as temporary_directory:
            with (
                patch.object(server, "DOWNLOADS_DIR", Path(temporary_directory)),
                patch.object(server, "get_idea_by_id", return_value=SAMPLE_IDEA),
                patch.object(
                    server,
                    "request_arxiv_papers",
                    return_value=(1, papers),
                ),
            ):
                result = server.find_arxiv_papers(1, 1)

            report_path = Path(result["report_path"])
            report_content = report_path.read_text(encoding="utf-8")

        self.assertTrue(result["success"])
        self.assertTrue(result["report_created"])
        self.assertIsNone(result["report_error"])
        self.assertEqual(report_path.parent, Path(temporary_directory))
        self.assertIn("# Energy-Efficient AI Systems", report_content)
        self.assertIn("https://arxiv.org/abs/2501.12345v1", report_content)
        self.assertIn("published in 2025 or later", report_content)


class StorageAndToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.data_directory = Path(self.temporary_directory.name) / "data"
        self.data_file = self.data_directory / "research_ideas.json"
        self.directory_patch = patch.object(
            storage,
            "DATA_DIR",
            self.data_directory,
        )
        self.file_patch = patch.object(storage, "DATA_FILE", self.data_file)
        self.directory_patch.start()
        self.file_patch.start()

    def tearDown(self) -> None:
        self.file_patch.stop()
        self.directory_patch.stop()
        self.temporary_directory.cleanup()

    def _add_two_ideas(self) -> None:
        first = server.add_research_idea(
            "Energy-Efficient AI Systems",
            "Reduce inference energy.",
            "AI, cloud computing, energy efficiency",
            5,
        )
        second = server.add_research_idea(
            "Privacy-Preserving Medical Imaging",
            "Train imaging models without exposing patient data.",
            "medical imaging, privacy, federated learning",
            4,
        )
        self.assertTrue(first["success"])
        self.assertTrue(second["success"])

    def test_add_and_search_research_ideas(self) -> None:
        self._add_two_ideas()

        result = server.search_research_ideas("federated")

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["ideas"][0]["id"], 2)

    def test_update_status_and_dashboard(self) -> None:
        self._add_two_ideas()

        update_result = server.update_idea_status(2, "experimenting")
        dashboard = server.get_research_dashboard()

        self.assertTrue(update_result["success"])
        self.assertEqual(update_result["new_status"], "experimenting")
        self.assertEqual(dashboard["total_ideas"], 2)
        self.assertEqual(dashboard["ideas_by_status"]["experimenting"], 1)
        self.assertEqual(len(dashboard["high_priority_ideas"]), 2)

    def test_storage_writes_atomically_and_validates_schema(self) -> None:
        storage.save_ideas([SAMPLE_IDEA])

        self.assertEqual(storage.load_ideas(), [SAMPLE_IDEA])
        self.assertEqual(list(self.data_directory.glob("*.tmp")), [])

        invalid_idea = {**SAMPLE_IDEA, "priority": 8}
        with self.assertRaisesRegex(ValueError, "invalid priority"):
            storage.save_ideas([invalid_idea])

    def test_invalid_json_is_reported_clearly(self) -> None:
        self.data_directory.mkdir(parents=True)
        self.data_file.write_text("not-json", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "contains invalid JSON"):
            storage.load_ideas()


class ReportTests(unittest.TestCase):
    def test_report_filenames_are_unique_and_non_destructive(self) -> None:
        _, papers = arxiv.parse_arxiv_response(SAMPLE_ARXIV_RESPONSE)
        generated_at = datetime(2026, 7, 18, 13, 0, tzinfo=timezone.utc)

        with TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory)
            first_path = write_arxiv_report(
                SAMPLE_IDEA,
                papers,
                output_dir=destination,
                generated_at=generated_at,
            )
            second_path = write_arxiv_report(
                SAMPLE_IDEA,
                papers,
                output_dir=destination,
                generated_at=generated_at,
            )

            first_data = first_path.read_text(encoding="utf-8")

        self.assertNotEqual(first_path, second_path)
        self.assertTrue(second_path.stem.endswith("-2"))
        self.assertIn("[Download PDF](https://arxiv.org/pdf/2501.12345v1)", first_data)

    def test_resource_returns_valid_json(self) -> None:
        with patch.object(server, "load_ideas", return_value=[SAMPLE_IDEA]):
            result = server.get_all_research_ideas()

        self.assertEqual(json.loads(result), [SAMPLE_IDEA])


if __name__ == "__main__":
    unittest.main()
