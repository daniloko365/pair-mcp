"""Source guard for the titled-GroupBox crash reproduced through Computer Use.

This prevents reintroducing the known trigger; it is not a physical UI oracle.
The matching real-window A/B observations live in the compatibility report.
"""
from pathlib import Path
import re


SOURCE = Path(__file__).resolve().parents[1] / "native" / "PairApp.swift"


def test_sections_avoid_named_groupbox_observer_crash():
    source = SOURCE.read_text()
    assert not re.search(r"\bGroupBox\s*\(", source), "Use the tested unlabeled GroupBox wrapper"
    for title in ("Разрешённые проекты", "Последние задачи", "Полный результат", "Подключить к обычному чату"):
        assert f'PanelSection("{title}")' in source


def test_section_titles_remain_visible_accessible_text():
    source = SOURCE.read_text()
    assert "struct PanelSection<Content: View>: View" in source
    wrapper = source.split("struct PanelSection<Content: View>: View", 1)[1].split("struct RouteEditor", 1)[0]
    assert "GroupBox {" in wrapper and "Text(title).font(.headline)" in wrapper
    assert "accessibilityHidden" not in wrapper
