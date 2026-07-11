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

public enum DraftKind: String, CaseIterable, Equatable, Sendable {
    case note
    case summary
    case pitch
    case edit

    public var title: String {
        switch self {
        case .note: "笔记"
        case .summary: "摘要"
        case .pitch: "选题"
        case .edit: "编辑稿"
        }
    }
}

public struct ReaderDraft: Equatable, Identifiable, Sendable {
    public let id: String
    public let title: String
    public let kind: DraftKind
    public let relativePath: String
}

@MainActor
public final class ReaderViewModel: ObservableObject {
    @Published public private(set) var articles: [LibraryArticle] = []
    @Published public private(set) var updatePreview: UpdatePreview?
    @Published public private(set) var drafts: [ReaderDraft] = []
    @Published public private(set) var isBusy = false
    @Published public private(set) var errorMessage: String?

    public let locations: AppLocations
    private let helper: any HelperCalling
    private var selectedPackage: URL?
    private var index: LibraryIndex?
    private let author: String
    private let now: () -> Date
    private let makeDraftID: () -> String

    public init(
        helper: any HelperCalling,
        locations: AppLocations,
        author: String = "本地编辑者",
        now: @escaping () -> Date = Date.init,
        makeDraftID: @escaping () -> String = { UUID().uuidString.lowercased() }
    ) {
        self.helper = helper
        self.locations = locations
        let trimmedAuthor = author.trimmingCharacters(in: .whitespacesAndNewlines)
        self.author = trimmedAuthor.isEmpty ? "本地编辑者" : trimmedAuthor
        self.now = now
        self.makeDraftID = makeDraftID
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

    public func createDraft(from article: LibraryArticle, kind: DraftKind) async {
        await perform {
            let draftID = makeDraftID()
            let timestamp = Self.timestamp(now())
            let result = try await helper.call(
                command: "create_draft",
                arguments: [
                    "vault": .string(locations.vault.path),
                    "draft_id": .string(draftID),
                    "draft_version": .integer(1),
                    "author": .string(author),
                    "title": .string(article.title),
                    "updated_at": .string(timestamp),
                    "source_ids": .array([.string(article.id)]),
                    "kind": .string(kind.rawValue),
                    "body": .string(Self.draftBody(article: article, kind: kind)),
                ]
            )
            guard
                result["draft_id"] == .string(draftID),
                case .string(let rawPath) = result["draft_path"],
                let relative = safeDraftRelativePath(rawPath)
            else { throw HelperClientError.invalidResponse }
            drafts.append(ReaderDraft(
                id: draftID,
                title: article.title,
                kind: kind,
                relativePath: relative
            ))
        }
    }

    public func exportDrafts(ids: [String], destination: URL) async {
        guard !isProtectedDestination(destination) else {
            errorMessage = "不能把编辑稿包写入只读原文或附件目录。"
            return
        }
        let selected = ids.compactMap { id in drafts.first(where: { $0.id == id }) }
        guard !ids.isEmpty, selected.count == ids.count, Set(ids).count == ids.count else {
            errorMessage = "请选择有效且不重复的编辑稿。"
            return
        }
        await perform {
            _ = try await helper.call(
                command: "build_drafts",
                arguments: [
                    "vault": .string(locations.vault.path),
                    "draft_paths": .array(selected.map { .string($0.relativePath) }),
                    "output": .string(destination.path),
                    "exported_at": .string(Self.timestamp(now())),
                ]
            )
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

    private func safeDraftRelativePath(_ rawPath: String) -> String? {
        let root = locations.vault.resolvingSymlinksInPath().standardizedFileURL
        let draftsRoot = root.appendingPathComponent("10-编辑稿", isDirectory: true)
        let candidate = URL(fileURLWithPath: rawPath).resolvingSymlinksInPath().standardizedFileURL
        guard
            candidate.path.hasPrefix(draftsRoot.path + "/"),
            candidate.pathExtension.lowercased() == "md"
        else { return nil }
        let nested = String(candidate.path.dropFirst(draftsRoot.path.count + 1))
        guard nested.split(separator: "/").first != "附件" else { return nil }
        return "10-编辑稿/" + nested
    }

    private func isProtectedDestination(_ destination: URL) -> Bool {
        let root = locations.vault.resolvingSymlinksInPath().standardizedFileURL
        let logicalRoot = locations.vault.standardizedFileURL
        let logical = destination.standardizedFileURL
        let parent = destination.deletingLastPathComponent().resolvingSymlinksInPath()
        let resolved = parent.appendingPathComponent(destination.lastPathComponent).standardizedFileURL
        return ["03-文章", "04-附件"].contains { zone in
            let protected = root.appendingPathComponent(zone, isDirectory: true).path
            let logicalProtected = logicalRoot.appendingPathComponent(zone, isDirectory: true).path
            return resolved.path == protected
                || resolved.path.hasPrefix(protected + "/")
                || logical.path == logicalProtected
                || logical.path.hasPrefix(logicalProtected + "/")
        }
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

    private static func timestamp(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: date)
    }

    private static func draftBody(article: LibraryArticle, kind: DraftKind) -> String {
        """
        # \(kind.title)：\(article.title)

        - 项目：\(article.project)
        - 公众号：\(article.account)
        - 发布日期：\(article.published)
        - 来源：\(article.sourceURL)

        在这里开始编辑。
        """
    }
}
