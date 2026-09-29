import AppKit
import Foundation
import SwiftUI

struct Route: Codable, Equatable {
    var providerId: String
    var model: String
    var effort: String? = nil
}
struct Provider: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var `protocol`: String
    var baseUrl: String
    var enabled = true
    var manualModels: [String] = []
    var allowLocal = false
}
struct Member: Codable, Identifiable, Equatable {
    var id: String
    var label: String
    var routes: [Route]
}
struct AgentSettings: Codable, Equatable {
    var model: String? = nil
    var effort: String? = nil
}
struct PairSettings: Codable, Equatable {
    var codex = AgentSettings()
    var claude = AgentSettings()
    var projectRoots: [String] = []
}
struct Limits: Codable, Equatable {
    var maxTokens: Int? = nil
    var timeSeconds: Double? = nil
    var budgetUsd: Double? = nil
}
struct JevSettings: Codable, Equatable {
    var enabled = false
    var providerId: String? = nil
    var model = "jev-latest"
}
struct Configuration: Codable, Equatable {
    var version = 1
    var revision = 0
    var providers: [Provider] = []
    var members: [Member] = []
    var pair = PairSettings()
    var limits = Limits()
    var jev = JevSettings()
    var synthesis: Route? = nil
}
struct MergeConflict: Identifiable, Equatable {
    var id: String
    var label: String
    var mine: String
    var current: String
}
struct ConfigMerge {
    struct Result {
        var config: Configuration
        var conflicts: [MergeConflict]
    }
    static func reconcile(base: Configuration, local: Configuration, remote: Configuration,
                          choices: [String: String] = [:]) -> Result {
        var resolver = Resolver(choices: choices)
        var result = remote
        result.version = resolver.field(base.version, local.version, remote.version, "version", "Версия настроек")
        result.providers = resolver.providers(base.providers, local.providers, remote.providers)
        result.members = resolver.members(base.members, local.members, remote.members)
        result.pair = resolver.pair(base.pair, local.pair, remote.pair)
        result.limits = resolver.limits(base.limits, local.limits, remote.limits)
        result.jev = resolver.jev(base.jev, local.jev, remote.jev)
        result.synthesis = resolver.field(base.synthesis, local.synthesis, remote.synthesis,
                                          "synthesis", "Маршрут синтеза")
        result.revision = remote.revision
        return Result(config: result, conflicts: resolver.conflicts)
    }
    private struct Resolver {
        var choices: [String: String]
        var conflicts: [MergeConflict] = []

        mutating func field<T: Equatable>(_ base: T, _ local: T, _ remote: T,
                                          _ id: String, _ label: String) -> T {
            if local == base { return remote }
            if remote == base || local == remote { return local }
            conflicts.append(MergeConflict(id: id, label: label,
                                           mine: summary(local), current: summary(remote)))
            return choices[id] == "current" ? remote : local
        }
        private func summary<T>(_ value: T) -> String {
            if let text = value as? String { return text.isEmpty ? "Пусто" : String(text.prefix(160)) }
            if let flag = value as? Bool { return flag ? "Да" : "Нет" }
            if let strings = value as? [String] { return strings.isEmpty ? "Пусто" : String(strings.joined(separator: ", ").prefix(160)) }
            return String(String(describing: value).prefix(160))
        }
        private func ids<T: Identifiable>(_ local: [T], _ remote: [T]) -> [String] where T.ID == String {
            var ordered: [String] = []
            for id in remote.map(\.id) + local.map(\.id) where !ordered.contains(id) { ordered.append(id) }
            return ordered
        }
        mutating func providers(_ base: [Provider], _ local: [Provider], _ remote: [Provider]) -> [Provider] {
            if local == base { return remote }
            if remote == base || local == remote { return local }
            var result: [Provider] = []
            for id in ids(local, remote) {
                let old = base.first { $0.id == id }
                let mine = local.first { $0.id == id }
                let current = remote.first { $0.id == id }
                if mine == old { if let current { result.append(current) }; continue }
                if current == old || mine == current { if let mine { result.append(mine) }; continue }
                let path = "providers.\(id)"
                let name = mine?.name ?? current?.name ?? id
                guard let old, let mine, let current else {
                    if let item: Provider = field(old, mine, current, path, "Подключение «\(name)» удалено или изменено") {
                        result.append(item)
                    }
                    continue
                }
                var item = current
                item.name = field(old.name, mine.name, current.name, path + ".name", "Подключение «\(name)» · название")
                item.protocol = field(old.protocol, mine.protocol, current.protocol, path + ".protocol", "Подключение «\(name)» · протокол")
                item.baseUrl = field(old.baseUrl, mine.baseUrl, current.baseUrl, path + ".baseUrl", "Подключение «\(name)» · адрес")
                item.enabled = field(old.enabled, mine.enabled, current.enabled, path + ".enabled", "Подключение «\(name)» · включено")
                item.manualModels = field(old.manualModels, mine.manualModels, current.manualModels,
                                          path + ".manualModels", "Подключение «\(name)» · ручные модели")
                item.allowLocal = field(old.allowLocal, mine.allowLocal, current.allowLocal,
                                        path + ".allowLocal", "Подключение «\(name)» · локальный API")
                result.append(item)
            }
            return result
        }
        mutating func members(_ base: [Member], _ local: [Member], _ remote: [Member]) -> [Member] {
            if local == base { return remote }
            if remote == base || local == remote { return local }
            var result: [Member] = []
            for id in ids(local, remote) {
                let old = base.first { $0.id == id }
                let mine = local.first { $0.id == id }
                let current = remote.first { $0.id == id }
                if mine == old { if let current { result.append(current) }; continue }
                if current == old || mine == current { if let mine { result.append(mine) }; continue }
                let path = "members.\(id)"
                let name = mine?.label ?? current?.label ?? id
                guard let old, let mine, let current else {
                    if let item: Member = field(old, mine, current, path, "Участник «\(name)» удалён или изменён") {
                        result.append(item)
                    }
                    continue
                }
                var item = current
                item.label = field(old.label, mine.label, current.label, path + ".label", "Участник «\(name)» · роль")
                item.routes = routes(old.routes, mine.routes, current.routes, path, name)
                result.append(item)
            }
            return result
        }
        mutating func routes(_ base: [Route], _ local: [Route], _ remote: [Route],
                             _ path: String, _ name: String) -> [Route] {
            if local == base { return remote }
            if remote == base || local == remote { return local }
            guard base.count == local.count, local.count == remote.count else {
                return field(base, local, remote, path + ".routes", "Участник «\(name)» · маршруты")
            }
            // Routes have no stable ID. If either side replaced or reordered
            // an identity, an index merge could attach effort to another model.
            let changedIdentity = base.indices.contains { index in
                let old = base[index]
                return local[index].providerId != old.providerId || local[index].model != old.model
                    || remote[index].providerId != old.providerId || remote[index].model != old.model
            }
            let duplicateIdentity = base.indices.contains { index in
                base[..<index].contains { $0.providerId == base[index].providerId && $0.model == base[index].model }
            }
            if changedIdentity || duplicateIdentity {
                return field(base, local, remote, path + ".routes", "Участник «\(name)» · маршруты")
            }
            var result: [Route] = []
            for index in base.indices {
                let old = base[index], mine = local[index], current = remote[index]
                let id = path + ".routes.\(index)"
                let label = "Участник «\(name)» · маршрут \(index + 1)"
                result.append(Route(
                    providerId: field(old.providerId, mine.providerId, current.providerId, id + ".providerId", label + " · источник"),
                    model: field(old.model, mine.model, current.model, id + ".model", label + " · модель"),
                    effort: field(old.effort, mine.effort, current.effort, id + ".effort", label + " · reasoning")))
            }
            return result
        }
        mutating func pair(_ base: PairSettings, _ local: PairSettings, _ remote: PairSettings) -> PairSettings {
            var result = remote
            result.codex.model = field(base.codex.model, local.codex.model, remote.codex.model, "pair.codex.model", "Codex · модель")
            result.codex.effort = field(base.codex.effort, local.codex.effort, remote.codex.effort, "pair.codex.effort", "Codex · reasoning")
            result.claude.model = field(base.claude.model, local.claude.model, remote.claude.model, "pair.claude.model", "Claude · модель")
            result.claude.effort = field(base.claude.effort, local.claude.effort, remote.claude.effort, "pair.claude.effort", "Claude · reasoning")
            result.projectRoots = field(base.projectRoots, local.projectRoots, remote.projectRoots, "pair.projectRoots", "Разрешённые проекты")
            return result
        }
        mutating func limits(_ base: Limits, _ local: Limits, _ remote: Limits) -> Limits {
            Limits(maxTokens: field(base.maxTokens, local.maxTokens, remote.maxTokens, "limits.maxTokens", "Лимиты · токены"),
                   timeSeconds: field(base.timeSeconds, local.timeSeconds, remote.timeSeconds, "limits.timeSeconds", "Лимиты · время"),
                   budgetUsd: field(base.budgetUsd, local.budgetUsd, remote.budgetUsd, "limits.budgetUsd", "Лимиты · бюджет"))
        }
        mutating func jev(_ base: JevSettings, _ local: JevSettings, _ remote: JevSettings) -> JevSettings {
            JevSettings(enabled: field(base.enabled, local.enabled, remote.enabled, "jev.enabled", "Jev · включено"),
                        providerId: field(base.providerId, local.providerId, remote.providerId, "jev.providerId", "Jev · подключение"),
                        model: field(base.model, local.model, remote.model, "jev.model", "Jev · модель"))
        }
    }
}
struct ModelEntry: Identifiable {
    var id: String
    var name: String
    var reasoning: [String]
    var reasoningSupported: Bool? = nil
    var reasoningKnown: Bool? = nil
    var reasoningEnumKnown: Bool? = nil
    var inputModalities: [String]? = nil
    var outputModalities: [String]? = nil
    var textChatCompatible: Bool? = nil
    var interactiveCompatible: Bool? = nil
    var batchVariant = false
    var compatibilityReason: String? = nil
    var listed = true

