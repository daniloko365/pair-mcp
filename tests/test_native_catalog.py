"""Execute public, pure Swift catalog rules; these are not physical UI checks."""
from pathlib import Path
import plistlib
import shutil
import subprocess

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "native" / "PairApp.swift"
INFO = SOURCE.with_name("Info.plist")


@pytest.fixture(scope="module")
def rules(tmp_path_factory):
    compiler = shutil.which("swiftc")
    if not compiler:
        pytest.skip("Pure native rules require the installed Swift compiler")
    source = SOURCE.read_text()
    assert "struct CatalogLoadTracker" in source, "Catalog requests need an executable deduplication rule"
    assert "struct CatalogSearch" in source, "Shared source and model search needs executable rules"
    provider = source.split("struct Provider:", 1)[1].split("struct Member:", 1)[0]
    helpers = source.split("struct ModelEntry:", 1)[1].split("struct SourceOption:", 1)[0]
    directory = tmp_path_factory.mktemp("native-catalog-public")
    script = directory / "rules.swift"
    script.write_text("import Foundation\nstruct Provider:" + provider + "struct ModelEntry:" + helpers + r'''
func require(_ condition: Bool, _ message: String) {
    if !condition { fatalError(message) }
}
let base = Provider(id: "public", name: "Public", protocol: "openai", baseUrl: "https://public.example/v1")
let first = CatalogSignature(provider: base, keyPresent: true, keyGeneration: 0)
var renamed = base
renamed.name = "Renamed"
var changed = base
changed.baseUrl = "https://changed.example/v1"
let second = CatalogSignature(provider: changed, keyPresent: true, keyGeneration: 0)
switch CommandLine.arguments[1] {
case "signature":
    require(first == CatalogSignature(provider: renamed, keyPresent: true, keyGeneration: 0), "Display name must not invalidate")
    require(first != second, "Endpoint must invalidate")
    changed = base; changed.manualModels = ["manual"]
    require(first != CatalogSignature(provider: changed, keyPresent: true, keyGeneration: 0), "Saved manual list must invalidate")
    require(first != CatalogSignature(provider: base, keyPresent: false, keyGeneration: 0), "Delayed key presence must trigger")
    require(first != CatalogSignature(provider: base, keyPresent: true, keyGeneration: 1), "Successful key replacement must trigger")
    require(CatalogRequestIdentity(providerID: "a", ready: true, signature: first) != CatalogRequestIdentity(providerID: "b", ready: true, signature: first), "Switching identical provider settings must still load that provider")
case "dedup":
    var tracker = CatalogLoadTracker()
    require(tracker.begin("public", signature: first, force: false), "First request expected")
    require(!tracker.begin("public", signature: first, force: true), "Inflight request must deduplicate even manual refresh")
    require(!tracker.begin("public", signature: second, force: false), "Changed signature must wait for active request")
    require(!tracker.finish("public", signature: first, current: second), "Stale response must be rejected")
    require(tracker.begin("public", signature: second, force: false), "Latest signature must get one request")
    require(tracker.finish("public", signature: second, current: second), "Matching response may apply")
case "no_retry":
    for _ in ["error", "empty"] {
        var tracker = CatalogLoadTracker()
        require(tracker.begin("public", signature: first, force: false), "First request expected")
        _ = tracker.finish("public", signature: first, current: first)
        for _ in 0..<20 { require(!tracker.begin("public", signature: first, force: false), "Polls must not retry error or empty response") }
        require(tracker.begin("public", signature: first, force: true), "Explicit refresh retries")
    }
case "compatibility":
    let ordinary = ModelEntry.parse(["id": "text", "capabilities": ["textChatCompatible": true, "interactiveCompatible": true]])!
    let unknown = ModelEntry.parse(["id": "unknown"])!
    let batch = ModelEntry.parse(["id": "text:batch", "capabilities": ["textChatCompatible": true]])!
    let media = ModelEntry.parse(["id": "media", "capabilities": ["outputModalities": ["image"]]])!
    require(ordinary.visibleInOrdinaryChat && unknown.visibleInOrdinaryChat, "Unknown catalog entries must remain available")
    require(!batch.visibleInOrdinaryChat && !media.visibleInOrdinaryChat, "Explicit batch and media must be hidden by default")
    require(unknown.textChatCompatible == nil && unknown.outputModalities == nil, "Absent fields must remain unknown")
    require(batch.compatibilityWarning == "Асинхронная batch-модель, не для обычного чата", "Batch warning must be honest")
    let filtered = ModelEntry.pickerEntries([ordinary, unknown, batch, media], selected: "media", manualIDs: ["manual"], showAll: false)
    require(filtered.map(\.id).contains("media") && filtered.map(\.id).contains("manual"), "Selected and manual IDs must survive filtering")
    require(!filtered.map(\.id).contains("text:batch"), "Unselected batch remains hidden")
    require(ModelEntry.pickerEntries([ordinary, batch, media], selected: "", manualIDs: [], showAll: true).count == 3, "Full catalog escape must retain all rows")
case "reasoning_enum":
    let model = ModelEntry.parse(["id": "x", "reasoning": ["low", "high"], "capabilities": ["reasoningSupported": true, "reasoningEnumKnown": true, "reasoningKnown": true]])!
    let known = model.reasoningChoices(saved: nil, allowUnknown: false)
    require(known.values == ["low", "high"] && known.warning == nil, "Advertised enum takes precedence")
    let preserved = model.reasoningChoices(saved: "max", allowUnknown: false)
    require(preserved.values == ["low", "high", "max"] && preserved.warning != nil, "Saved invalid effort must remain visible")
case "reasoning_supported":
    let model = ModelEntry.parse(["id": "x", "capabilities": ["reasoningSupported": true, "reasoningEnumKnown": false, "reasoningKnown": true]])!
    let choices = model.reasoningChoices(saved: nil, allowUnknown: false)
    require(choices.values == ["none", "minimal", "low", "medium", "high", "xhigh", "max"], "Unknown enum offers only accepted generic choices")
    require(choices.warning == "Уровни не объявлены; значение проверит поставщик", "Generic values must not pretend to be an advertised enum")
case "reasoning_empty_enum":
    let model = ModelEntry.parse(["id": "x", "reasoning": [], "capabilities": ["reasoningSupported": true, "reasoningEnumKnown": true, "reasoningKnown": true]])!
    let fresh = model.reasoningChoices(saved: nil, allowUnknown: true)
    require(fresh.values.isEmpty && fresh.disabled, "Known empty enum must not invent generic levels")
    require(fresh.warning != nil && fresh.warning != "Уровни не объявлены; значение проверит поставщик", "Known empty enum needs an honest explanation")
    let preserved = model.reasoningChoices(saved: "high", allowUnknown: false)
    require(preserved.values == ["high"] && !preserved.disabled && preserved.warning != nil, "Known-invalid saved level remains visible and explicitly clearable")
    let cli = ModelEntry.parse(["id": "cli", "reasoning": ["low", "high"]])!
    require(cli.reasoningChoices(saved: nil, allowUnknown: false).values == ["low", "high"], "CLI enum without enum-known flag must still work")
case "reasoning_unsupported":
    let model = ModelEntry.parse(["id": "x", "capabilities": ["reasoningSupported": false, "reasoningKnown": true]])!
    require(model.reasoningChoices(saved: nil, allowUnknown: true).disabled, "Known unsupported fresh route stays default")
    let preserved = model.reasoningChoices(saved: "high", allowUnknown: false)
    require(preserved.values == ["high"] && !preserved.disabled && preserved.warning != nil, "Unsupported saved value may only be cleared explicitly")
case "reasoning_unknown":
    let model = ModelEntry.parse(["id": "manual"])!
    require(model.reasoningChoices(saved: nil, allowUnknown: false).values.isEmpty, "Unknown reasoning requires explicit advanced choice")
    let explicit = model.reasoningChoices(saved: nil, allowUnknown: true)
    require(explicit.values.count == 7 && explicit.warning != nil, "Explicit unknown support remains honest")
    require(model.reasoningChoices(saved: "high", allowUnknown: false).values.contains("high"), "Existing value never disappears")
case "safe_errors":
    require(CatalogFailure.message("catalog_unavailable") == "подключение к каталогу недоступно", "Connection error must not invent an auth cause")
    require(CatalogFailure.message("RoutingError") == "подключение к каталогу недоступно", "Older backend generic failure remains usable")
    require(!CatalogFailure.message("arbitrary private upstream body").contains("private"), "Unknown details never enter the UI")
    require(CatalogFailure.message("catalog_http_429").contains("частоту"), "Known rate failure has useful wording")
    for code in ["catalog_connection_error", "catalog_timeout"] {
        let message = CatalogFailure.message(code)
        require(message.contains("сеть") && message.contains("VPN") && message.contains("адрес"), "Transport failure needs network, VPN and endpoint guidance")
        require(!message.contains("ручн"), "Transport failure cannot be solved by changing model ID")
    }
case "catalog_search":
    let sources = [(id: "codex", name: "Подписка Codex"), (id: "or-public", name: "OpenRouter"), (id: "cl-public", name: "Clodex")]
    require(CatalogSearch.sourceSuggestion(sources, selected: "codex", query: "router") == "OpenRouter", "Recognize a different source typed in model search")
    require(CatalogSearch.sourceSuggestion(sources, selected: "codex", query: "OPEN") == "OpenRouter", "Source suggestion is case insensitive")
    require(CatalogSearch.sourceSuggestion(sources, selected: "or-public", query: "OpenRouter") == nil, "Do not suggest switching to the selected source")
    require(CatalogSearch.sourceSuggestion(sources, selected: "codex", query: "missing") == nil, "Unmatched text is just a model query")
    require(CatalogSearch.matches("SOL", id: "openai/gpt-6-sol", name: "Sol"), "Model ID and name search remains available")
case "explicit_default":
    require(CatalogSearch.defaultModel(source: "or-public", configuredNative: nil).isEmpty, "API route never chooses a paid catalog alias automatically")
    require(CatalogSearch.defaultModel(source: "codex", configuredNative: nil).isEmpty, "Unconfigured CLI route requires an explicit model")
    require(CatalogSearch.defaultModel(source: "codex", configuredNative: "gpt-6-sol") == "gpt-6-sol", "Explicit configured native model remains usable")
    require(CatalogSearch.defaultModel(source: "or-public", configuredNative: "paid-alias").isEmpty, "Only native settings may supply a default model")
default: fatalError("Unknown public test case")
}
''')
    executable = directory / "rules"
    result = subprocess.run([compiler, "-swift-version", "5", str(script), "-o", str(executable)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return executable


@pytest.mark.parametrize("case", ["signature", "dedup", "no_retry", "compatibility", "reasoning_enum", "reasoning_supported", "reasoning_empty_enum", "reasoning_unsupported", "reasoning_unknown", "safe_errors", "catalog_search", "explicit_default"])
def test_pure_native_catalog_rules(rules, case):
    result = subprocess.run([str(rules), case], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_relevant_views_load_without_changing_selected_model():
    source = SOURCE.read_text()
    route = source.split("struct RouteEditor:", 1)[1].split("struct MemberEditor:", 1)[0]
    assert ".task(id: app.catalogRequestIdentity(route.providerId))" in route
    assert "app.ensureCatalogLoaded(route.providerId)" in route
    assert 'Toggle("Весь каталог (включая batch и медиа)"' in route
    assert 'Text("По умолчанию поставщика").tag("")' in route
    assert 'route.effort = $0.isEmpty ? nil : $0' in route
    provider = source.split("struct ProviderEditor:", 1)[1].split("struct ConnectionsPanel:", 1)[0]
    assert ".task(id: app.catalogRequestIdentity(provider.id))" in provider
    loading = source.split("func ensureCatalogLoaded", 1)[1].split("func models(for", 1)[0]
    assert "draft =" not in loading and "route.model =" not in loading and "pendingKeys" not in loading
    status = source.split("func catalogStatus", 1)[1].split("func syncModels", 1)[0]
    assert "Введите ID вручную или обновите каталог" not in status
    default_route = source.split("func defaultRoute(for", 1)[1].split("func start()", 1)[0]
    assert "CatalogSearch.defaultModel" in default_route
    assert "advertised.first" not in default_route
    assert 'TextField("Поиск модели", text: $search)' in route
    assert '.onChange(of: search)' not in route and '.onSubmit' not in route


def test_search_is_model_only_after_explicit_source_selection():
    route = SOURCE.read_text().split("struct RouteEditor:", 1)[1].split("struct MemberEditor:", 1)[0]
    assert 'TextField("Поиск модели", text: $search)' in route
    assert 'ForEach(allSources) { source in' in route
    assert 'CatalogSearch.sourceSuggestion' in route
    assert 'Text("Модели не найдены")' not in route
    assert 'entries.insert(available.first { $0.id == route.model }' in route
    assert '.onChange(of: search)' not in route and '.onSubmit' not in route


def test_council_keeps_compact_rows_with_only_one_editor_open():
    source = SOURCE.read_text()
    member = source.split("struct MemberEditor:", 1)[1].split("struct LimitsEditor:", 1)[0]
    council = source.split("struct CouncilPanel:", 1)[1].split("struct ProviderEditor:", 1)[0]
    assert "@State private var expandedMemberID: String?" in council
    assert "expanded: expandedMemberID == member.id" in council
    assert "expandedMemberID = expandedMemberID == member.id ? nil : member.id" in council
    assert "if expanded {" in member
    assert 'TextField("Роль (как отвечать)"' in member
    assert "входит в инструкцию модели" in member
    assert 'RouteEditor(route: $member.routes[0], label: "Основной маршрут")' in member
    assert 'DisclosureGroup("Резервы (' in member
    assert 'Button("Добавить участника", systemImage: "plus")' in council
    assert 'Button("Добавить резерв", systemImage: "plus")' in member
    assert 'if app.keyPresent[route.providerId] != true' in member
    assert 'if app.catalogErrors[route.providerId] != nil' in member
    assert 'if let warning = routeWarning' in member
    assert '.task(id: app.catalogRequestIdentity(primary?.providerId ?? ""))' in member


def test_connections_show_key_presence_and_catalog_warning_before_expanding_editor():
    source = SOURCE.read_text()
    provider = source.split("struct ProviderEditor:", 1)[1].split("struct ConnectionsPanel:", 1)[0]
    connections = source.split("struct ConnectionsPanel:", 1)[1].split("struct AgentCard:", 1)[0]
    summary, details = provider.split("if expanded {", 1)
    assert "@State private var expandedProviderID: String?" in connections
    assert "expanded: expandedProviderID == provider.id" in connections
    assert "expandedProviderID = expandedProviderID == provider.id ? nil : provider.id" in connections
    assert 'Text(provider.name.isEmpty ? "Без названия" : provider.name)' in summary
    assert 'app.keyPresent[provider.id] == true ? "Ключ сохранён" : "Ключ не сохранён"' in summary
    assert "if let warning = app.catalogErrors[provider.id]" in summary
    assert 'Button(expanded ? "Готово" : "Настроить"' in summary
    assert 'TextField("Название"' not in summary
    assert 'SecureField(' not in summary
    assert 'pendingKeys' not in summary
    for control in ['TextField("Название"', 'Toggle("Включено"', 'Picker("Протокол"',
                    'TextField("https://…/v1"', 'SecureField(', 'Button("Удалить ключ"',
                    'Button(app.syncing.contains(provider.id) ? "Обновляем…" : "Обновить модели")',
                    '"В Keychain"', 'help("Удалить подключение")',
                    'DisclosureGroup("Дополнительно"', 'TextEditor(text:',
                    'Toggle("Разрешить локальный API"']:
        assert control in details
    assert '.task(id: app.catalogRequestIdentity(provider.id))' in provider
    assert '.confirmationDialog("Удалить сохранённый ключ?"' in provider
    assert '.confirmationDialog("Убрать подключение?"' in provider
    assert 'Button("Добавить API", systemImage: "plus")' in connections
    assert 'Button("Подключить к Codex")' in connections
    assert 'Button("Подключить к Claude")' in connections


def test_regular_app_keeps_status_item_and_compact_header():
    source = SOURCE.read_text()
    info = plistlib.loads(INFO.read_bytes())
    assert info.get("LSUIElement") is False
    assert "NSApp.setActivationPolicy(.regular)" in source
    assert "NSStatusBar.system.statusItem" in source
    assert "func applicationShouldHandleReopen" in source
    panel = source.split("struct PanelView:", 1)[1].split("final class PairWindow:", 1)[0]
    assert 'Image(systemName: "person.2.fill")' not in panel
    assert 'Picker("Раздел"' not in panel
    assert ".labelsHidden()" in panel
