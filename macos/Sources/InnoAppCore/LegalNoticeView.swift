import Foundation
import SwiftUI

public struct LegalNoticeDocument: Equatable, Sendable {
    public let title: String
    public let text: String
}

public enum LegalNoticeError: Error, Equatable, Sendable {
    case resourcesUnavailable
    case missingDocument(String)
    case unreadableDocument(String)
}

extension LegalNoticeError: LocalizedError {
    public var errorDescription: String? {
        switch self {
        case .resourcesUnavailable:
            "无法找到 App 的许可证资源目录。"
        case let .missingDocument(name):
            "缺少许可证文件：\(name)"
        case let .unreadableDocument(name):
            "无法读取许可证文件：\(name)"
        }
    }
}

public struct LegalNoticeLoader: Sendable {
    private struct Descriptor: Sendable {
        let filename: String
        let title: String
    }

    private static let descriptors = [
        Descriptor(
            filename: "inno-news-suite-LICENSE.txt",
            title: "英诺资讯工具 MIT License"
        ),
        Descriptor(
            filename: "wechat-article-exporter-LICENSE.txt",
            title: "wechat-article-exporter MIT License"
        ),
        Descriptor(
            filename: "moore-wechat-article-downloader-LICENSE.txt",
            title: "moore-wechat-article-downloader MIT License"
        ),
        Descriptor(filename: "THIRD_PARTY_NOTICES.md", title: "第三方软件声明"),
    ]

    private let resourcesURL: URL

    public init(resourcesURL: URL) {
        self.resourcesURL = resourcesURL
    }

    public func load() throws -> [LegalNoticeDocument] {
        let directory = resourcesURL
            .appendingPathComponent("ThirdPartyLicenses", isDirectory: true)
        return try Self.descriptors.map { descriptor in
            let url = directory.appendingPathComponent(descriptor.filename, isDirectory: false)
            guard FileManager.default.fileExists(atPath: url.path) else {
                throw LegalNoticeError.missingDocument(descriptor.filename)
            }
            do {
                return LegalNoticeDocument(
                    title: descriptor.title,
                    text: try String(contentsOf: url, encoding: .utf8)
                )
            } catch {
                throw LegalNoticeError.unreadableDocument(descriptor.filename)
            }
        }
    }
}

public enum LegalNoticeCopy {
    public static let authorizationBoundary = """
    本项目代码以 MIT License 开源；两项上游软件也按各自 MIT License 使用并保留署名。文章、图片和附件的版权归原作者或其他权利人，MIT License 不授予这些内容的使用权。请仅在具有合法授权或其他合法依据时采集、保存和分享内容。能够登录或运营公众号，不当然等于已获权代表所有权利人授权采集或再分发。
    """
}

public struct LegalNoticeView: View {
    private let documents: Result<[LegalNoticeDocument], Error>

    public init(resourcesURL: URL? = Bundle.main.resourceURL) {
        guard let resourcesURL else {
            documents = .failure(LegalNoticeError.resourcesUnavailable)
            return
        }
        documents = Result { try LegalNoticeLoader(resourcesURL: resourcesURL).load() }
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            GroupBox("版权与授权边界") {
                Text(LegalNoticeCopy.authorizationBoundary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                    .padding(.vertical, 4)
            }

            switch documents {
            case let .success(documents):
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        ForEach(documents, id: \.title) { document in
                            DisclosureGroup(document.title) {
                                Text(document.text)
                                    .font(.system(.caption, design: .monospaced))
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .textSelection(.enabled)
                                    .padding(.top, 8)
                            }
                            .padding(12)
                            .background(.background, in: RoundedRectangle(cornerRadius: 10))
                        }
                    }
                }
            case let .failure(error):
                Text(error.localizedDescription)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
        }
    }
}