    static func parse(_ item: [String: Any]) -> ModelEntry? {
        guard let id = item["id"] as? String ?? item["model"] as? String, !id.isEmpty else { return nil }
        let capabilities = item["capabilities"] as? [String: Any] ?? [:]
        let reasoning = item["reasoning"] as? [String]
            ?? (item["reasoning"] as? [[String: Any]] ?? []).compactMap { $0["reasoningEffort"] as? String ?? $0["effort"] as? String }
        return ModelEntry(id: id, name: item["name"] as? String ?? id, reasoning: reasoning,
                          reasoningSupported: capabilities["reasoningSupported"] as? Bool,
                          reasoningKnown: capabilities["reasoningKnown"] as? Bool,
                          reasoningEnumKnown: capabilities["reasoningEnumKnown"] as? Bool,
                          inputModalities: capabilities["inputModalities"] as? [String],
                          outputModalities: capabilities["outputModalities"] as? [String],
                          textChatCompatible: capabilities["textChatCompatible"] as? Bool,
                          interactiveCompatible: capabilities["interactiveCompatible"] as? Bool,
                          batchVariant: capabilities["batchVariant"] as? Bool ?? false,
                          compatibilityReason: capabilities["compatibilityReason"] as? String)
    }
    var isBatch: Bool { batchVariant || id.lowercased().hasSuffix(":batch") }
    var visibleInOrdinaryChat: Bool {
        !isBatch && textChatCompatible != false && interactiveCompatible != false
            && (inputModalities == nil || inputModalities!.contains("text"))
            && (outputModalities == nil || outputModalities!.contains("text"))
    }
    var compatibilityWarning: String? {
        if isBatch { return "Асинхронная batch-модель, не для обычного чата" }
        if !visibleInOrdinaryChat { return "Модель не объявлена совместимой с обычным текстовым чатом" }
        if textChatCompatible == nil { return "Возможности не объявлены; совместимость не гарантирована" }
        return nil
    }
    static func pickerEntries(_ entries: [ModelEntry], selected: String, manualIDs: [String], showAll: Bool) -> [ModelEntry] {
        var result = entries.filter { showAll || $0.visibleInOrdinaryChat || $0.id == selected || manualIDs.contains($0.id) }
        for id in manualIDs + (selected.isEmpty ? [] : [selected]) where !result.contains(where: { $0.id == id }) {
            result.append(ModelEntry(id: id, name: id, reasoning: [], listed: false))
        }
        return result.sorted { $0.id.localizedCaseInsensitiveCompare($1.id) == .orderedAscending }
    }
    func reasoningChoices(saved: String?, allowUnknown: Bool) -> ReasoningChoices {
        let generic = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
        let current = saved.flatMap { $0.isEmpty ? nil : $0 }
        var values: [String] = []
        var warning: String? = nil
        var disabled = false
        if reasoningEnumKnown == true || !reasoning.isEmpty {
            values = reasoning
            if values.isEmpty {
                disabled = current == nil
                warning = "Поставщик объявил пустой список уровней; используется его значение по умолчанию"
            }
            if let current, !values.contains(current) { warning = "Сохранённый уровень не объявлен поставщиком; значение сохранено" }
        } else if reasoningKnown == true && reasoningSupported == false {
            disabled = current == nil
            if current != nil { warning = "Поставщик не объявляет поддержку reasoning; сохранённое значение оставлено" }
        } else if reasoningSupported == true {
            values = generic
            warning = "Уровни не объявлены; значение проверит поставщик"
        } else if allowUnknown || current != nil {
            values = generic
            warning = "Поддержка reasoning неизвестна; значение проверит поставщик"
        }
        if let current, !values.contains(current) { values.append(current) }
        return ReasoningChoices(values: values, warning: warning, disabled: disabled)
    }
}
struct ReasoningChoices {
    var values: [String]
    var warning: String?
    var disabled: Bool
}
struct CatalogSignature: Hashable {
    var protocolName: String
    var baseURL: String
    var enabled: Bool
    var allowLocal: Bool
    var manualModels: [String]
    var keyPresent: Bool
    var keyGeneration: Int
    init(provider: Provider, keyPresent: Bool, keyGeneration: Int) {
        protocolName = provider.protocol; baseURL = provider.baseUrl
        enabled = provider.enabled; allowLocal = provider.allowLocal
        manualModels = provider.manualModels; self.keyPresent = keyPresent; self.keyGeneration = keyGeneration
    }
}
struct CatalogRequestIdentity: Hashable {
    var providerID: String
    var ready: Bool
    var signature: CatalogSignature?
}
struct CatalogFailure {
    static func message(_ code: String) -> String {
        switch code {
        case "missing_key": return "API-ключ не сохранён"
        case "catalog_http_401", "catalog_http_403": return "поставщик отклонил доступ к каталогу"
        case "catalog_http_429": return "поставщик ограничил частоту запросов"
        case "catalog_http_404", "catalog_http_405": return "поставщик не предоставил каталог; можно использовать ручной ID"
        case "catalog_connection_error": return "нет связи с сервером моделей; проверьте сеть, VPN и адрес подключения, затем обновите каталог"
        case "catalog_timeout": return "сервер моделей не ответил вовремя; проверьте сеть, VPN и адрес подключения, затем обновите каталог"
        case "invalid_endpoint", "http_requires_loopback", "private_endpoint_requires_confirmation": return "проверьте сохранённый адрес подключения"
        case "provider_unavailable": return "сохранённое подключение недоступно"
        default: return "подключение к каталогу недоступно"
        }
    }
}
struct CatalogLoadTracker {
    private var attempted: [String: CatalogSignature] = [:]
    private var active: [String: CatalogSignature] = [:]
    mutating func begin(_ id: String, signature: CatalogSignature, force: Bool) -> Bool {
        guard active[id] == nil, force || attempted[id] != signature else { return false }
        attempted[id] = signature; active[id] = signature
        return true
    }
    mutating func finish(_ id: String, signature: CatalogSignature, current: CatalogSignature?) -> Bool {
        guard active[id] == signature else { return false }
        active[id] = nil
        return current == signature
    }
}
struct CatalogSearch {
    static func matches(_ query: String, id: String, name: String) -> Bool {
        let entered = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return entered.isEmpty || id.range(of: entered, options: .caseInsensitive) != nil
            || name.range(of: entered, options: .caseInsensitive) != nil
    }
    static func sourceSuggestion(_ sources: [(id: String, name: String)], selected: String, query: String) -> String? {
        let entered = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !entered.isEmpty else { return nil }
        return sources.first { $0.id != selected && matches(entered, id: $0.id, name: $0.name) }?.name
    }
    static func defaultModel(source: String, configuredNative: String?) -> String {
        ["codex", "claude"].contains(source) ? configuredNative ?? "" : ""
    }
}
struct SourceOption: Identifiable {
    var id: String
    var name: String
}
enum PairError: LocalizedError {
    case message(String)
    case conflict
    case backendUnavailable
    case staleSession
    var errorDescription: String? {
        if case .message(let message) = self { return message }
        if case .conflict = self { return "Настройки изменились в другом окне." }
        if case .backendUnavailable = self { return "Локальный Pair временно недоступен." }
        if case .staleSession = self { return "Подключение к локальному Pair устарело." }
        return "Не удалось выполнить действие."
    }
}

final class LocalSessionDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        // The bearer is valid only for the original loopback endpoint. A
        // provider/backend response must never redirect it to another origin.
        completionHandler(nil)
    }
}

