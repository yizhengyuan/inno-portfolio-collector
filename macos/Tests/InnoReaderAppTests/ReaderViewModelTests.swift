import Foundation
import Testing
import InnoAppCore
@testable import InnoReaderFeature

private actor ReaderRecordingHelper: HelperCalling {
    struct Call: Equatable, Sendable {
        let command: String
        let arguments: [String: JSONValue]
    }

    private var calls: [Call] = []
    private let responses: [String: [String: JSONValue]]
    private let failureCommand: String?

    init(
        responses: [String: [String: JSONValue]] = [:],
        failureCommand: String? = nil
    ) {
        self.responses = responses
        self.failureCommand = failureCommand
    }

    func call(command: String, arguments: [String: JSONValue]) async throws -> [String: JSONValue] {
        calls.append(Call(command: command, arguments: arguments))
        if command == failureCommand { throw HelperClientError.helperFailure("更新包版本不匹配") }
        if command == "create_draft", responses[command] == nil,
           case .string(let id) = arguments["draft_id"],
           case .string(let vault) = arguments["vault"] {
            return [
                "draft_id": .string(id),
                "draft_path": .string(
                    URL(fileURLWithPath: vault).appendingPathComponent("10-编辑稿/\(id).md").path
                ),
            ]
        }
        return responses[command] ?? [:]
    }

    func recordedCalls() -> [Call] { calls }
}

@Suite("Reader view model")
@MainActor
struct ReaderViewModelTests {
    private func locations() throws -> AppLocations {
        let support = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        return try AppLocations.resolve(
            role: .reader,
            applicationSupport: support,
            bundleURL: URL(fileURLWithPath: "/Applications/Reader.app", isDirectory: true)
        )
    }

    private let package = URL(fileURLWithPath: "/tmp/news.inno-update")

    private func previewResponse(kind: String = "baseline") -> [String: JSONValue] {
        [
            "kind": .string(kind),
            "base_version": kind == "baseline" ? .null : .string("sha256:" + String(repeating: "1", count: 64)),
            "target_version": .string("sha256:" + String(repeating: "2", count: 64)),
            "included": .array([.string("03-文章/项目/new.md")]),
            "deleted": .array([.string("03-文章/项目/old.md")]),
        ]
    }

    @Test("baseline preview requires explicit apply and refreshes only after success")
    func baselineImport() async throws {
        let paths = try locations()
        let helper = ReaderRecordingHelper(responses: [
            "preview_update": previewResponse(),
            "apply_update": ["target_version": .string("sha256:" + String(repeating: "2", count: 64))],
        ])
        let model = ReaderViewModel(helper: helper, locations: paths)

        await model.previewUpdate(package: package)
        #expect(model.updatePreview?.kind == .baseline)
        #expect(await helper.recordedCalls().map(\.command) == ["preview_update"])

        await model.applyPreviewedUpdate()
        #expect(model.updatePreview == nil)
        let calls = await helper.recordedCalls()
        #expect(calls.map(\.command) == ["preview_update", "apply_update"])
        #expect(calls[1].arguments["vault"] == .string(paths.vault.path))
    }

    @Test("incremental preview exposes a human-readable diff")
    func incrementalPreview() async throws {
        let helper = ReaderRecordingHelper(responses: ["preview_update": previewResponse(kind: "incremental")])
        let model = ReaderViewModel(helper: helper, locations: try locations())

        await model.previewUpdate(package: package)

        #expect(model.updatePreview?.kind == .incremental)
        #expect(model.updatePreview?.includedCount == 1)
        #expect(model.updatePreview?.deletedCount == 1)
        #expect(model.updatePreview?.baseVersion != nil)
    }

    @Test("version mismatch keeps the successful preview for retry")
    func versionMismatch() async throws {
        let helper = ReaderRecordingHelper(
            responses: ["preview_update": previewResponse(kind: "incremental")],
            failureCommand: "apply_update"
        )
        let model = ReaderViewModel(helper: helper, locations: try locations())
        await model.previewUpdate(package: package)

        await model.applyPreviewedUpdate()

        #expect(model.errorMessage == "更新包版本不匹配")
        #expect(model.updatePreview != nil)
    }

