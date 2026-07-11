import Combine
import Foundation
import InnoAppCore

public enum UpdateKind: String, Equatable, Sendable {
    case baseline
    case incremental
}

public struct UpdatePreview: Equatable, Sendable {
    public let kind: UpdateKind
    public let baseVersion: String?
    public let targetVersion: String
    public let included: [String]
    public let deleted: [String]

    public var includedCount: Int { included.count }
    public var deletedCount: Int { deleted.count }
}

@MainActor
public final class ReaderViewModel: ObservableObject {
    @Published public private(set) var articles: [LibraryArticle] = []
    @Published public private(set) var updatePreview: UpdatePreview?
    @Published public private(set) var isBusy = false
    @Published public private(set) var errorMessage: String?

    public let locations: AppLocations
    private let helper: any HelperCalling
    private var selectedPackage: URL?
    private var index: LibraryIndex?

    public init(helper: any HelperCalling, locations: AppLocations) {
        self.helper = helper
        self.locations = locations
    }

    public func refresh() async {
        await perform {
            let result = try await helper.call(
                command: "status",
                arguments: ["vault": .string(locations.vault.path)]
            )
            if result["vault_exists"] == .boolean(false) {
                index = nil
                articles = []
            } else {
                try loadLibrary()
            }
        }
    }

    public func previewUpdate(package: URL) async {
        updatePreview = nil
        selectedPackage = nil
        await perform {
            let result = try await helper.call(
                command: "preview_update",
                arguments: ["package": .string(package.path)]
            )
            updatePreview = try Self.preview(from: result)
            selectedPackage = package
        }
    }

    public func applyPreviewedUpdate() async {
        guard updatePreview != nil, let selectedPackage else {
            errorMessage = "请先选择并预览更新包。"
            return
        }
        await perform {
            _ = try await helper.call(
                command: "apply_update",
                arguments: [
                    "package": .string(selectedPackage.path),
                    "vault": .string(locations.vault.path),
                ]
            )
            updatePreview = nil
            self.selectedPackage = nil
            if FileManager.default.fileExists(atPath: locations.vault.path) {
                try loadLibrary()
            } else {
                index = nil
                articles = []
            }
        }
    }

    public func rebuildDashboard() async {
        await perform {
            _ = try await helper.call(
                command: "rebuild_dashboard",
                arguments: ["vault": .string(locations.vault.path)]
            )
            if index == nil { try loadLibrary() }
        }
    }

    public func filteredArticles(query: String, project: String?) -> [LibraryArticle] {
        index?.search(query, project: project) ?? []
    }

    public func articleURL(for article: LibraryArticle) throws -> URL {
        guard let index else { throw LibraryIndexError.unreadableManifest }
        return try index.url(for: article)
    }

    public func dashboardURL() throws -> URL {
        guard let index else { throw LibraryIndexError.unreadableManifest }
        return try index.dashboardURL()
    }

    private func loadLibrary() throws {
        let loaded = try LibraryIndex.load(vault: locations.vault)
        index = loaded
        articles = loaded.articles
    }

    private func perform(_ operation: () async throws -> Void) async {
        guard !isBusy else { return }
        isBusy = true
        errorMessage = nil
        defer { isBusy = false }
        do {
            try await operation()
        } catch is CancellationError {
            errorMessage = nil
        } catch let error as HelperClientError {
            switch error {
            case .helperFailure(let message): errorMessage = message
            case .timedOut: errorMessage = "本地助手响应超时。"
            case .outputTooLarge: errorMessage = "本地助手输出超过安全上限。"
            case .launchFailed: errorMessage = "本地助手不可用。"
            case .invalidResponse, .responseIDMismatch: errorMessage = "本地助手返回了无效响应。"
            }
        } catch {
            errorMessage = "本地资料库不可用。"
        }
    }

    private static func preview(from values: [String: JSONValue]) throws -> UpdatePreview {
        guard
            case .string(let rawKind) = values["kind"],
            let kind = UpdateKind(rawValue: rawKind),
            case .string(let targetVersion) = values["target_version"],
            case .array(let rawIncluded) = values["included"],
            case .array(let rawDeleted) = values["deleted"]
        else { throw HelperClientError.invalidResponse }
        func strings(_ values: [JSONValue]) throws -> [String] {
            try values.map {
                guard case .string(let value) = $0 else { throw HelperClientError.invalidResponse }
                return value
            }
        }
        let baseVersion: String?
        switch values["base_version"] {
        case .string(let value): baseVersion = value
        case .null: baseVersion = nil
        default: throw HelperClientError.invalidResponse
        }
        if kind == .baseline && baseVersion != nil { throw HelperClientError.invalidResponse }
        if kind == .incremental && baseVersion == nil { throw HelperClientError.invalidResponse }
        return UpdatePreview(
            kind: kind,
            baseVersion: baseVersion,
            targetVersion: targetVersion,
            included: try strings(rawIncluded),
            deleted: try strings(rawDeleted)
        )
    }
}