@MainActor
final class PanelModel: ObservableObject {
    @Published var draft = Configuration()
    @Published var saved = Configuration()
    @Published var tab = "Совет"
    @Published var ready = false
    @Published var busy = false
    @Published var message = "Запускаем локальный Pair…"
    @Published var error: String? = nil
    @Published var keyPresent: [String: Bool] = [:]
    @Published var pendingKeys: [String: String] = [:]
    @Published var catalogs: [String: [ModelEntry]] = [:]
    @Published var catalogErrors: [String: String] = [:]
    @Published var catalogSignatures: [String: CatalogSignature] = [:]
    @Published var catalogKeyGenerations: [String: Int] = [:]
    @Published var syncing: Set<String> = []
    @Published var agents: [String: Any] = [:]
    @Published var jobs: [[String: Any]] = []
    @Published var selectedJob: [String: Any]? = nil
    @Published var selectedProject = ""
    @Published var prompt = ""
    @Published var selectedAgent = "codex"
    @Published var invalidFields: [String: String] = [:]
    @Published var mergeConflicts: [MergeConflict] = []
    @Published var conflictChoices: [String: String] = [:]
    weak var rosterUndoManager: UndoManager?
    private var token = ""
    private var port = 0
    private var backend: Process?
    private var timer: Timer?
    private var refreshing = false
    private var reconnecting = false
    private var needsNewHandshake = false
    private var bootstrapping = false
    private var bootstrapGeneration = 0
    private var lastBootstrapAttempt = Date.distantPast
    private var stopped = false
    private var draftBase: Configuration? = nil
    private var catalogTracker = CatalogLoadTracker()
    private var requestedCatalogs: Set<String> = []
    private let session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.urlCache = nil
        configuration.connectionProxyDictionary = [:]
        configuration.timeoutIntervalForRequest = 60
        configuration.timeoutIntervalForResource = 90
        return URLSession(configuration: configuration, delegate: LocalSessionDelegate(), delegateQueue: nil)
    }()
    var dirty: Bool { draft != saved || pendingKeys.values.contains { !$0.isEmpty } || !invalidFields.isEmpty }
    var baseURL: URL? { port > 0 ? URL(string: "http://127.0.0.1:\(port)") : nil }
    var councilSources: [SourceOption] {
        var options: [SourceOption] = []
        for id in ["codex", "claude"] {
            let metadata = agents[id] as? [String: Any] ?? [:]
            let installed = metadata["installed"] as? Bool ?? metadata["available"] as? Bool
            guard installed == true else { continue }
            let name = id == "codex" ? "Codex" : "Claude Code"
            let authenticated = metadata["loggedIn"] as? Bool
            let billing = metadata["billingMode"] as? String
            let label = authenticated == true && billing == "subscription" ? "Подписка \(name)"
                : authenticated == false ? "\(name) · нужен вход"
                : billing == "api" ? "\(name) · CLI с API-входом"
                : "\(name) · вход не проверен"
            options.append(SourceOption(id: id, name: label))
        }
        options += draft.providers.filter { $0.enabled && $0.protocol != "jev" }.map { SourceOption(id: $0.id, name: $0.name) }
        return options
    }
    var preferredSource: String {
        for id in ["codex", "claude"] {
            let metadata = agents[id] as? [String: Any] ?? [:]
            if metadata["loggedIn"] as? Bool == true, metadata["billingMode"] as? String == "subscription", councilSources.contains(where: { $0.id == id }) { return id }
        }
        return councilSources.first?.id ?? ""
    }
    func defaultRoute(for source: String) -> Route {
        let settings = source == "codex" ? draft.pair.codex : source == "claude" ? draft.pair.claude : nil
        let model = CatalogSearch.defaultModel(source: source, configuredNative: settings?.model)
        return Route(providerId: source, model: model, effort: settings?.effort)
    }

    func start() {
        guard !stopped, !bootstrapping else { return }
        bootstrapping = true
        bootstrapGeneration += 1
        let generation = bootstrapGeneration
        lastBootstrapAttempt = Date()
        let environment = ProcessInfo.processInfo.environment
        let resources = Bundle.main.resourceURL!
        let bundledCore = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/PairCore").path
        let product = environment["PAIR_ROOT"].map { URL(fileURLWithPath: $0) }
            ?? resources.appendingPathComponent("product")
        let packagedPython = resources.appendingPathComponent("runtime/bin/python3").path
        let recordedPython = (try? String(contentsOf: resources.appendingPathComponent("python-path.txt"), encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
        let python = environment["PAIR_PYTHON"]
            ?? (FileManager.default.isExecutableFile(atPath: packagedPython) ? packagedPython : nil)
            ?? recordedPython
            ?? product.appendingPathComponent(".venv/bin/python").path
        let useBundledCore = environment["PAIR_PYTHON"] == nil && FileManager.default.isExecutableFile(atPath: bundledCore)
        guard useBundledCore || FileManager.default.isExecutableFile(atPath: python) else {
            bootstrapping = false
            error = "Python Pair не найден. Соберите приложение по инструкции или укажите PAIR_PYTHON."
            message = "Pair не запущен"
            return
        }
        let state = environment["PAIR_STATE"]
            ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Pair").path
        let process = Process()
        process.executableURL = URL(fileURLWithPath: useBundledCore ? bundledCore : python)
        process.arguments = (useBundledCore ? [] : ["-m", "pair_core"]) + ["app-server", "--state", state, "--port", "0"]
        process.currentDirectoryURL = useBundledCore ? resources : product
        var childEnvironment = environment
        childEnvironment["PAIR_KEYCHAIN_HELPER"] = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/pair-keychain").path
        childEnvironment["PYTHONUNBUFFERED"] = "1"
        // Finder-launched apps have a short PATH. These are normal official CLI
        // installation locations, not shell-profile/credential discovery.
        let cliPaths = [NSHomeDirectory() + "/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"]
        childEnvironment["PATH"] = cliPaths.joined(separator: ":")
        process.environment = childEnvironment
        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr
        // Drain backend diagnostics, but never forward a handshake or arbitrary
        // error body to console, pasteboard or an application log.
        stderr.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil }
        }
        process.terminationHandler = { [weak self] terminated in
            Task { @MainActor in
                guard let self else { return }
                guard !self.stopped, self.backend === terminated else { return }
                // A bootstrap process can return an existing shared server's
                // descriptor and exit successfully. Polling verifies that server.
                if terminated.terminationStatus == 0 { return }
                self.ready = false
                self.reconnecting = true
                self.needsNewHandshake = true
                self.message = "Локальный процесс остановлен"
                self.error = "Pair остановился. Панель восстановит связь; черновик сохранён."
                self.rebootstrapIfNeeded()
            }
        }
        do {
            try process.run()
            backend = process
            Task { @MainActor [weak self, weak process] in
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                guard let self, let process, !self.stopped, self.backend === process,
                      self.bootstrapGeneration == generation, self.bootstrapping else { return }
                // A process that might own shared jobs is never terminated by
                // a panel timeout. One pending bootstrap remains one process.
                self.ready = false
                self.message = "Нужен повторный запуск панели"
                self.error = "Локальный Pair не подтвердил запуск за 15 секунд. Попробуйте завершить только приложение Pair и открыть его снова; если ошибка повторится, проверьте статус Pair в чате. Работающие задания не отменялись."
            }
            if timer == nil {
                timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
                    Task { @MainActor in
                        guard let self else { return }
                        await self.refresh()
                        self.rebootstrapIfNeeded()
                    }
                }
            }
            let handle = stdout.fileHandleForReading
            Task.detached { [weak self] in
                var line = Data()
                while line.count < 4096 {
                    let byte = handle.readData(ofLength: 1)
                    if byte.isEmpty || byte == Data([10]) { break }
                    line.append(byte)
                }
                let handshake = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any]
                let serverPort = handshake?["port"] as? Int
                let serverToken = handshake?["token"] as? String
                await self?.connected(port: serverPort, token: serverToken, generation: generation)
                // A second stdout message must not block the backend pipe.
                while !handle.readData(ofLength: 4096).isEmpty {}
            }
        } catch {
            bootstrapping = false
            self.error = "Не удалось запустить локальный процесс Pair. Проверьте сборку и Python."
            message = "Pair не запущен"
        }
    }
    private func rebootstrapIfNeeded() {
        guard !stopped, needsNewHandshake, !bootstrapping,
              Date().timeIntervalSince(lastBootstrapAttempt) >= 9 else { return }
        start()
    }
    private func connected(port: Int?, token: String?, generation: Int) async {
        guard generation == bootstrapGeneration else { return }
        bootstrapping = false
        guard !stopped else { return }
        guard let port, (1...65535).contains(port), let token, !token.isEmpty else {
            needsNewHandshake = true
            error = "Локальный процесс не подтвердил готовность. Настройки и ключи не передавались."
            return
        }
        self.port = port
        self.token = token
        needsNewHandshake = false
        error = nil
        ready = true
        await refresh(initial: true)
    }
    func stop() {
        stopped = true
        timer?.invalidate()
        timer = nil
        backend?.terminationHandler = nil
        // The backend and official CLI jobs also serve ordinary MCP clients.
        // Quitting a settings panel must not terminate their ongoing work.
        token = ""
        pendingKeys.removeAll()
        session.invalidateAndCancel()
    }
    func request(_ path: String, method: String = "GET", body: Any? = nil) async throws -> Any {
        guard (ready || (method == "GET" && path == "/state")), let base = baseURL,
              path.hasPrefix("/"), !path.contains("://"),
              let url = URL(string: path, relativeTo: base), url.host == "127.0.0.1", url.port == port else {
            throw PairError.message("Pair ещё не готов. Подождите запуска локального процесса.")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if body != nil || ["POST", "PUT", "PATCH"].contains(method) {
            request.httpBody = try JSONSerialization.data(withJSONObject: body ?? [:])
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse,
              response.url?.host == "127.0.0.1", response.url?.port == port else {
            throw PairError.message("Ответ не от локального Pair. Действие отменено.")
        }
        let object = (try? JSONSerialization.jsonObject(with: data)) ?? [:]
        guard (200..<300).contains(response.statusCode) else {
            if path == "/config", method == "PUT", response.statusCode == 409 { throw PairError.conflict }
            if path == "/state", response.statusCode == 401 { throw PairError.staleSession }
            if path == "/state", response.statusCode >= 500 { throw PairError.backendUnavailable }
            let detail = (object as? [String: Any])?["error"] as? String
                ?? (object as? [String: Any])?["message"] as? String
                ?? "Операция не выполнена (\(response.statusCode))."
            throw PairError.message(detail)
        }
        return object
    }
    func refresh(initial: Bool = false) async {
        guard !stopped, port > 0, !token.isEmpty, !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            guard let state = try await request("/state") as? [String: Any] else {
                throw PairError.message("Локальный Pair вернул неполное состояние.")
            }
            let receiptURL = Bundle.main.resourceURL!.appendingPathComponent("Core/_internal/release-info.json")
            if FileManager.default.fileExists(atPath: receiptURL.path) {
                let receipt = (try? Data(contentsOf: receiptURL)).flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
                let expected = receipt?["sourceFingerprint"] as? String
                let actual = (state["build"] as? [String: Any])?["sourceFingerprint"] as? String
                if expected == nil || actual != expected {
                    ready = false
                    message = "Нужен переход на новую версию Pair"
                    throw PairError.message("Работает другая версия локального Pair. Сначала дождитесь её задач, затем обновите приложение; задания не прерывались.")
                }
            }
            guard let config = decodeConfig(state) else {
                throw PairError.message("Локальный Pair вернул неполные настройки.")
            }
            applyConfig(config)
            let nextKeys = state["keyPresent"] as? [String: Bool] ?? [:]
            let nextJobs = state["jobs"] as? [[String: Any]] ?? []
            let nextAgents = state["agents"] as? [String: Any] ?? agents
            if keyPresent != nextKeys { keyPresent = nextKeys }
            if !sameJSON(jobs, nextJobs) { jobs = nextJobs }
            if !sameJSON(agents, nextAgents) { agents = nextAgents }
            let wasUnavailable = !ready
            ready = true
            lastBootstrapAttempt = .distantPast
            if reconnecting || wasUnavailable {
                reconnecting = false
                error = nil
                message = "Связь с локальным Pair восстановлена"
            } else if initial { message = "Готово · изменения применяются к следующему вызову" }
            if let id = selectedJob?["id"] as? String {
                if let nextJob = (try? await request("/jobs/\(id)")) as? [String: Any], !sameJSON(selectedJob ?? [:], nextJob) {
                    selectedJob = nextJob
                }
            }
        } catch {
            let backendUnavailable: Bool
            if let pairError = error as? PairError, case .backendUnavailable = pairError {
                backendUnavailable = true
            } else { backendUnavailable = false }
            let staleSession: Bool
            if let pairError = error as? PairError, case .staleSession = pairError {
                staleSession = true
            } else { staleSession = false }
            let networkError = error as NSError
            let connectionLost = networkError.domain == NSURLErrorDomain && [
                URLError.cannotConnectToHost.rawValue,
                URLError.networkConnectionLost.rawValue,
                URLError.cannotFindHost.rawValue,
            ].contains(networkError.code)
            if networkError.domain == NSURLErrorDomain || backendUnavailable || staleSession {
                reconnecting = true
                self.ready = false
                self.message = "Связь с локальным Pair потеряна"
                self.error = "Локальный процесс недоступен. Pair проверит связь снова; черновик сохранён в панели."
                if connectionLost || staleSession {
                    needsNewHandshake = true
                    rebootstrapIfNeeded()
                }
            } else if initial { self.error = error.localizedDescription }
        }
    }
    private func decodeConfig(_ state: [String: Any]) -> Configuration? {
        guard let object = state["config"],
              let data = try? JSONSerialization.data(withJSONObject: object) else { return nil }
        return try? JSONDecoder().decode(Configuration.self, from: data)
    }
    private func applyConfig(_ config: Configuration) {
        let hadUnsavedChanges = dirty
        if !hadUnsavedChanges && draft != config {
            rosterUndoManager?.removeAllActions()
            draft = config
            draftBase = nil
            mergeConflicts = []
            conflictChoices = [:]
        } else if hadUnsavedChanges && saved.revision != config.revision {
            if draftBase == nil { draftBase = saved }
            mergeConflicts = []
            conflictChoices = [:]
        }
        if saved != config { saved = config }
        if selectedProject.isEmpty { selectedProject = config.pair.projectRoots.first ?? "" }
    }
    private func latestConfig() async throws -> Configuration {
        guard let state = try await request("/state") as? [String: Any],
              let config = decodeConfig(state) else {
            throw PairError.message("Не удалось загрузить актуальные настройки Pair.")
        }
        applyConfig(config)
        return config
    }
    private func sameJSON(_ lhs: Any, _ rhs: Any) -> Bool {
        NSDictionary(dictionary: ["value": lhs]).isEqual(to: ["value": rhs])
    }
    func save() async {
        guard !busy else { return }
        guard invalidFields.isEmpty else { error = invalidFields.keys.sorted().compactMap { invalidFields[$0] }.first; return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            var committed: Configuration? = nil
            for attempt in 0...1 {
                var candidate = draft
                if draft.revision != saved.revision {
                    guard let base = draftBase else {
                        throw PairError.message("Не удалось сверить черновик. Снова загрузите настройки Pair.")
                    }
                    let merged = ConfigMerge.reconcile(base: base, local: draft, remote: saved, choices: conflictChoices)
                    mergeConflicts = merged.conflicts
                    if merged.conflicts.contains(where: { conflictChoices[$0.id] != "mine" && conflictChoices[$0.id] != "current" }) {
                        error = "Настройки изменились в другом окне. Выберите вариант для каждого пересечения и повторите сохранение."
                        message = "Черновик сохранён в панели"
                        return
                    }
                    candidate = merged.config
                }
                if candidate == saved {
                    committed = saved
                    break
                }
                do {
                    let data = try JSONEncoder().encode(candidate)
                    let object = try JSONSerialization.jsonObject(with: data)
                    let response = try await request("/config", method: "PUT", body: object)
                    guard let data = try? JSONSerialization.data(withJSONObject: (response as? [String: Any])?["config"] ?? response),
                          let accepted = try? JSONDecoder().decode(Configuration.self, from: data) else {
                        throw PairError.message("Pair не подтвердил сохранение. Проверьте настройки перед повтором.")
                    }
                    committed = accepted
                    break
                } catch PairError.conflict {
                    _ = try await latestConfig()
                    if attempt == 1 {
                        throw PairError.message("Настройки снова изменились. Черновик сохранён; проверьте изменения и повторите сохранение.")
                    }
                }
            }
            guard let committed else { return }
            rosterUndoManager?.removeAllActions()
            draft = committed
            saved = committed
            draftBase = nil
            mergeConflicts = []
            conflictChoices = [:]
            for provider in draft.providers {
                if let key = pendingKeys[provider.id], !key.isEmpty {
                    _ = try await request("/providers/\(provider.id)/key", method: "POST", body: ["key": key])
                    pendingKeys[provider.id] = nil
                    catalogKeyGenerations[provider.id, default: 0] += 1
                }
            }
            message = "Сохранено · следующий вызов использует новые настройки"
            await refresh()
        } catch {
            self.error = error.localizedDescription
            message = "Не всё сохранено · проверьте сообщение"
        }
    }
    func catalogSignature(_ providerID: String) -> CatalogSignature? {
        guard let provider = saved.providers.first(where: { $0.id == providerID }) else { return nil }
        return CatalogSignature(provider: provider, keyPresent: keyPresent[providerID] == true,
                                keyGeneration: catalogKeyGenerations[providerID] ?? 0)
    }
    func catalogRequestIdentity(_ providerID: String) -> CatalogRequestIdentity {
        CatalogRequestIdentity(providerID: providerID, ready: ready && !busy, signature: catalogSignature(providerID))
    }
    func ensureCatalogLoaded(_ providerID: String) {
        guard !["codex", "claude"].contains(providerID), !providerID.isEmpty else { return }
        requestedCatalogs.insert(providerID)
        // This task belongs to the model, so leaving a view does not cancel a
        // shared catalog request used by another route or the Connections tab.
        Task { await syncModels(providerID, force: false) }
    }
    func catalogStatus(_ providerID: String) -> [String] {
        guard !["codex", "claude"].contains(providerID) else { return [] }
        var status: [String] = []
        if syncing.contains(providerID) { status.append("Загрузка каталога…") }
        if let error = catalogErrors[providerID] {
            status.append(catalogs[providerID] == nil ? "Каталог не загружен: \(error)"
                          : "Каталог не загружен: \(error). Показан последний известный каталог")
        }
        if let loaded = catalogSignatures[providerID], loaded != catalogSignature(providerID) {
            status.append("Каталог от прежнего подключения")
        }
        if !syncing.contains(providerID), catalogErrors[providerID] == nil {
            if saved.providers.first(where: { $0.id == providerID }) == nil { status.append("Сначала сохраните подключение и ключ") }
            else if keyPresent[providerID] != true { status.append("Сначала сохраните API-ключ") }
            else if catalogs[providerID]?.isEmpty == true { status.append("Каталог пуст. Можно ввести точный ID вручную") }
        }
        return status
    }
    func syncModels(_ providerID: String, force: Bool = true) async {
        guard !syncing.contains(providerID) else { return }
        if ["codex", "claude"].contains(providerID) {
            syncing.insert(providerID)
            defer { syncing.remove(providerID) }
            do {
                let updated = try await request("/agents") as? [String: Any] ?? agents
                if !sameJSON(agents, updated) { agents = updated }
                let count = models(for: providerID).count
                message = count > 0 ? "Список официального CLI обновлён · \(count) моделей" : "CLI не предоставил каталог; можно указать точный ID вручную"
            } catch { self.error = error.localizedDescription }
            return
        }
        guard ready, !busy, let signature = catalogSignature(providerID), signature.enabled,
              signature.protocolName != "jev", signature.keyPresent else {
            if force { catalogErrors[providerID] = "Сначала сохраните подключение и ключ" }
            return
        }
        guard catalogTracker.begin(providerID, signature: signature, force: force) else { return }
        syncing.insert(providerID)
        catalogErrors[providerID] = nil
        defer {
            _ = catalogTracker.finish(providerID, signature: signature, current: catalogSignature(providerID))
            syncing.remove(providerID)
            if requestedCatalogs.contains(providerID), catalogSignature(providerID) != signature {
                ensureCatalogLoaded(providerID)
            }
        }
        do {
            let response = try await request("/providers/\(providerID)/models", method: "POST")
            guard catalogSignature(providerID) == signature else { return }
            let entries = (response as? [String: Any])?["models"] as? [[String: Any]] ?? response as? [[String: Any]] ?? []
            catalogs[providerID] = entries.compactMap(ModelEntry.parse)
            catalogSignatures[providerID] = signature
            if force { message = "Каталог обновлён · \(entries.count) моделей" }
        } catch {
            guard catalogSignature(providerID) == signature else { return }
            catalogErrors[providerID] = (error as NSError).domain == NSURLErrorDomain
                ? "нет связи с локальным Pair" : CatalogFailure.message(error.localizedDescription)
        }
    }
    func models(for providerID: String, selected: String = "") -> [ModelEntry] {
        var entries = catalogs[providerID] ?? []
        if ["codex", "claude"].contains(providerID) {
            let metadata = agents[providerID] as? [String: Any] ?? [:]
            entries = (metadata["models"] as? [[String: Any]] ?? []).compactMap { item in
                guard var entry = ModelEntry.parse(item) else { return nil }
                entry.textChatCompatible = true; entry.interactiveCompatible = true
                return entry
            }
        }
        let manual = draft.providers.first { $0.id == providerID }?.manualModels ?? []
        for id in manual + (selected.isEmpty ? [] : [selected]) where !entries.contains(where: { $0.id == id }) {
            entries.append(ModelEntry(id: id, name: id, reasoning: [], listed: false))
        }
        return entries.sorted { $0.id.localizedCaseInsensitiveCompare($1.id) == .orderedAscending }
    }
    func addMember() {
        replaceRoster(draft.members + [Member(id: UUID().uuidString, label: "Участник \(draft.members.count + 1)", routes: [defaultRoute(for: preferredSource)])], actionName: "Добавить участника")
    }
    func removeMember(_ id: String) {
        replaceRoster(draft.members.filter { $0.id != id }, actionName: "Убрать участника")
    }
    private func replaceRoster(_ members: [Member], actionName: String) {
        guard draft.members != members else { return }
        let previous = draft.members
        rosterUndoManager?.registerUndo(withTarget: self) { target in
            target.replaceRoster(previous, actionName: actionName)
        }
        rosterUndoManager?.setActionName(actionName)
        draft.members = members
    }
    func addProvider() {
        draft.providers.append(Provider(id: UUID().uuidString, name: "Новое подключение", protocol: "openai", baseUrl: "https://", manualModels: []))
    }
    func removeProvider(_ id: String) {
        guard !draft.members.contains(where: { $0.routes.contains(where: { $0.providerId == id }) }),
              draft.synthesis?.providerId != id, draft.jev.providerId != id else {
            error = "Это подключение используется в совете или Jev. Сначала смените его в настройках."
            return
        }
        draft.providers.removeAll { $0.id == id }
        pendingKeys[id] = nil
        catalogs[id] = nil
        // Saved Keychain deletion requires an explicit second action, not a
        // silent destructive side effect of editing an unsaved configuration.
    }
    func deleteKey(_ id: String) async {
        do {
            _ = try await request("/providers/\(id)/key", method: "DELETE")
            pendingKeys[id] = nil
            await refresh()
            message = "Ключ удалён из Keychain"
        } catch { self.error = error.localizedDescription }
    }
    func chooseProject() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.title = "Выберите папку, в которой агенту разрешено работать"
        panel.prompt = "Разрешить эту папку"
        if panel.runModal() == .OK, let url = panel.url {
            let path = url.standardizedFileURL.path
            if !draft.pair.projectRoots.contains(path) { draft.pair.projectRoots.append(path) }
            selectedProject = path
        }
    }
    func startJob() async {
        guard !busy, !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        guard saved.pair.projectRoots.contains(selectedProject) else {
            error = "Сохраните разрешение на выбранную папку перед запуском задачи."
            return
        }
        busy = true
        defer { busy = false }
        do {
            let settings = selectedAgent == "codex" ? saved.pair.codex : saved.pair.claude
            var body: [String: Any] = ["kind": "cli", "agent": selectedAgent, "prompt": prompt, "project": selectedProject, "mode": "subscription"]
            if let model = settings.model, !model.isEmpty { body["model"] = model }
            if let effort = settings.effort, !effort.isEmpty { body["effort"] = effort }
            body["limits"] = try JSONSerialization.jsonObject(with: JSONEncoder().encode(saved.limits))
            selectedJob = try await request("/jobs", method: "POST", body: body) as? [String: Any]
            message = "Задача передана официальному агенту"
            await refresh()
        } catch { self.error = error.localizedDescription }
    }
    func cancelJob(_ id: String) async {
        do {
            selectedJob = try await request("/jobs/\(id)/cancel", method: "POST") as? [String: Any]
            await refresh()
        } catch { self.error = error.localizedDescription }
    }
    func inspectJob(_ id: String) async {
        do { selectedJob = try await request("/jobs/\(id)") as? [String: Any] }
        catch { self.error = error.localizedDescription }
    }
    func connectCodex() async {
        do {
            let result = try await request("/install/codex", method: "POST") as? [String: Any]
            message = result?["message"] as? String ?? "Интеграция Pair установлена. Откройте новый чат Codex."
        } catch { self.error = error.localizedDescription }
    }
    func connectClaude() async {
        do {
            let result = try await request("/install/claude", method: "POST") as? [String: Any]
            message = result?["message"] as? String ?? "Интеграция Pair установлена. Откройте новый чат Claude Code."
        } catch { self.error = error.localizedDescription }
    }
}

