import Foundation
import Testing
@testable import InnoAppCore

@Suite("Legal notices")
struct LegalNoticeTests {
    @Test("loads every distributed legal document in stable order")
    func loadsDocuments() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let licenses = root.appendingPathComponent("ThirdPartyLicenses", isDirectory: true)
        try FileManager.default.createDirectory(at: licenses, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let fixtures = [
            ("inno-news-suite-LICENSE.txt", "project license"),
            ("wechat-article-exporter-LICENSE.txt", "wechat license"),
            ("moore-wechat-article-downloader-LICENSE.txt", "moore license"),
            ("THIRD_PARTY_NOTICES.md", "notices"),
        ]
        for (name, content) in fixtures {
            try content.write(
                to: licenses.appendingPathComponent(name),
                atomically: true,
                encoding: .utf8
            )
        }

        let documents = try LegalNoticeLoader(resourcesURL: root).load()

        #expect(documents.map(\.title) == [
            "英诺资讯工具 MIT License",
            "wechat-article-exporter MIT License",
            "moore-wechat-article-downloader MIT License",
            "第三方软件声明",
        ])
        #expect(documents.map(\.text) == fixtures.map(\.1))
    }

    @Test("reports every missing legal document without hiding its name")
    func missingDocument() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let licenses = root.appendingPathComponent("ThirdPartyLicenses", isDirectory: true)
        try FileManager.default.createDirectory(at: licenses, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let filenames = [
            "inno-news-suite-LICENSE.txt",
            "wechat-article-exporter-LICENSE.txt",
            "moore-wechat-article-downloader-LICENSE.txt",
            "THIRD_PARTY_NOTICES.md",
        ]
        for filename in filenames {
            try "fixture".write(
                to: licenses.appendingPathComponent(filename),
                atomically: true,
                encoding: .utf8
            )
        }

        for filename in filenames {
            let url = licenses.appendingPathComponent(filename)
            try FileManager.default.removeItem(at: url)
            #expect(throws: LegalNoticeError.missingDocument(filename)) {
                try LegalNoticeLoader(resourcesURL: root).load()
            }
            try "fixture".write(to: url, atomically: true, encoding: .utf8)
        }
    }

    @Test("copyright and authorization boundary is explicit")
    func authorizationBoundary() {
        let copy = LegalNoticeCopy.authorizationBoundary
        #expect(copy.contains("文章、图片和附件的版权归原作者或其他权利人"))
        #expect(copy.contains("合法授权"))
        #expect(copy.contains("不当然等于"))
        #expect(copy.contains("再分发"))
    }
}
