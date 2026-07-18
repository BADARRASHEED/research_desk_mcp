import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import server


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


class ArxivSearchTests(unittest.TestCase):
    def test_query_contains_topic_and_fixed_2025_start_date(self) -> None:
        idea = {
            "title": "Energy-Efficient AI Systems",
            "problem": "Reduce GPU energy use.",
            "tags": ["AI", "energy efficiency"],
        }
        query = server.build_arxiv_search_query(
            idea,
            now=datetime(2026, 7, 18, 12, 30, tzinfo=timezone.utc),
        )

        self.assertIn('ti:"Energy-Efficient AI Systems"', query)
        self.assertIn('all:"energy efficiency"', query)
        self.assertIn(
            "submittedDate:[202501010000 TO 202607181230]",
            query,
        )

    def test_parser_keeps_only_recent_arxiv_links(self) -> None:
        total_results, papers = server.parse_arxiv_response(
            SAMPLE_ARXIV_RESPONSE
        )

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

    def test_unknown_idea_id_is_rejected_before_network_request(self) -> None:
        result = server.find_arxiv_papers(999_999, 5)

        self.assertFalse(result["success"])
        self.assertEqual(result["papers"], [])

    def test_result_limit_is_bounded(self) -> None:
        result = server.find_arxiv_papers(1, 26)

        self.assertFalse(result["success"])
        self.assertEqual(result["papers"], [])

    def test_paper_search_automatically_writes_markdown_report(self) -> None:
        idea = {
            "id": 1,
            "title": "Energy-Efficient AI Systems",
            "problem": "Reduce GPU energy use.",
            "tags": ["AI", "energy efficiency"],
            "priority": 5,
            "status": "exploring",
        }
        _, papers = server.parse_arxiv_response(SAMPLE_ARXIV_RESPONSE)
        with TemporaryDirectory() as temporary_directory:
            with (
                patch.object(server, "DOWNLOADS_DIR", Path(temporary_directory)),
                patch.object(server, "get_idea_by_id", return_value=idea),
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


if __name__ == "__main__":
    unittest.main()
