from __future__ import annotations

import json

from decidian_docling.config import GeminiSettings
from decidian_docling.ui import _live_progress_callback, _page_metric


def test_docx_without_page_provenance_is_not_rendered_as_zero_pages() -> None:
    value, help_text = _page_metric(
        {
            "source": {"extension": ".docx"},
            "counts": {"pages": 0},
            "provenance_scope": "unavailable",
        }
    )

    assert value == "Unavailable"
    assert help_text == "DOCX page provenance is unavailable in this conversion."


def test_docx_with_section_provenance_is_not_rendered_as_zero_pages() -> None:
    value, help_text = _page_metric(
        {
            "source": {"extension": ".docx"},
            "counts": {"pages": 0},
            "provenance_scope": "section_only",
        }
    )

    assert value == "Unavailable"
    assert help_text == "DOCX page provenance is unavailable in this conversion."


def test_page_metric_preserves_real_page_counts() -> None:
    assert _page_metric(
        {
            "source": {"extension": ".pdf"},
            "counts": {"pages": 48},
            "provenance_scope": "page",
        }
    ) == (48, None)


def test_live_dashboard_state_survives_rerun_and_can_replay_journal(
    monkeypatch,
    tmp_path,
) -> None:
    import decidian_docling.ui as ui

    rendered = []
    monkeypatch.setattr(ui.st, "session_state", {})
    monkeypatch.setattr(
        ui,
        "_render_live_ai_dashboard",
        lambda placeholder, state, events: rendered.append((dict(state), list(events))),
    )
    settings = GeminiSettings(True, "secret", "gemini-3.5-flash", 30, 0, 1)
    callback = _live_progress_callback(object(), settings, reset=True)
    callback({"event": "response_received", "total_tokens": 900, "estimated_cost_inr": 2.5})

    _live_progress_callback(object(), settings)

    assert rendered[-1][0]["event"] == "response_received"
    assert rendered[-1][0]["total_tokens"] == 900
    assert len(rendered[-1][1]) == 1

    journal = tmp_path / "run" / "gemini_events.jsonl"
    journal.parent.mkdir()
    journal.write_text(
        json.dumps({"event": "review_completed", "review_status": "failed", "requests_used": 5}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ui.st, "session_state", {})

    _live_progress_callback(object(), settings, replay_path=journal)

    assert rendered[-1][0]["event"] == "review_completed"
    assert rendered[-1][0]["requests_used"] == 5
