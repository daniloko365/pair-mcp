"""Source wiring guards, not a substitute for physical status-item acceptance."""
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "native" / "PairApp.swift"


def test_status_target_lives_through_optimized_event_loop():
    source = SOURCE.read_text()
    assert "item.button?.target = self" in source
    assert "item.button?.action = #selector(togglePanel)" in source
    assert "withExtendedLifetime(delegate) { application.run() }" in source


def test_reveal_restores_minimized_window_on_active_space():
    source = SOURCE.read_text()
    assert "window.collectionBehavior.insert(.moveToActiveSpace)" in source
    toggle = source.split("@objc func togglePanel()", 1)[1].split("private func installMainMenu()", 1)[0]
    assert "window.isVisible && window.isKeyWindow && !window.isMiniaturized" in toggle
    assert "NSApp.isActive" not in toggle
    assert "window.deminiaturize(nil)" in toggle
    assert "window.makeKeyAndOrderFront(nil)" in toggle
