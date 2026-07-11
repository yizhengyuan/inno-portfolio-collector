import Foundation
import Testing
@testable import InnoReaderFeature

@Suite("Reader library index")
struct LibraryIndexTests {
    private func makeVault(articles: [String: [String: Any]]) throws -> URL {
        let vault = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let system = vault.appendingPathComponent("90-系统", isDirectory: true)
        try FileManager.default.createDirectory(at: system, withIntermediateDirectories: true)
        let data = try JSONSerialization.data(
            withJSONObject: ["version": 1, "articles": articles],
            options: [.sortedKeys]
        )
        try data.write(to: system.appendingPathComponent("manifest.json"))
        return vault
    }

    private func record(
        id: String,
        title: String,
        project: String,
        account: String,
        published: String,
        path: String
    ) -> [String: Any] {
        [
            "key": id, "title": title, "project": project, "account": account,
            "published": published, "path": path,
        ]
    }

    @Test("loads safe records in stable newest-first order")
    func loadAndOrder() throws {
        let older = "sha256:" + String(repeating: "a", count: 64)
        let newer = "sha256:" + String(repeating: "b", count: 64)
        let vault = try makeVault(articles: [
            older: record(
                id: older, title: "旧文章", project: "项目甲", account: "甲号",
                published: "2026-01-01", path: "03-文章/项目甲/2026-01-01-旧文章-aaaaaaaa.md"
            ),
            newer: record(
                id: newer, title: "新文章", project: "项目乙", account: "乙号",
                published: "2026-07-11", path: "03-文章/项目乙/2026-07-11-新文章-bbbbbbbb.md"
            ),
        ])

        let index = try LibraryIndex.load(vault: vault)

        #expect(index.articles.map(\.id) == [newer, older])
    }

    @Test("search is case and width insensitive across title project and account")
    func search() throws {
        let id = "sha256:" + String(repeating: "c", count: 64)
        let vault = try makeVault(articles: [
            id: record(
                id: id, title: "ＡＩ 机器人", project: "Project Alpha", account: "INNO观察",
                published: "2026-07-11", path: "03-文章/Alpha/2026-07-11-ai-cccccccc.md"
            ),
        ])
        let index = try LibraryIndex.load(vault: vault)

        #expect(index.search("ai").map(\.id) == [id])
        #expect(index.search("project alpha").map(\.id) == [id])
        #expect(index.search("inno").map(\.id) == [id])
        #expect(index.search("missing").isEmpty)
    }

    @Test("rejects unstable IDs and unsafe article paths")
    func rejectsUnsafeRecords() throws {
        let stable = "sha256:" + String(repeating: "d", count: 64)
        let unstableVault = try makeVault(articles: [
            "not-stable": record(
                id: "not-stable", title: "文章", project: "项目", account: "公众号",
                published: "2026-07-11", path: "03-文章/项目/a.md"
            ),
        ])
        #expect(throws: LibraryIndexError.self) { try LibraryIndex.load(vault: unstableVault) }

        let unicodeDigitID = "sha256:" + String(repeating: "０", count: 64)
        let unicodeDigitVault = try makeVault(articles: [
            unicodeDigitID: record(
                id: unicodeDigitID, title: "文章", project: "项目", account: "公众号",
                published: "2026-07-11", path: "03-文章/项目/a.md"
            ),
        ])
        #expect(throws: LibraryIndexError.self) { try LibraryIndex.load(vault: unicodeDigitVault) }

        let unsafeVault = try makeVault(articles: [
            stable: record(
                id: stable, title: "文章", project: "项目", account: "公众号",
                published: "2026-07-11", path: "03-文章/../escaped.md"
            ),
        ])
        #expect(throws: LibraryIndexError.self) { try LibraryIndex.load(vault: unsafeVault) }
    }

    @Test("article and dashboard URLs remain inside the Vault")
    func localURLs() throws {
        let id = "sha256:" + String(repeating: "e", count: 64)
        let relative = "03-文章/项目/2026-07-11-文章-eeeeeeee.md"
        let vault = try makeVault(articles: [
            id: record(
                id: id, title: "文章", project: "项目", account: "公众号",
                published: "2026-07-11", path: relative
            ),
        ])
        let articleURL = vault.appendingPathComponent(relative)
        try FileManager.default.createDirectory(
            at: articleURL.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        try Data().write(to: articleURL)
        let dashboard = vault.appendingPathComponent("80-离线看板/index.html")
        try FileManager.default.createDirectory(
            at: dashboard.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        try Data().write(to: dashboard)
        let index = try LibraryIndex.load(vault: vault)

        #expect(try index.url(for: index.articles[0]) == articleURL.standardizedFileURL)
        #expect(try index.dashboardURL() == dashboard.standardizedFileURL)
    }

    @Test("opening rejects a symlink that escapes the Vault")
    func symlinkEscape() throws {
        let id = "sha256:" + String(repeating: "f", count: 64)
        let relative = "03-文章/link/escaped-ffffffff.md"
        let vault = try makeVault(articles: [
            id: record(
                id: id, title: "文章", project: "项目", account: "公众号",
                published: "2026-07-11", path: relative
            ),
        ])
        let outside = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: outside, withIntermediateDirectories: true)
        try Data().write(to: outside.appendingPathComponent("escaped-ffffffff.md"))
        let articleRoot = vault.appendingPathComponent("03-文章", isDirectory: true)
        try FileManager.default.createDirectory(at: articleRoot, withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(
            at: articleRoot.appendingPathComponent("link"), withDestinationURL: outside
        )
        let index = try LibraryIndex.load(vault: vault)

        #expect(throws: LibraryIndexError.self) { try index.url(for: index.articles[0]) }
    }
}
