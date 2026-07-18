"""Shared project configuration."""

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "research_ideas.json"
DOWNLOADS_DIR = Path.home() / "Downloads"

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_MIN_PAPER_YEAR = 2025
ARXIV_MAX_RESULTS = 25
ARXIV_REQUEST_TIMEOUT_SECONDS = 30
ARXIV_MIN_REQUEST_INTERVAL_SECONDS = 3.0
ARXIV_CACHE_TTL_SECONDS = 24 * 60 * 60

XML_NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}