// Named SwiftUI GroupBox labels reproducibly trap this Computer Use build while
// processing AX children. Keep the visible heading as ordinary accessible text
// inside the same unlabeled GroupBox already used by members and agent cards.
// This is a compatibility workaround, not a change to backend or permissions.
struct PanelSection<Content: View>: View {
    let title: String
    let content: Content
    init(_ title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }
    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                Text(title).font(.headline)
                content
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

struct RouteEditor: View {
    @EnvironmentObject var app: PanelModel
    @Binding var route: Route
    var label: String = ""
    @State private var search = ""
    @State private var manual = false
    @State private var showAll = false
    @State private var allowUnknownReasoning = false
    private enum Field: Hashable { case search, source, model }
    @FocusState private var focused: Field?
    private var allSources: [SourceOption] {
        var sources = app.councilSources
        if !route.providerId.isEmpty, !sources.contains(where: { $0.id == route.providerId }) {
            sources.append(SourceOption(id: route.providerId, name: "\(route.providerId) · недоступен"))
        }
        return sources
    }
    private var selectedSource: SourceOption? { allSources.first { $0.id == route.providerId } }
    private var modelQuery: String { search.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var sourceSuggestion: String? {
        CatalogSearch.sourceSuggestion(allSources.map { (id: $0.id, name: $0.name) }, selected: route.providerId, query: search)
    }
    private var available: [ModelEntry] { app.models(for: route.providerId, selected: route.model) }
    private var ordinaryChoices: [ModelEntry] {
        ModelEntry.pickerEntries(available, selected: route.model,
                                manualIDs: app.draft.providers.first { $0.id == route.providerId }?.manualModels ?? [], showAll: showAll)
    }
    private var filtered: [ModelEntry] {
        let entries = ordinaryChoices.filter { CatalogSearch.matches(modelQuery, id: $0.id, name: $0.name) }
        return modelQuery.isEmpty ? Array(entries.prefix(100)) : entries
    }
    private var options: [ModelEntry] {
        var entries = filtered
        if !route.model.isEmpty, !entries.contains(where: { $0.id == route.model }) {
            entries.insert(available.first { $0.id == route.model } ?? ModelEntry(id: route.model, name: route.model, reasoning: [], listed: false), at: 0)
        }
        return entries
    }
    private var selectedEntry: ModelEntry? { available.first { $0.id == route.model } }
    private var reasoningChoices: ReasoningChoices {
        (selectedEntry ?? ModelEntry(id: route.model, name: route.model, reasoning: [], listed: false))
            .reasoningChoices(saved: route.effort, allowUnknown: allowUnknownReasoning)
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            if !label.isEmpty { Text(label).font(.caption).foregroundStyle(.secondary) }
            HStack {
                Text("Источник").frame(width: 75, alignment: .leading)
                Picker("Источник", selection: $route.providerId) {
                    Text("Выберите источник").tag("")
                    ForEach(allSources) { source in
                        Text(source.name).tag(source.id)
                    }
                }.labelsHidden().frame(maxWidth: .infinity).focused($focused, equals: .source)
                Button { Task { await app.syncModels(route.providerId) } } label: {
                    Image(systemName: "arrow.clockwise")
                }.disabled(route.providerId.isEmpty || app.syncing.contains(route.providerId))
                    .help("Обновить каталог API или официального CLI; генерация не запускается")
            }
            TextField("Поиск модели", text: $search)
                .accessibilityLabel("Поиск модели")
                .focused($focused, equals: .search)
            ForEach(app.catalogStatus(route.providerId), id: \.self) { status in
                Text(status).font(.caption).foregroundStyle(.secondary)
            }
            if manual {
                TextField("Точный ID модели", text: $route.model)
                    .accessibilityLabel("Точный ID модели")
                    .focused($focused, equals: .model)
            } else {
                Picker("Модель", selection: $route.model) {
                    Text("Выберите модель").tag("")
                    ForEach(options) { entry in Text(entry.id).tag(entry.id) }
                }.labelsHidden().focused($focused, equals: .model)
            }
            HStack {
                Toggle("Ввести ID вручную", isOn: $manual).toggleStyle(.checkbox).font(.caption)
                Spacer()
                Picker("Reasoning", selection: Binding(get: { route.effort ?? "" }, set: { route.effort = $0.isEmpty ? nil : $0 })) {
                    Text("По умолчанию поставщика").tag("")
                    ForEach(reasoningChoices.values, id: \.self) { Text($0).tag($0) }
                }.frame(width: 270).disabled(reasoningChoices.disabled)
            }
            Toggle("Весь каталог (включая batch и медиа)", isOn: $showAll).toggleStyle(.checkbox).font(.caption)
            if selectedEntry?.reasoningEnumKnown != true && selectedEntry?.reasoningKnown != true && selectedEntry?.reasoningSupported != true && (selectedEntry?.reasoning.isEmpty ?? true) {
                Toggle("Указать reasoning при неизвестной поддержке", isOn: $allowUnknownReasoning).toggleStyle(.checkbox).font(.caption)
            }
            if let warning = reasoningChoices.warning { Text(warning).font(.caption).foregroundStyle(.secondary) }
            if let selected = selectedEntry, !route.model.isEmpty {
                if !selected.listed { Text("Выбранная модель отсутствует в каталоге").font(.caption).foregroundStyle(.secondary) }
                if let warning = selected.compatibilityWarning { Text(warning).font(.caption).foregroundStyle(.secondary) }
            }
            if modelQuery.isEmpty && ordinaryChoices.count > 100 {
                Text("\(ordinaryChoices.count) моделей · показаны первые 100, введите поиск").font(.caption2).foregroundStyle(.secondary)
            }
            if !modelQuery.isEmpty && filtered.isEmpty {
                if let suggestion = sourceSuggestion {
                    Text("\(suggestion) — это источник. Выберите его в поле «Источник». Текущая модель сохранена.")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    Text("Нет совпадений по «\(modelQuery)» в моделях \(selectedSource?.name ?? "источника"). Текущая модель сохранена.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .task(id: app.catalogRequestIdentity(route.providerId)) { app.ensureCatalogLoaded(route.providerId) }
        .onChange(of: route.providerId) { source in search = ""; allowUnknownReasoning = false; route = app.defaultRoute(for: source); focused = .source }
        .onChange(of: route.model) { _ in allowUnknownReasoning = false }
    }
}

struct MemberEditor: View {
    @EnvironmentObject var app: PanelModel
    @Binding var member: Member
    var expanded: Bool
    var toggle: () -> Void
    var remove: () -> Void
    @State private var reserves = false
    private var primary: Route? { member.routes.first }
    private var sourceName: String {
        guard let source = primary?.providerId, !source.isEmpty else { return "Источник не выбран" }
        return app.councilSources.first { $0.id == source }?.name ?? "\(source) · недоступен"
    }
    private var modelName: String {
        guard let model = primary?.model, !model.isEmpty else { return "Модель не выбрана" }
        return model
    }
    private var routeWarning: String? {
        guard let route = primary, !route.providerId.isEmpty else { return "Источник не выбран" }
        if route.model.isEmpty { return "Модель не выбрана" }
        if !["codex", "claude"].contains(route.providerId) {
            if app.keyPresent[route.providerId] != true { return "API-ключ не сохранён" }
            if app.catalogErrors[route.providerId] != nil { return "Каталог недоступен; модель сохранена" }
        }
        return nil
    }
    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 7) {
                HStack {
                    Text("Роль: \(member.label.isEmpty ? "не задана" : member.label)").font(.headline).lineLimit(1)
                    Spacer(minLength: 8)
                    Button(expanded ? "Готово" : "Настроить", action: toggle).font(.caption)
                        .accessibilityLabel("\(expanded ? "Свернуть" : "Настроить") роль \(member.label)")
                }
                HStack(spacing: 5) {
                    Text(sourceName).lineLimit(1)
                    Image(systemName: "chevron.right").font(.caption2)
                    Text(modelName).lineLimit(1).truncationMode(.middle)
                    Spacer(minLength: 4)
                    if let effort = primary?.effort, !effort.isEmpty { Text(effort).lineLimit(1) }
                    if member.routes.count > 1 { Text("+\(member.routes.count - 1) резерв").lineLimit(1) }
                }.font(.caption).foregroundStyle(.secondary)
                if let warning = routeWarning {
                    Text("⚠ \(warning)").font(.caption2).foregroundStyle(.orange)
                }
                if expanded {
                    Divider()
                    TextField("Роль (как отвечать)", text: $member.label)
                        .accessibilityLabel("Роль (как отвечать)")
                    Text("Эта роль входит в инструкцию модели; опишите, как участник должен отвечать.")
                        .font(.caption).foregroundStyle(.secondary)
                    if !member.routes.isEmpty {
                        RouteEditor(route: $member.routes[0], label: "Основной маршрут")
                    }
                    DisclosureGroup("Резервы (\(max(0, member.routes.count - 1)))", isExpanded: $reserves) {
                        VStack(spacing: 10) {
                            ForEach(Array(member.routes.indices.dropFirst()), id: \.self) { index in
                                HStack(alignment: .top) {
                                    RouteEditor(route: $member.routes[index], label: "Резерв \(index)")
                                    Button { member.routes.remove(at: index) } label: { Image(systemName: "minus.circle") }
                                        .buttonStyle(.borderless).help("Убрать резерв")
                                }
                            }
                            Button("Добавить резерв", systemImage: "plus") {
                                member.routes.append(app.defaultRoute(for: app.preferredSource))
                            }
                        }.padding(.top, 8)
                    }.font(.caption)
                    Button(role: .destructive, action: remove) {
                        Label("Убрать участника", systemImage: "minus.circle")
                    }.font(.caption)
                }
            }.padding(6).frame(maxWidth: .infinity, alignment: .leading)
        }
        .task(id: app.catalogRequestIdentity(primary?.providerId ?? "")) {
            app.ensureCatalogLoaded(primary?.providerId ?? "")
        }
    }
}

struct LimitsEditor: View {
    @EnvironmentObject var app: PanelModel
    @State private var expanded = false
    var body: some View {
        DisclosureGroup("Лимиты и экономия", isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    NumericSetting(label: "Токенов на ответ", id: "maxTokens", integer: true, minimum: 1, value: Binding(get: { app.draft.limits.maxTokens.map(Double.init) }, set: { app.draft.limits.maxTokens = $0.map(Int.init) }))
                    NumericSetting(label: "Время, секунд", id: "timeSeconds", minimum: 0, strictlyGreater: true, value: $app.draft.limits.timeSeconds)
                    NumericSetting(label: "Бюджет, $", id: "budgetUsd", minimum: 0, value: $app.draft.limits.budgetUsd)
                }
                Text("Пустое поле — без собственного лимита. Квоты и ограничения поставщиков остаются; токены подписочного CLI нельзя ограничить как API.")
                    .font(.caption).foregroundStyle(.secondary)
                Toggle("Jev: инструменты отбора и проверки", isOn: $app.draft.jev.enabled)
                if app.draft.jev.enabled {
                    Picker("Подключение Jev", selection: Binding(get: { app.draft.jev.providerId ?? "" }, set: { app.draft.jev.providerId = $0.isEmpty ? nil : $0 })) {
                        Text("Выберите в Подключениях").tag("")
                        ForEach(app.draft.providers.filter { $0.protocol == "jev" || URL(string: $0.baseUrl)?.host == "openrouter.ai" }) { Text($0.name).tag($0.id) }
                    }
                    .onChange(of: app.draft.jev.providerId) { id in
                        guard let provider = app.draft.providers.first(where: { $0.id == id }) else { return }
                        app.draft.jev.model = URL(string: provider.baseUrl)?.host == "openrouter.ai" ? "typesafe/jev-1.13" : "jev-latest"
                    }
                    Text("Jev оплачивается по тарифу своего API. Не заменяет тесты; инструменты вызываются явно, исходные данные сохраняются.").font(.caption).foregroundStyle(.secondary)
                }
            }.padding(.top, 8)
        }
    }
}

