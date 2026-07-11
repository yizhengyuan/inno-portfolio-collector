import Foundation

public enum LibraryIndexError: Error, Equatable, Sendable {
    case unreadableManifest
    case invalidManifest
    case unsafeArticlePath
    case missingLocalFile
}

public struct LibraryArticle: Equatable, Identifiable, Sendable {
    public let id: String
    public let title: String
    public let project: String
    public let account: String
    public let published: String
    public let sourceURL: String
    public let relativePath: String

    public init(
        id: String,
        title: String,
        project: String,
        account: String,
        published: String,
        sourceURL: String,
        relativePath: String
    ) {
        self.id = id
        self.title = title
        self.project = project
        self.account = account
        self.published = published
        self.sourceURL = sourceURL
        self.relativePath = relativePath
    }
}

public struct LibraryIndex: Equatable, Sendable {
    public let articles: [LibraryArticle]
    private let vault: URL

    public static func load(vault: URL) throws -> Self {
        let root = vault.standardizedFileURL
        let manifest = root.appendingPathComponent("90-系统/manifest.json")
        let data: Data
        do {
            data = try Data(contentsOf: manifest)
        } catch {
            throw LibraryIndexError.unreadableManifest
        }
        let payload: Any
        do {
            payload = try JSONSerialization.jsonObject(with: data)
        } catch {
            throw LibraryIndexError.invalidManifest
        }
        guard
            let object = payload as? [String: Any],
            object["version"] as? Int == 1,
            let records = object["articles"] as? [String: Any]
        else {
            throw LibraryIndexError.invalidManifest
        }

        var articles: [LibraryArticle] = []
        for (id, rawRecord) in records {
            guard
                isStableID(id),
                let record = rawRecord as? [String: Any],
                record["key"] as? String == id,
                let title = nonempty(record["title"]),
                let project = nonempty(record["project"]),
                let account = nonempty(record["account"]),
                let published = nonempty(record["published"]),
                validDate(published),
                let relativePath = nonempty(record["path"]),
                safeArticlePath(relativePath)
            else {
                throw LibraryIndexError.invalidManifest
            }
            articles.append(LibraryArticle(
                id: id,
                title: title,
                project: project,
                account: account,
                published: published,
                sourceURL: record["source_url"] as? String ?? "",
                relativePath: relativePath
            ))
        }
        articles.sort {
            if $0.published != $1.published { return $0.published > $1.published }
            if $0.title != $1.title { return $0.title > $1.title }
            return $0.id > $1.id
        }
        return Self(articles: articles, vault: root)
    }

    public func search(_ query: String, project: String? = nil) -> [LibraryArticle] {
        let needle = Self.normalized(query.trimmingCharacters(in: .whitespacesAndNewlines))
        return articles.filter { article in
            let projectMatches = project == nil || project == "" || article.project == project
            guard projectMatches else { return false }
            guard !needle.isEmpty else { return true }
            return [article.title, article.project, article.account]
                .map(Self.normalized)
                .contains { $0.contains(needle) }
        }
    }

    public func url(for article: LibraryArticle) throws -> URL {
        guard articles.contains(where: { $0.id == article.id && $0.relativePath == article.relativePath }) else {
            throw LibraryIndexError.invalidManifest
        }
        return try localFile(relativePath: article.relativePath)
    }

    public func dashboardURL() throws -> URL {
        try localFile(relativePath: "80-离线看板/index.html")
    }

    private func localFile(relativePath: String) throws -> URL {
        let resolvedRoot = vault.resolvingSymlinksInPath().standardizedFileURL
        let candidate = vault.appendingPathComponent(relativePath)
            .resolvingSymlinksInPath().standardizedFileURL
        guard Self.isInside(candidate, root: resolvedRoot) else {
            throw LibraryIndexError.unsafeArticlePath
        }
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: candidate.path, isDirectory: &isDirectory), !isDirectory.boolValue else {
            throw LibraryIndexError.missingLocalFile
        }
        return candidate
    }

    private static func nonempty(_ value: Any?) -> String? {
        guard let value = value as? String, !value.isEmpty else { return nil }
        return value
    }

    private static func isStableID(_ value: String) -> Bool {
        guard value.hasPrefix("sha256:"), value.count == 71 else { return false }
        return value.dropFirst(7).unicodeScalars.allSatisfy {
            (48...57).contains($0.value) || (97...102).contains($0.value)
        }
    }

    private static func safeArticlePath(_ value: String) -> Bool {
        guard
            value == value.precomposedStringWithCanonicalMapping,
            !value.hasPrefix("/"),
            !value.contains("\\"),
            !value.contains("\0")
        else { return false }
        let parts = value.split(separator: "/", omittingEmptySubsequences: false)
        return parts.count >= 3
            && parts.first == "03-文章"
            && parts.allSatisfy { !$0.isEmpty && $0 != "." && $0 != ".." }
            && value.lowercased().hasSuffix(".md")
    }

    private static func validDate(_ value: String) -> Bool {
        guard value.count == 10 else { return false }
        let pieces = value.split(separator: "-", omittingEmptySubsequences: false)
        guard
            pieces.count == 3,
            pieces[0].count == 4,
            pieces[1].count == 2,
            pieces[2].count == 2,
            let year = Int(pieces[0]),
            let month = Int(pieces[1]),
            let day = Int(pieces[2])
        else { return false }
        let calendar = Calendar(identifier: .gregorian)
        guard let date = calendar.date(from: DateComponents(year: year, month: month, day: day)) else {
            return false
        }
        let components = calendar.dateComponents([.year, .month, .day], from: date)
        return components.year == year && components.month == month && components.day == day
    }

    private static func normalized(_ value: String) -> String {
        value.folding(
            options: [.caseInsensitive, .widthInsensitive, .diacriticInsensitive],
            locale: Locale(identifier: "zh_CN")
        )
    }

    private static func isInside(_ candidate: URL, root: URL) -> Bool {
        candidate.path == root.path || candidate.path.hasPrefix(root.path + "/")
    }
}
