import AppKit
import SwiftUI

public struct ReaderContentView: View {
    private enum Section: String, CaseIterable, Identifiable {
        case reading = "阅读"
        case dashboard = "看板"
        case editing = "编辑"
        case updates = "更新"
        case obsidian = "Obsidian"
        var id: String { rawValue }
    }

    @ObservedObject private var model: ReaderViewModel
    @State private var selection: Section? = .reading
    @State private var query = ""
    @State private var project = ""
    @State private var activeTask: Task<Void, Never>?
    @State private var selectedArticleID = ""
    @State private var obsidianMessage: String?

    public init(model: ReaderViewModel) {
        self.model = model
    }

    public var body: some View {
        NavigationSplitView {
            List(Section.allCases, selection: $selection) { section in
                Label(section.rawValue, systemImage: icon(for: section))
            }
            .navigationTitle("英诺资讯")
        } detail: {
            VStack(alignment: .leading, spacing: 16) {
                header
                if let error = model.errorMessage {
                    Text(error).foregroundStyle(.red).textSelection(.enabled)
                }
                content
            }
            .padding(24)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .frame(minWidth: 900, minHeight: 580)
        .task { await model.refresh() }
    }

    private var header: some View {
        HStack {
            Text(selection?.rawValue ?? "阅读").font(.largeTitle.bold())
            Spacer()
            if model.isBusy {
                ProgressView()
                Button("取消") { activeTask?.cancel() }
            }
        }
    }

    @ViewBuilder private var content: some View {
        switch selection ?? .reading {
        case .reading:
            HStack {
                TextField("搜索标题、项目或公众号", text: $query)
                    .textFieldStyle(.roundedBorder)
                Picker("项目", selection: $project) {
                    Text("全部项目").tag("")
                    ForEach(projects, id: \.self) { Text($0).tag($0) }
                }
                .frame(width: 220)
            }
            List(filteredArticles) { article in
                Button {
                    if let url = try? model.articleURL(for: article) { NSWorkspace.shared.open(url) }
                } label: {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(article.title).font(.headline)
                        Text("\(article.project) · \(article.account) · \(article.published)")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                .buttonStyle(.plain)
            }
        case .dashboard:
            Text("看板完全来自本地资料库，不加载远程网页内容。")
            HStack {
                Button("打开离线看板") {
                    if let url = try? model.dashboardURL() { NSWorkspace.shared.open(url) }
                }
                Button("重建看板") { start { await model.rebuildDashboard() } }
                    .disabled(model.isBusy)
            }
        case .editing:
            Text("编辑稿只写入 10-编辑稿，不会修改采集到的原文或附件。")
            Picker("来源文章", selection: $selectedArticleID) {
                Text("请选择文章").tag("")
                ForEach(model.articles) { article in
                    Text(article.title).tag(article.id)
                }
            }
            HStack {
                ForEach(DraftKind.allCases, id: \.rawValue) { kind in
                    Button("新建\(kind.title)") { createDraft(kind) }
                        .disabled(model.isBusy || selectedArticle == nil)
                }
            }
            if !model.drafts.isEmpty {
                Divider()
                Text("本次新建的编辑稿").font(.headline)
                ForEach(model.drafts) { draft in
                    Text("\(draft.kind.title) · \(draft.title)")
                }
                Button("导出全部编辑稿…") { saveDraftPackage() }
                    .disabled(model.isBusy)
            }
        case .updates:
            Text("先预览更新包；只有再次确认后才会写入本地资料库。")
            Button("选择更新包…") { openUpdate() }.disabled(model.isBusy)
            if let preview = model.updatePreview {
                GroupBox("更新预览") {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(preview.kind == .baseline ? "首次完整资料包" : "增量资料包")
                        Text("新增或变更：\(preview.includedCount) 项")
                        Text("删除：\(preview.deletedCount) 项")
                        Text("目标版本：\(preview.targetVersion)").textSelection(.enabled)
                        Button("确认应用更新") { start { await model.applyPreviewedUpdate() } }
                            .buttonStyle(.borderedProminent)
                            .disabled(model.isBusy)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        case .obsidian:
            Text("推荐安装 Obsidian，以完整使用双向链接、标签和个人笔记。")
            Text(model.locations.vault.path).textSelection(.enabled).foregroundStyle(.secondary)
            Button("在 Obsidian 中打开") {
                obsidianMessage = ObsidianLauncher().open(vault: model.locations.vault)
                    ? nil : "尚未检测到 Obsidian；安装后可直接打开本地资料库，其他阅读功能不受影响。"
            }
            if let obsidianMessage {
                Text(obsidianMessage).foregroundStyle(.secondary)
            }
        }
    }

    private var projects: [String] {
        Array(Set(model.articles.map(\.project))).sorted()
    }

    private var filteredArticles: [LibraryArticle] {
        model.filteredArticles(query: query, project: project)
    }

    private var selectedArticle: LibraryArticle? {
        model.articles.first { $0.id == selectedArticleID }
    }

    private func icon(for section: Section) -> String {
        switch section {
        case .reading: "newspaper"
        case .dashboard: "chart.bar"
        case .editing: "square.and.pencil"
        case .updates: "shippingbox.and.arrow.backward"
        case .obsidian: "link"
        }
    }

    private func openUpdate() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        start { await model.previewUpdate(package: url) }
    }

    private func createDraft(_ kind: DraftKind) {
        guard let selectedArticle else { return }
        start { await model.createDraft(from: selectedArticle, kind: kind) }
    }

    private func saveDraftPackage() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "英诺编辑稿.inno-drafts"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        start { await model.exportDrafts(ids: model.drafts.map(\.id), destination: url) }
    }

    private func start(_ operation: @escaping @MainActor () async -> Void) {
        guard activeTask == nil else { return }
        activeTask = Task {
            await operation()
            activeTask = nil
        }
    }
}