struct NumericSetting: View {
    @EnvironmentObject var app: PanelModel
    var label: String
    var id: String
    var integer = false
    var minimum: Double
    var strictlyGreater = false
    @Binding var value: Double?
    @State private var text = ""
    @FocusState private var focused: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.caption)
            TextField("Без ограничения", text: $text).focused($focused)
                .accessibilityLabel(label)
                .onChange(of: text) { entered in
                    let raw = entered.trimmingCharacters(in: .whitespaces)
                    if raw.isEmpty {
                        value = nil
                        app.invalidFields[id] = nil
                    } else if let number = Double(raw.replacingOccurrences(of: ",", with: ".")), number.isFinite,
                              (strictlyGreater ? number > minimum : number >= minimum),
                              !integer || (number.rounded() == number && number < Double(Int.max)) {
                        value = number
                        app.invalidFields[id] = nil
                    } else {
                        app.invalidFields[id] = "\(label): введите \(integer ? "целое " : "")число \(strictlyGreater ? "больше" : "не меньше") \(Int(minimum)) или очистите поле."
                    }
                }
            if app.invalidFields[id] != nil { Text("Проверьте число").font(.caption2).foregroundStyle(.red) }
        }
        .onAppear { updateText() }
        .onChange(of: value) { _ in if !focused { updateText() } }
    }
    private func updateText() {
        if let number = value { text = integer ? String(Int(number)) : String(number) }
        else { text = "" }
    }
}

