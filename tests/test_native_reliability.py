"""Executable merge rules and source wiring for the native recovery paths."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "native" / "PairApp.swift"


@pytest.fixture(scope="module")
def merge_rules(tmp_path_factory):
    compiler = shutil.which("swiftc")
    if not compiler:
        pytest.skip("Native merge rules require Swift")
    source = SOURCE.read_text()
    assert "struct ConfigMerge {" in source
    models = source.split("struct Route:", 1)[1].split("struct ModelEntry:", 1)[0]
    directory = tmp_path_factory.mktemp("native-merge")
    script = directory / "merge.swift"
    script.write_text("import Foundation\nstruct Route:" + models + r'''
func check(_ condition: Bool, _ message: String) {
    if !condition { fatalError(message) }
}
let original = Configuration(
    revision: 4,
    providers: [Provider(id: "api", name: "API", protocol: "openai", baseUrl: "https://example.test/v1")],
    members: [Member(id: "one", label: "Design", routes: [Route(providerId: "api", model: "m1")])]
)
switch CommandLine.arguments[1] {
case "independent":
    var mine = original
    mine.members[0].label = "Engineering"
    var theirs = original
    theirs.revision = 5
    theirs.providers[0].baseUrl = "https://other.test/v1"
    let result = ConfigMerge.reconcile(base: original, local: mine, remote: theirs)
    check(result.conflicts.isEmpty, "Separate edits should merge")
    check(result.config.revision == 5, "Server revision must win")
    check(result.config.members[0].label == "Engineering", "Keep local role")
    check(result.config.providers[0].baseUrl == "https://other.test/v1", "Keep remote endpoint")
case "same_member_fields":
    var mine = original
    mine.members[0].label = "Engineering"
    var theirs = original
    theirs.revision = 5
    theirs.members[0].routes[0].model = "m2"
    let result = ConfigMerge.reconcile(base: original, local: mine, remote: theirs)
    check(result.conflicts.isEmpty, "Different fields of same member should merge")
    check(result.config.members[0].label == "Engineering", "Keep local label")
    check(result.config.members[0].routes[0].model == "m2", "Keep remote model")
case "overlap":
    var mine = original
    mine.members[0].label = "Engineering"
    var theirs = original
    theirs.revision = 5
    theirs.members[0].label = "Research"
    let unresolved = ConfigMerge.reconcile(base: original, local: mine, remote: theirs)
    check(unresolved.conflicts.count == 1, "Overlapping edit needs one explicit choice")
    check(unresolved.conflicts[0].mine.contains("Engineering") && unresolved.conflicts[0].current.contains("Research"),
          "Both values must be visible before choosing")
    let id = unresolved.conflicts[0].id
    let current = ConfigMerge.reconcile(base: original, local: mine, remote: theirs, choices: [id: "current"])
    let own = ConfigMerge.reconcile(base: original, local: mine, remote: theirs, choices: [id: "mine"])
    check(current.config.members[0].label == "Research", "Current choice must retain external edit")
    check(own.config.members[0].label == "Engineering", "Own choice must apply only when explicit")
case "concurrent_adds":
    var mine = original
    mine.members.append(Member(id: "local", label: "Local", routes: []))
    var theirs = original
    theirs.revision = 5
    theirs.members.append(Member(id: "remote", label: "Remote", routes: []))
    let result = ConfigMerge.reconcile(base: original, local: mine, remote: theirs)
    check(result.conflicts.isEmpty, "Independent additions should merge")
    check(result.config.members.map(\.id) == ["one", "remote", "local"], "Keep server order and both additions")
case "remove_vs_edit":
    var mine = original
    mine.members.removeAll()
    var theirs = original
    theirs.revision = 5
    theirs.members[0].label = "Remote changed"
    let result = ConfigMerge.reconcile(base: original, local: mine, remote: theirs)
    check(result.conflicts.count == 1, "Deletion must not silently discard a concurrent edit")
case "stable_route_fields":
    var basis = original
    basis.members[0].routes.append(Route(providerId: "api", model: "m2"))
    var mine = basis
    mine.members[0].routes[0].effort = "high"
    var theirs = basis
    theirs.revision = 5
    theirs.members[0].routes[1].effort = "low"
    let result = ConfigMerge.reconcile(base: basis, local: mine, remote: theirs)
    check(result.conflicts.isEmpty, "Distinct effort changes on stable routes should merge")
    check(result.config.members[0].routes.map(\.effort) == ["high", "low"], "Preserve both route edits")
case "route_reorder":
    var basis = original
    basis.members[0].routes.append(Route(providerId: "api", model: "m2"))
    var mine = basis
    mine.members[0].routes[0].effort = "high"
    var theirs = basis
    theirs.revision = 5
    theirs.members[0].routes.swapAt(0, 1)
    let unresolved = ConfigMerge.reconcile(base: basis, local: mine, remote: theirs)
    check(unresolved.conflicts.count == 1, "Route reorder needs an explicit structural conflict")
    let id = unresolved.conflicts[0].id
    check(id == "members.one.routes", "Conflict should cover the whole ambiguous route list")
    let current = ConfigMerge.reconcile(base: basis, local: mine, remote: theirs, choices: [id: "current"])
    let own = ConfigMerge.reconcile(base: basis, local: mine, remote: theirs, choices: [id: "mine"])
    check(current.config.members[0].routes == theirs.members[0].routes, "Current choice keeps remote order")
    check(own.config.members[0].routes == mine.members[0].routes, "Own choice keeps local effort on original route")
case "route_replacement":
    var mine = original
    mine.members[0].routes[0].effort = "high"
    var theirs = original
    theirs.revision = 5
    theirs.members[0].routes[0].model = "replacement"
    let result = ConfigMerge.reconcile(base: original, local: mine, remote: theirs)
    check(result.conflicts.count == 1 && result.conflicts[0].id == "members.one.routes",
          "Replacing route identity must not inherit another route's effort")
default:
    fatalError("unknown case")
}
''')
    executable = directory / "merge"
    result = subprocess.run([compiler, "-swift-version", "5", str(script), "-o", str(executable)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return executable


@pytest.mark.parametrize("case", ["independent", "same_member_fields", "overlap", "concurrent_adds", "remove_vs_edit",
                                   "stable_route_fields", "route_reorder", "route_replacement"])
def test_merge_rules(merge_rules, case):
    result = subprocess.run([str(merge_rules), case], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_native_reconnect_and_eof_wiring():
    source = SOURCE.read_text()
    refresh = source.split("func refresh(initial:", 1)[1].split("private func sameJSON", 1)[0]
    assert "ready = false" in refresh
    assert "ready = true" in refresh
    assert refresh.index("ready = true") < refresh.index('if let id = selectedJob?["id"]'), \
        "The selected job request must run after /state has restored readiness"
    assert "path == \"/state\"" in source.split("func request(", 1)[1].split("func refresh(", 1)[0]
    assert 'if path == "/state", response.statusCode >= 500 { throw PairError.backendUnavailable }' in source
    assert 'case .backendUnavailable = pairError' in refresh
    assert "handle.readabilityHandler = nil" in source
    assert "if data.isEmpty" in source


def test_silent_bootstrap_has_actionable_manual_recovery_without_killing_jobs():
    source = SOURCE.read_text()
    watchdog = source.split("try? await Task.sleep(nanoseconds: 15_000_000_000)", 1)[1].split("if timer == nil", 1)[0]
    assert "Попробуйте завершить только приложение Pair и открыть его снова" in watchdog
    assert "если ошибка повторится" in watchdog
    assert "Работающие задания не отменялись" in watchdog
    assert "terminate()" not in watchdog


def test_selected_job_and_http_5xx_recover_on_first_successful_poll(tmp_path):
    compiler = shutil.which("swiftc")
    if not compiler:
        pytest.skip("Native reconnect requires Swift")

    config = {
        "version": 1, "revision": 3, "providers": [], "members": [],
        "pair": {"codex": {"model": None, "effort": None},
                 "claude": {"model": None, "effort": None}, "projectRoots": []},
        "limits": {"maxTokens": None, "timeSeconds": None, "budgetUsd": None},
        "jev": {"enabled": False, "providerId": None, "model": "jev-latest"},
        "synthesis": None,
    }
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/state" and requests.count("/state") == 1:
                payload = {"error": "temporary"}
                status = 503
            elif self.path == "/state":
                payload = {"config": config, "keyPresent": {}, "jobs": [], "agents": {}}
                status = 200
            elif self.path == "/jobs/job-1":
                payload = {"id": "job-1", "status": "completed"}
                status = 200
            else:
                self.send_error(404)
                return
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = SOURCE.read_text()
        source = source.replace(
            "    func start() {",
            '''    func testReconnect(port: Int) {
        self.port = port
        self.token = "fixture"
        self.ready = false
        self.reconnecting = true
        self.selectedJob = ["id": "job-1", "status": "running"]
    }
    func start() {''',
            1,
        ).replace("@main\nstruct PairApplication", "struct PairApplication", 1)
        source += '''
@main struct ReconnectHarness {
    @MainActor static func main() async {
        let model = PanelModel()
        model.testReconnect(port: Int(CommandLine.arguments[1])!)
        await model.refresh()
        guard !model.ready else { fatalError("HTTP 503 must leave panel in reconnect state") }
        await model.refresh()
        guard model.ready, model.selectedJob?["status"] as? String == "completed" else {
            fatalError("First reconnect must restore readiness and selected job")
        }
    }
}
'''
        script = tmp_path / "reconnect.swift"
        script.write_text(source)
        executable = tmp_path / "reconnect"
        built = subprocess.run([compiler, "-swift-version", "5", "-parse-as-library", "-framework", "AppKit",
                                "-framework", "SwiftUI", str(script), "-o", str(executable)],
                               capture_output=True, text=True, timeout=90)
        assert built.returncode == 0, built.stderr
        result = subprocess.run([str(executable), str(server.server_port)], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert requests == ["/state", "/state", "/jobs/job-1"]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("failure_mode", ["stale_token", "connection_refused"])
def test_native_rebootstrap_recovers_replaced_port_and_token_without_duplicate_pollers(tmp_path, failure_mode):
    compiler = shutil.which("swiftc")
    if not compiler:
        pytest.skip("Native rebootstrap requires Swift")

    phase = tmp_path / "phase"
    phase.write_text("stable")
    handshake = tmp_path / "handshake.json"
    launches = tmp_path / "launches"
    closed = tmp_path / "old_endpoint_closed"
    stop_watcher = threading.Event()
    config = {
        "version": 1, "revision": 3, "providers": [], "members": [],
        "pair": {"codex": {"model": None, "effort": None},
                 "claude": {"model": None, "effort": None}, "projectRoots": []},
        "limits": {"maxTokens": None, "timeSeconds": None, "budgetUsd": None},
        "jev": {"enabled": False, "providerId": None, "model": "jev-latest"},
        "synthesis": None,
    }

    def make_handler(token, replacement=False):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get("Authorization") != f"Bearer {token}" or (not replacement and phase.read_text() == "replacement"):
                    self.respond(401, {"error": "Local authorization required"})
                elif self.path == "/state" and not replacement and phase.read_text() == "transient":
                    phase.write_text("stable")
                    self.respond(503, {"error": "temporary"})
                elif self.path == "/state":
                    self.respond(200, {"config": config, "keyPresent": {}, "jobs": [], "agents": {}})
                elif self.path == "/jobs/job-1":
                    self.respond(200, {"id": "job-1", "status": "completed" if replacement else "running"})
                else:
                    self.respond(404, {"error": "missing"})

            def respond(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        return Handler

    old_server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler("fixture-one"))
    new_server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler("fixture-two", replacement=True))
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (old_server, new_server)]
    for thread in threads:
        thread.start()
    watcher = None
    if failure_mode == "connection_refused":
        def close_old_when_replaced():
            while not stop_watcher.is_set():
                if phase.read_text() == "replacement":
                    old_server.shutdown()
                    old_server.server_close()
                    closed.write_text("closed")
                    return
                time.sleep(0.01)
        watcher = threading.Thread(target=close_old_when_replaced, daemon=True)
        watcher.start()
    else:
        closed.write_text("use stale-token response")
    handshake.write_text(json.dumps({"port": old_server.server_port, "token": "fixture-one"}))
    package = tmp_path / "pair_core"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text('''import os
from pathlib import Path
with open(os.environ["PAIR_TEST_LAUNCHES"], "a") as log:
    log.write("launch\\n")
print(Path(os.environ["PAIR_TEST_HANDSHAKE"]).read_text(), flush=True)
''')
    try:
        source = SOURCE.read_text().replace(
            "    func start() {",
            "    func testTimer() -> Timer? { timer }\n    func start() {",
            1,
        ).replace("@main\nstruct PairApplication", "struct PairApplication", 1)
        source += r'''
@main struct BootstrapHarness {
    @MainActor static func main() async throws {
        let model = PanelModel()
        model.selectedJob = ["id": "job-1", "status": "running"]
        model.start()
        for _ in 0..<60 {
            if model.ready && model.testTimer() != nil { break }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        guard model.ready, let firstTimer = model.testTimer() else { fatalError("Initial handshake failed") }

        try "transient".write(toFile: CommandLine.arguments[1], atomically: true, encoding: .utf8)
        await model.refresh()
        guard !model.ready else { fatalError("503 must mark the panel unavailable") }
        await model.refresh()
        guard model.ready else { fatalError("Same endpoint should recover after transient 503") }

        let second = ["port": Int(CommandLine.arguments[3])!, "token": "fixture-two"] as [String : Any]
        try JSONSerialization.data(withJSONObject: second).write(to: URL(fileURLWithPath: CommandLine.arguments[2]), options: .atomic)
        try "replacement".write(toFile: CommandLine.arguments[1], atomically: true, encoding: .utf8)
        for _ in 0..<50 {
            if FileManager.default.fileExists(atPath: CommandLine.arguments[4]) { break }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        guard FileManager.default.fileExists(atPath: CommandLine.arguments[4]) else { fatalError("Old endpoint did not close") }
        await model.refresh()
        for _ in 0..<80 {
            if model.ready && model.selectedJob?["status"] as? String == "completed" { break }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        guard model.ready, model.selectedJob?["status"] as? String == "completed" else {
            fatalError("Replacement port/token did not restore the selected job")
        }
        guard model.testTimer() === firstTimer else { fatalError("Rebootstrap created a duplicate timer") }
        for _ in 0..<10 { await model.refresh() }
        model.stop()
    }
}
'''
        script = tmp_path / "bootstrap.swift"
        script.write_text(source)
        executable = tmp_path / "bootstrap"
        built = subprocess.run([compiler, "-swift-version", "5", "-parse-as-library", "-framework", "AppKit",
                                "-framework", "SwiftUI", str(script), "-o", str(executable)],
                               capture_output=True, text=True, timeout=90)
        assert built.returncode == 0, built.stderr
        environment = {**os.environ, "PAIR_PYTHON": sys.executable, "PAIR_ROOT": str(tmp_path),
                       "PAIR_STATE": str(tmp_path / "state"), "PAIR_TEST_HANDSHAKE": str(handshake),
                       "PAIR_TEST_LAUNCHES": str(launches)}
        result = subprocess.run([str(executable), str(phase), str(handshake), str(new_server.server_port), str(closed)],
                                env=environment, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert launches.read_text().splitlines() == ["launch", "launch"], "503 and idle polls must not spawn extra bootstraps"
    finally:
        stop_watcher.set()
        if watcher:
            watcher.join(timeout=1)
        if failure_mode != "connection_refused" or not closed.exists():
            old_server.shutdown()
            old_server.server_close()
        new_server.shutdown()
        new_server.server_close()


def test_conflict_ui_requires_explicit_choice():
    source = SOURCE.read_text()
    panel = source.split("struct PanelView:", 1)[1].split("final class PairWindow:", 1)[0]
    assert "app.mergeConflicts" in panel
    assert '"mine"' in panel and '"current"' in panel
    assert "Черновик" in panel
    save = source.split("func save() async", 1)[1].split("func catalogSignature", 1)[0]
    assert 'conflictChoices[$0.id] != "mine" && conflictChoices[$0.id] != "current"' in save
