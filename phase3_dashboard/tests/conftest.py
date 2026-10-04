import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def make_pdf(pages: list[str]) -> bytes:
    """Build a small text PDF in memory (one string per page)."""
    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        page.insert_text((72, 72), text, fontsize=11)
    data = document.tobytes()
    document.close()
    return data


@pytest.fixture
def sim_backend():
    from phase3_dashboard.backends.simulated import SimulatedBackend

    return SimulatedBackend(delay=0)


@pytest.fixture
def sample_session(sim_backend):
    """A paused review of the first sample policy."""
    from phase3_dashboard.core import analysis

    document = sim_backend.load_policy(sim_backend.list_policies()[0])
    return analysis.analyze_policy(sim_backend, document, [c["chunk_id"] for c in document.chunks])