struct CouncilPanel: View {
    @EnvironmentObject var app: PanelModel
    @State private var expandedMemberID: String? = nil
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Участники совета").font(.headline)
            Text("Независимые ответы на вопрос из обычного чата Codex или Claude.")
                .font(.caption).foregroundStyle(.secondary)
            if app.draft.members.isEmpty {
                GroupBox {
                    VStack(spacing: 8) {
                        Image(systemName: "person.3").font(.title2)
                        Text("Соберите свой совет").font(.headline)
                        Text(app.councilSources.isEmpty ? "Подключите официальный CLI или API в «Подключениях»." : "Выберите доступную подписку или API-модели.")
                            .foregroundStyle(.secondary)
                    }.frame(maxWidth: .infinity).padding(12)
                }
            }
            ForEach($app.draft.members) { $member in
                MemberEditor(member: $member, expanded: expandedMemberID == member.id, toggle: {
                    expandedMemberID = expandedMemberID == member.id ? nil : member.id
                }) {
                    if expandedMemberID == member.id { expandedMemberID = nil }
                    app.removeMember(member.id)
                }
            }
            Button("Добавить участника", systemImage: "plus") { app.addMember() }
                .disabled(app.councilSources.isEmpty)
            if app.draft.members.contains(where: { $0.routes.contains(where: { ["codex", "claude"].contains($0.providerId) }) }) && app.draft.pair.projectRoots.isEmpty {
                HStack {
                    Text("Для подписочного совета выберите проект в «Паре».").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("Выбрать проект") { app.tab = "Пара"; app.chooseProject() }
                }
            }
            DisclosureGroup("Итог совета") {
                VStack(alignment: .leading, spacing: 10) {
                    Toggle("Сводку делает выбранная модель", isOn: Binding(get: { app.draft.synthesis != nil }, set: { value in
                        app.draft.synthesis = value ? app.draft.members.first?.routes.first ?? app.defaultRoute(for: app.preferredSource) : nil
                    }))
                    if app.draft.synthesis != nil {
                        RouteEditor(route: Binding(get: { app.draft.synthesis ?? Route(providerId: "", model: "") }, set: { app.draft.synthesis = $0 }))
                    } else {
                        Text("Итог сформирует ваш агент в чате. Дополнительного API-вызова для сводки нет.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }.padding(.top, 8)
            }
            LimitsEditor()
        }
    }
}

struct ProviderEditor: View {
    @EnvironmentObject var app: PanelModel
    @Binding var provider: Provider
    var expanded: Bool
    var toggle: () -> Void
    @State private var showAdvanced = false
    @State private var confirmKeyDeletion = false
    @State private var confirmRemoval = false
    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 7) {
                HStack {
                    Text(provider.name.isEmpty ? "Без названия" : provider.name).font(.headline).lineLimit(1)
                    Spacer(minLength: 8)
                    Text(app.keyPresent[provider.id] == true ? "Ключ сохранён" : "Ключ не сохранён")
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    Button(expanded ? "Готово" : "Настроить", action: toggle).font(.caption)
                        .accessibilityLabel("\(expanded ? "Свернуть" : "Настроить") подключение \(provider.name)")
                }
                if let warning = app.catalogErrors[provider.id] {
                    Text("⚠ Каталог: \(warning)").font(.caption2).foregroundStyle(.orange)
                }
                if expanded {
                    Divider()
                    HStack {
                        TextField("Название", text: $provider.name).font(.headline)
                        Toggle("Включено", isOn: $provider.enabled).toggleStyle(.checkbox)
                        Button(role: .destructive) { confirmRemoval = true } label: { Image(systemName: "minus.circle") }
                            .buttonStyle(.borderless).help("Удалить подключение")
                    }
                    HStack {
                        Picker("Протокол", selection: $provider.protocol) {
                            Text("OpenAI-совместимый").tag("openai")
                            Text("Anthropic").tag("anthropic")
                            Text("Jev").tag("jev")
                        }
                        TextField("https://…/v1", text: $provider.baseUrl)
                            .accessibilityLabel("Базовый адрес API")
                    }
                    SecureField(app.keyPresent[provider.id] == true ? "Ключ сохранён · введите новый для замены" : "API-ключ", text: Binding(get: { app.pendingKeys[provider.id] ?? "" }, set: { app.pendingKeys[provider.id] = $0 }))
                        .accessibilityLabel("API-ключ для \(provider.name)")
                    HStack {
                        Label(app.keyPresent[provider.id] == true ? "В Keychain" : "Ключ не сохранён", systemImage: app.keyPresent[provider.id] == true ? "checkmark.shield" : "key")
                            .font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        if app.keyPresent[provider.id] == true {
                            Button("Удалить ключ", role: .destructive) { confirmKeyDeletion = true }.font(.caption)
                        }
                        if provider.protocol != "jev" {
                            Button(app.syncing.contains(provider.id) ? "Обновляем…" : "Обновить модели") {
                                Task { await app.syncModels(provider.id) }
                            }.disabled(app.syncing.contains(provider.id))
                        }
                    }
                    if provider.protocol != "jev" {
                        ForEach(app.catalogStatus(provider.id), id: \.self) { status in
                            Text(status).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    DisclosureGroup("Дополнительно", isExpanded: $showAdvanced) {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("ID моделей вручную — по одному на строку").font(.caption)
                            TextEditor(text: Binding(get: { provider.manualModels.joined(separator: "\n") }, set: { provider.manualModels = $0.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty } }))
                                .font(.system(.caption, design: .monospaced)).frame(height: 65)
                                .overlay(RoundedRectangle(cornerRadius: 5).stroke(Color.secondary.opacity(0.2)))
                            Toggle("Разрешить локальный API", isOn: $provider.allowLocal)
                            Text("Включайте только для своего доверенного локального сервера. Ключи и вопросы передаются выбранному адресу.")
                                .font(.caption).foregroundStyle(.secondary)
                        }.padding(.top, 8)
                    }
                }
            }.padding(6).frame(maxWidth: .infinity, alignment: .leading)
        }
        .task(id: app.catalogRequestIdentity(provider.id)) { app.ensureCatalogLoaded(provider.id) }
        .confirmationDialog("Удалить сохранённый ключ?", isPresented: $confirmKeyDeletion) {
            Button("Удалить из Keychain", role: .destructive) { Task { await app.deleteKey(provider.id) } }
        } message: { Text("Подключение останется, но API-запросы без ключа могут перестать работать.") }
        .confirmationDialog("Убрать подключение?", isPresented: $confirmRemoval) {
            Button("Убрать из настроек", role: .destructive) { app.removeProvider(provider.id) }
        } message: { Text("Сначала уберите его из совета. Сохранённый ключ удаляется отдельно кнопкой «Удалить ключ».") }
    }
}