    @Test("reader flows never request collector commands")
    func roleIsolation() async throws {
        let helper = ReaderRecordingHelper(responses: [
            "status": ["role": .string("reader"), "vault_exists": .boolean(false)],
            "preview_update": previewResponse(),
            "apply_update": [:],
            "rebuild_dashboard": ["dashboard_path": .string("80-离线看板/index.html")],
        ])
        let model = ReaderViewModel(helper: helper, locations: try locations())

        await model.refresh()
        await model.previewUpdate(package: package)
        await model.applyPreviewedUpdate()
        await model.rebuildDashboard()

        let commands = await helper.recordedCalls().map(\.command)
        #expect(commands == ["status", "preview_update", "apply_update", "rebuild_dashboard"])
        #expect(!commands.contains("collect"))
    }

    @Test("draft creation and export stay in the editable zone")
    func draftBoundaries() async throws {
        let paths = try locations()
        let helper = ReaderRecordingHelper(responses: ["build_drafts": ["draft_count": .integer(1)]])
        let model = ReaderViewModel(
            helper: helper,
            locations: paths,
            author: "朋友甲",
            now: { Date(timeIntervalSince1970: 1_788_163_200) },
            makeDraftID: { "reader-draft-0001" }
        )
        let article = LibraryArticle(
            id: "sha256:" + String(repeating: "a", count: 64),
            title: "项目新进展",
            project: "项目甲",
            account: "甲公众号",
            published: "2026-07-11",
            sourceURL: "https://mp.weixin.qq.com/s/example",
            relativePath: "03-文章/项目甲/source-aaaaaaaa.md"
        )

        await model.createDraft(from: article, kind: .summary)
        #expect(model.drafts.map(\.id) == ["reader-draft-0001"])
        await model.exportDrafts(
            ids: ["reader-draft-0001"],
            destination: URL(fileURLWithPath: "/tmp/friend.inno-drafts")
        )

        let calls = await helper.recordedCalls()
        #expect(calls.map(\.command) == ["create_draft", "build_drafts"])
        #expect(calls[0].arguments["kind"] == .string("summary"))
        #expect(calls[0].arguments["source_ids"] == .array([.string(article.id)]))
        #expect(calls[0].arguments["vault"] == .string(paths.vault.path))
        #expect(calls[0].arguments["path"] == nil)
        #expect(calls[1].arguments["draft_paths"] == .array([.string("10-编辑稿/reader-draft-0001.md")]))
    }

    @Test("draft export rejects destinations inside source and attachment zones")
    func exportRejectsProtectedDestination() async throws {
        let paths = try locations()
        let helper = ReaderRecordingHelper()
        let model = ReaderViewModel(
            helper: helper,
            locations: paths,
            author: "朋友甲",
            makeDraftID: { "reader-draft-0002" }
        )
        let article = LibraryArticle(
            id: "sha256:" + String(repeating: "b", count: 64),
            title: "文章", project: "项目", account: "公众号", published: "2026-07-11",
            sourceURL: "", relativePath: "03-文章/项目/source-bbbbbbbb.md"
        )
        await model.createDraft(from: article, kind: .note)
        let before = await helper.recordedCalls().count

        await model.exportDrafts(
            ids: ["reader-draft-0002"],
            destination: paths.vault.appendingPathComponent("03-文章/forbidden.inno-drafts")
        )
        #expect(model.errorMessage == "不能把编辑稿包写入只读原文或附件目录。")
        await model.exportDrafts(
            ids: ["reader-draft-0002"],
            destination: paths.vault.appendingPathComponent("04-附件/forbidden.inno-drafts")
        )

        #expect(await helper.recordedCalls().count == before)
    }
}