struct ConnectionsPanel: View {
    @EnvironmentObject var app: PanelModel
    @State private var expandedProviderID: String? = nil
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Ключ вводится один раз и хранится в Keychain этого Mac. Никакого аккаунта Pair.")
                .foregroundStyle(.secondary)
            ForEach($app.draft.providers) { $provider in
                ProviderEditor(provider: $provider, expanded: expandedProviderID == provider.id, toggle: {
                    expandedProviderID = expandedProviderID == provider.id ? nil : provider.id
                })
            }
            Button("Добавить API", systemImage: "plus") {
                app.addProvider()
                expandedProviderID = app.draft.providers.last?.id
            }
            PanelSection("Подключить к обычному чату") {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Pair добавляет только свой MCP и skill. Основной провайдер вашего агента не меняется.")
                        .font(.caption).foregroundStyle(.secondary)
                    HStack {
                        Button("Подключить к Codex") { Task { await app.connectCodex() } }
                        Button("Подключить к Claude") { Task { await app.connectClaude() } }
                    }
                    Text("Подписочный вход — только в официальном Codex или Claude Code. При отсутствии CLI Pair подскажет, что установить.")
                        .font(.caption).foregroundStyle(.secondary)
                }.frame(maxWidth: .infinity, alignment: .leading).padding(6)
            }
        }
    }
}

struct AgentCard: View {
    var name: String
    var details: [String: Any]
    @State private var expanded = false
    private var available: Bool? { details["available"] as? Bool ?? details["installed"] as? Bool }
    private var loginLabel: String {
        if let explicit = details["authMode"] as? String ?? details["login"] as? String { return explicit }
        if details["loggedIn"] as? Bool == false { return "не выполнен" }
        if details["loggedIn"] as? Bool == true {
            switch details["billingMode"] as? String {
            case "subscription": return "подписка"
            case "api": return "API"
            default: return "выполнен · тип оплаты неизвестен"
            }
        }
        return "не проверен"
    }
    private var skillEntries: [[String: Any]] {
        if let items = details["skills"] as? [[String: Any]] { return items }
        return (details["skills"] as? [String: Any])?["skills"] as? [[String: Any]] ?? []
    }
    private var quotaRows: [(String, Double, Date?)] {
        var rows: [(String, Double, Date?)] = []
        func visit(_ value: Any, label: String, depth: Int) {
            guard depth < 6 else { return }
            if let array = value as? [Any] {
                for item in array { visit(item, label: label, depth: depth + 1) }
            } else if let object = value as? [String: Any] {
                let remaining = (object["remainingPercent"] as? NSNumber)?.doubleValue
                    ?? (object["usedPercent"] as? NSNumber).map { 100 - $0.doubleValue }
                if let remaining {
                    let duration = (object["windowDurationMins"] as? NSNumber)?.intValue
                    let title = object["label"] as? String
                        ?? (duration == 300 ? "5 часов" : duration == 10080 ? "Неделя" : label.isEmpty ? "Квота" : label)
                    let timestamp = (object["resetsAt"] as? NSNumber)?.doubleValue
                    let reset = timestamp.map { Date(timeIntervalSince1970: $0 > 100_000_000_000 ? $0 / 1000 : $0) }
                    rows.append((title, max(0, min(100, remaining)), reset))
                } else {
                    for key in object.keys.sorted() where ["windows", "primary", "secondary", "rateLimits", "rateLimitsByLimitId"].contains(key) || object[key] is [String: Any] {
                        if let child = object[key] { visit(child, label: ["primary", "secondary", "windows", "rateLimits", "rateLimitsByLimitId"].contains(key) ? label : key, depth: depth + 1) }
                    }
                }
            }
        }
        if let value = details["quotas"] { visit(value, label: "", depth: 0) }
        return rows
    }
    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 7) {
                HStack {
                    Text(name).font(.headline)
                    Spacer()
                    Text(available == true ? "Установлен" : available == false ? "Не найден" : "Статус неизвестен")
                        .foregroundStyle(available == true ? Color.green : Color.secondary)
                }
                Text("Вход: \(loginLabel) · \(details["version"] as? String ?? "версия неизвестна")")
                    .font(.caption).foregroundStyle(.secondary)
                DisclosureGroup("Лимиты и skills", isExpanded: $expanded) {
                    VStack(alignment: .leading, spacing: 6) {
                        if quotaRows.isEmpty {
                            Text("Квоты недоступны — это не нулевой остаток.").foregroundStyle(.secondary)
                        } else {
                            ForEach(Array(quotaRows.enumerated()), id: \.offset) { _, quota in
                                HStack {
                                    Text(quota.0)
                                    Spacer()
                                    Text("Осталось \(Int(quota.1.rounded()))%")
                                }
                                if let reset = quota.2 {
                                    Text("Сброс: \(reset.formatted(date: .abbreviated, time: .shortened))").foregroundStyle(.secondary)
                                }
                            }
                        }
                        Divider()
                        if skillEntries.isEmpty {
                            Text("Метаданные skills пока недоступны.").foregroundStyle(.secondary)
                        } else {
                            Text("Skills: \(skillEntries.count)").fontWeight(.medium)
                            ForEach(Array(skillEntries.enumerated()), id: \.offset) { _, skill in
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(skill["name"] as? String ?? "Без названия")
                                    if let description = skill["description"] as? String {
                                        Text(description).foregroundStyle(.secondary).lineLimit(3)
                                    }
                                }
                            }
                        }
                        if let observed = details["observedAt"] as? Double {
                            Text("Проверено: \(Date(timeIntervalSince1970: observed).formatted(date: .omitted, time: .shortened))").foregroundStyle(.secondary)
                        }
                    }.font(.caption).textSelection(.enabled).padding(.top, 6)
                }.font(.caption)
            }.padding(6)
        }
    }
}

struct PairPanel: View {
    @EnvironmentObject var app: PanelModel
    @State private var taskExpanded = false
    @State private var cliSettings = false
    private var resultText: String {
        guard let job = app.selectedJob else { return "" }
        if let result = job["result"] as? String { return result }
        if let result = job["result"] as? [String: Any] {
            if let text = result["text"] as? String { return text }
            if let data = try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]) { return String(data: data, encoding: .utf8) ?? "" }
        }
        return job["error"] as? String ?? "Результат пока не готов."
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Основная работа остаётся в вашем чате. Pair передаёт задачи официальным агентам и сохраняет результат.")
                .foregroundStyle(.secondary)
            AgentCard(name: "Codex", details: app.agents["codex"] as? [String: Any] ?? [:])
            AgentCard(name: "Claude Code", details: app.agents["claude"] as? [String: Any] ?? [:])
            PanelSection("Разрешённые проекты") {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(app.draft.pair.projectRoots, id: \.self) { root in
                        HStack {
                            Text(root).font(.caption).lineLimit(2).textSelection(.enabled)
                            Spacer()
                            Button { app.draft.pair.projectRoots.removeAll { $0 == root } } label: { Image(systemName: "minus.circle") }.buttonStyle(.borderless)
                                .help("Убрать разрешение на эту папку для следующих задач")
                        }
                    }
                    Button("Разрешить папку…", systemImage: "folder.badge.plus") { app.chooseProject() }
                    Text("Агенты работают только в выбранных папках; штатные ограничения CLI сохраняются.")
                        .font(.caption).foregroundStyle(.secondary)
                }.frame(maxWidth: .infinity, alignment: .leading).padding(6)
            }
            DisclosureGroup("Модели официальных агентов", isExpanded: $cliSettings) {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Text("Codex").frame(width: 70, alignment: .leading)
                        TextField("По умолчанию CLI", text: Binding(get: { app.draft.pair.codex.model ?? "" }, set: { app.draft.pair.codex.model = $0.isEmpty ? nil : $0 }))
                        effortPicker(Binding(get: { app.draft.pair.codex.effort ?? "" }, set: { app.draft.pair.codex.effort = $0.isEmpty ? nil : $0 }))
                    }
                    HStack {
                        Text("Claude").frame(width: 70, alignment: .leading)
                        TextField("По умолчанию CLI", text: Binding(get: { app.draft.pair.claude.model ?? "" }, set: { app.draft.pair.claude.model = $0.isEmpty ? nil : $0 }))
                        effortPicker(Binding(get: { app.draft.pair.claude.effort ?? "" }, set: { app.draft.pair.claude.effort = $0.isEmpty ? nil : $0 }))
                    }
                    Text("Используйте IDs, доступные вашему официальному CLI. Недоступная модель даст ошибку, не скрытую замену.")
                        .font(.caption).foregroundStyle(.secondary)
                }.padding(.top, 8)
            }
            DisclosureGroup("Передать задачу из панели", isExpanded: $taskExpanded) {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Picker("Агент", selection: $app.selectedAgent) { Text("Codex").tag("codex"); Text("Claude").tag("claude") }
                        Picker("Проект", selection: $app.selectedProject) {
                            Text("Выберите папку").tag("")
                            ForEach(app.draft.pair.projectRoots, id: \.self) { Text(URL(fileURLWithPath: $0).lastPathComponent).tag($0) }
                        }
                    }
                    TextEditor(text: $app.prompt).frame(height: 85)
                        .overlay(RoundedRectangle(cornerRadius: 5).stroke(Color.secondary.opacity(0.2)))
                        .accessibilityLabel("Задача агенту")
                    Button("Передать задачу") { Task { await app.startJob() } }
                        .disabled(app.prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || app.selectedProject.isEmpty || app.busy)
                }.padding(.top, 8)
            }
            if !app.jobs.isEmpty {
                PanelSection("Последние задачи") {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(Array(app.jobs.prefix(8).enumerated()), id: \.offset) { _, job in
                            HStack {
                                Text(job["kind"] as? String ?? "Задача")
                                Text(job["status"] as? String ?? "неизвестно").foregroundStyle(.secondary)
                                Spacer()
                                if let id = job["id"] as? String {
                                    Button("Результат") { Task { await app.inspectJob(id) } }.font(.caption)
                                    if ["queued", "running", "starting"].contains(job["status"] as? String ?? "") {
                                        Button("Остановить") { Task { await app.cancelJob(id) } }.font(.caption)
                                    }
                                }
                            }.font(.caption)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(6)
                }
            }
            if app.selectedJob != nil {
                PanelSection("Полный результат") {
                    ScrollView { Text(resultText).font(.system(.caption, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                        .frame(minHeight: 70, maxHeight: 200)
                }
            }
        }
    }
    private func effortPicker(_ binding: Binding<String>) -> some View {
        Picker("Reasoning", selection: binding) {
            Text("По умолчанию").tag("")
            ForEach(["low", "medium", "high", "xhigh", "max"], id: \.self) { Text($0).tag($0) }
        }.frame(width: 150)
    }
}

struct PanelView: View {
    @EnvironmentObject var app: PanelModel
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Text("Pair").font(.headline)
                Spacer()
                Circle().fill(app.ready ? Color.green : Color.orange).frame(width: 7, height: 7)
                Text(app.ready ? "Локально" : app.baseURL == nil ? "Запуск" : "Переподключение")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(.horizontal, 18).padding(.vertical, 10)
            Picker("", selection: $app.tab) {
                Text("Совет").tag("Совет")
                Text("Пара").tag("Пара")
                Text("Подключения").tag("Подключения")
            }.pickerStyle(.segmented).labelsHidden().accessibilityLabel("Навигация Pair")
                .padding(.horizontal, 18).padding(.bottom, 10)
            Divider()
            if let error = app.error {
                HStack(alignment: .top) {
                    Image(systemName: "exclamationmark.triangle")
                    Text(error).font(.caption).textSelection(.enabled)
                    Spacer()
                    Button { app.error = nil } label: { Image(systemName: "xmark") }.buttonStyle(.borderless)
                }.foregroundStyle(Color.red).padding(12).background(Color.red.opacity(0.06))
            }
            if !app.mergeConflicts.isEmpty {
                ScrollView {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Настройки изменились в другом окне. Черновик сохранён; выберите вариант только для пересекающихся полей.")
                            .font(.caption)
                        ForEach(app.mergeConflicts) { conflict in
                            VStack(alignment: .leading, spacing: 3) {
                                HStack {
                                    Text(conflict.label).font(.caption).frame(maxWidth: .infinity, alignment: .leading)
                                    Picker("Вариант", selection: Binding(
                                        get: { app.conflictChoices[conflict.id] ?? "" },
                                        set: { app.conflictChoices[conflict.id] = $0 }
                                    )) {
                                        Text("Выберите…").tag("")
                                        Text("Актуальный").tag("current")
                                        Text("Мой черновик").tag("mine")
                                    }.frame(width: 170)
                                }
                                Text("Мой: \(conflict.mine) · Актуальный: \(conflict.current)")
                                    .font(.caption2).foregroundStyle(.secondary).textSelection(.enabled)
                            }
                        }
                    }
                }.frame(maxHeight: 190).padding(12).background(Color.orange.opacity(0.09))
            }
            ScrollView {
                Group {
                    switch app.tab {
                    case "Пара": PairPanel()
                    case "Подключения": ConnectionsPanel()
                    default: CouncilPanel()
                    }
                }.padding(14).frame(maxWidth: .infinity, alignment: .leading)
            }.disabled(!app.ready || app.busy)
            Divider()
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(app.invalidFields.keys.sorted().compactMap { app.invalidFields[$0] }.first ?? (app.dirty ? "Есть несохранённые изменения" : app.message)).font(.caption).lineLimit(2)
                    Text("Работающие задачи не меняются.").font(.caption2).foregroundStyle(.secondary)
                }
                Spacer()
                if app.busy { ProgressView().controlSize(.small) }
                Button("Сохранить") {
                    NSApp.keyWindow?.makeFirstResponder(nil)
                    Task { await app.save() }
                }
                    .buttonStyle(.borderedProminent).keyboardShortcut("s", modifiers: .command)
                    .disabled(!app.ready || !app.dirty || app.busy || !app.invalidFields.isEmpty)
            }.padding(14)
        }
        .frame(minWidth: 580, idealWidth: 620, maxWidth: 850, minHeight: 440, idealHeight: 500)
    }
}

@MainActor
final class PairWindow: NSWindow {
    let rosterUndo = UndoManager()
    override var undoManager: UndoManager? { rosterUndo }
    @objc func undo(_ sender: Any?) { rosterUndo.undo() }
    @objc func redo(_ sender: Any?) { rosterUndo.redo() }
    override func validateMenuItem(_ item: NSMenuItem) -> Bool {
        switch item.action {
        case #selector(undo(_:)): return rosterUndo.canUndo
        case #selector(redo(_:)): return rosterUndo.canRedo
        default: return super.validateMenuItem(item)
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuItemValidation {
    let model = PanelModel()
    var item: NSStatusItem!
    var window: NSWindow!
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = NSImage(systemSymbolName: "person.2.fill", accessibilityDescription: "Открыть Pair")
        item.button?.target = self
        item.button?.action = #selector(togglePanel)
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        window = PairWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 500), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        model.rosterUndoManager = window.undoManager
        window.title = "Pair"
        window.isReleasedWhenClosed = false
        window.collectionBehavior.insert(.moveToActiveSpace)
        window.contentView = NSHostingView(rootView: PanelView().environmentObject(model))
        window.center()
        installMainMenu()
        model.start()
        showPanel()
    }
    @objc func togglePanel() {
        if NSApp.currentEvent?.type == .rightMouseUp {
            let menu = NSMenu()
            menu.addItem(withTitle: "Открыть Pair", action: #selector(showPanel), keyEquivalent: "")
            menu.addItem(NSMenuItem.separator())
            menu.addItem(withTitle: "Завершить Pair", action: #selector(quit), keyEquivalent: "q")
            for entry in menu.items { entry.target = self }
            item.menu = menu
            item.button?.performClick(nil)
            item.menu = nil
        } else if window.isVisible && window.isKeyWindow && !window.isMiniaturized { window.orderOut(nil) }
        else { showPanel() }
    }
    @objc func showPanel() {
        if window.isMiniaturized { window.deminiaturize(nil) }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }
    private func installMainMenu() {
        let main = NSMenu()
        func group(_ title: String) -> NSMenu {
            let entry = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            let menu = NSMenu(title: title)
            entry.submenu = menu
            main.addItem(entry)
            return menu
        }
        func action(_ menu: NSMenu, _ title: String, _ selector: Selector, key: String = "", modifiers: NSEvent.ModifierFlags = .command) {
            let entry = NSMenuItem(title: title, action: selector, keyEquivalent: key)
            entry.keyEquivalentModifierMask = modifiers
            entry.target = self
            menu.addItem(entry)
        }
        let appMenu = group("Pair")
        action(appMenu, "Открыть Pair", #selector(showPanel))
        appMenu.addItem(NSMenuItem.separator())
        action(appMenu, "Завершить Pair", #selector(quit), key: "q")

        let file = group("Файл")
        action(file, "Сохранить настройки", #selector(saveSettings), key: "s")
        action(file, "Закрыть окно", #selector(closePanel), key: "w")

        let edit = group("Правка")
        for (title, selector, key, modifiers) in [
            ("Отменить", #selector(PairWindow.undo(_:)), "z", NSEvent.ModifierFlags.command),
            ("Повторить", #selector(PairWindow.redo(_:)), "z", [.command, .shift]),
            ("Вырезать", #selector(NSText.cut(_:)), "x", .command),
            ("Копировать", #selector(NSText.copy(_:)), "c", .command),
            ("Вставить", #selector(NSText.paste(_:)), "v", .command),
            ("Выделить всё", #selector(NSText.selectAll(_:)), "a", .command),
        ] {
            let entry = NSMenuItem(title: title, action: selector, keyEquivalent: key)
            entry.keyEquivalentModifierMask = modifiers
            // Nil target preserves the native responder-chain text actions.
            edit.addItem(entry)
        }
        let view = group("Вид")
        action(view, "Совет", #selector(selectCouncil), key: "1")
        action(view, "Пара", #selector(selectPair), key: "2")
        action(view, "Подключения", #selector(selectConnections), key: "3")

        let council = group("Совет")
        action(council, "Добавить участника", #selector(addCouncilMember), key: "n", modifiers: [.command, .shift])
        action(council, "Убрать последнего участника", #selector(removeLastCouncilMember))
        NSApp.mainMenu = main
    }
    @objc func saveSettings() {
        guard model.ready, !model.busy else { return }
        window.makeFirstResponder(nil)
        Task {
            await Task.yield()
            guard model.dirty, model.invalidFields.isEmpty else { return }
            await model.save()
        }
    }
    @objc func closePanel() { window.performClose(nil) }
    @objc func selectCouncil() { model.tab = "Совет"; showPanel() }
    @objc func selectPair() { model.tab = "Пара"; showPanel() }
    @objc func selectConnections() { model.tab = "Подключения"; showPanel() }
    @objc func addCouncilMember() {
        guard model.ready, !model.busy, !model.councilSources.isEmpty else { return }
        window.makeFirstResponder(nil)
        model.tab = "Совет"
        model.addMember()
        showPanel()
    }
    @objc func removeLastCouncilMember() {
        guard model.ready, !model.busy, let member = model.draft.members.last else { return }
        window.makeFirstResponder(nil)
        model.removeMember(member.id)
    }
    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        switch menuItem.action {
        case #selector(saveSettings):
            // A field editor may still hold an uncommitted edit. Allow Cmd-S
            // to end that edit first; the action rechecks dirty/validation.
            return model.ready && !model.busy && (model.dirty || window.firstResponder is NSTextView)
        case #selector(addCouncilMember): return model.ready && !model.busy && !model.councilSources.isEmpty
        case #selector(removeLastCouncilMember):
            if let member = model.draft.members.last { menuItem.title = "Убрать «\(member.label)»" }
            else { menuItem.title = "Убрать последнего участника" }
            return model.ready && !model.busy && !model.draft.members.isEmpty
        case #selector(closePanel): return window.isVisible
        case #selector(selectCouncil): menuItem.state = model.tab == "Совет" ? .on : .off
        case #selector(selectPair): menuItem.state = model.tab == "Пара" ? .on : .off
        case #selector(selectConnections): menuItem.state = model.tab == "Подключения" ? .on : .off
        default: break
        }
        return true
    }
    @objc func quit() { NSApp.terminate(nil) }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        window.makeFirstResponder(nil)
        guard !model.busy else {
            model.error = "Дождитесь окончания сохранения настроек. Работающие задачи продолжаются."
            showPanel()
            return .terminateCancel
        }
        guard model.dirty else { return .terminateNow }
        showPanel()
        let alert = NSAlert()
        alert.messageText = "Сохранить изменения Pair?"
        alert.informativeText = "Настройки изменены, но не сохранены. Работающие задания продолжатся независимо от закрытия панели."
        alert.addButton(withTitle: "Сохранить")
        alert.addButton(withTitle: "Не сохранять")
        alert.addButton(withTitle: "Отмена")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self else { NSApp.reply(toApplicationShouldTerminate: false); return }
            if response == .alertFirstButtonReturn {
                Task {
                    await self.model.save()
                    NSApp.reply(toApplicationShouldTerminate: !self.model.dirty && self.model.error == nil)
                }
            } else {
                NSApp.reply(toApplicationShouldTerminate: response == .alertSecondButtonReturn)
            }
        }
        return .terminateLater
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showPanel(); return true }
    func applicationWillTerminate(_ notification: Notification) { model.stop() }
}

@main
struct PairApplication {
    @MainActor static func main() {
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        // AppKit delegate and status-button target do not own this local delegate.
        // Retain it for the entire optimized event loop, including menu-bar clicks.
        withExtendedLifetime(delegate) { application.run() }
    }
}
