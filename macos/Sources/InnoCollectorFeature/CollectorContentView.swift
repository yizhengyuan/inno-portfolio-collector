import AppKit
import InnoAppCore
import SwiftUI

public struct CollectorContentView: View {
    private enum Section: String, CaseIterable, Identifiable {
        case overview = "概览"
        case collect = "采集"
        case library = "资料库"
        case delivery = "交付"
        case inbox = "稿件收件箱"
        case about = "关于与许可证"
        var id: String { rawValue }
    }

    @ObservedObject private var model: CollectorViewModel
    @State private var selection: Section? = .overview
    @State private var activeTask: Task<Void, Never>?

    public init(model: CollectorViewModel) {
        self.model = model
    }

    public var body: some View {
        NavigationSplitView {
            List(Section.allCases, selection: $selection) { section in
                Label(section.rawValue, systemImage: icon(for: section))
                    .tag(section)
            }
            .navigationTitle("英诺资讯采集")
        } detail: {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    header
                    if let error = model.errorMessage {
                        Text(error).foregroundStyle(.red).textSelection(.enabled)
                    }
                    content
                }
                .padding(24)
                .frame(maxWidth: 920, alignment: .leading)
            }
        }
        .frame(minWidth: 880, minHeight: 560)
        .task { await model.refresh() }
        .onDisappear { model.stopLocalLogin() }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading) {
                Text(selection?.rawValue ?? "概览").font(.largeTitle.bold())
                Text("登录状态与采集能力只保存在这台 Mac。")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if model.isBusy {
                ProgressView()
                Button("取消") { activeTask?.cancel() }
            }
        }
    }

    @ViewBuilder private var content: some View {
        switch selection ?? .overview {
        case .overview:
            HStack(spacing: 12) {
                metric("文章", model.summary?.articleCount ?? 0)
                metric("项目", model.summary?.projectCount ?? 0)
                metric("部分失败", model.summary?.failedProjects ?? 0)
            }
            Button("刷新状态") { start { await model.refresh() } }
                .disabled(model.isBusy)
        case .collect:
            Text("仅供采集者本人在这台 Mac 扫码登录；请勿分享采集端或登录状态。")
                .foregroundStyle(.secondary)
            Button("打开本地登录后台") {
                start { await model.openLocalLogin() }
            }
            .disabled(model.isBusy)
            Divider()
            Text("先运行预检，确认登录状态与 10 个公众号精确映射，再开始采集。")
            HStack {
                Button("运行预检") { start { await model.preflight() } }
                Button("开始采集") { start { await model.collect() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.isBusy || !model.lastPreflightSucceeded)
            }
        case .library:
            Text("资料库位于：\(model.locations.vault.path)")
                .textSelection(.enabled)
            Button("在 Finder 中显示") {
                NSWorkspace.shared.activateFileViewerSelecting([model.locations.vault])
            }
        case .delivery:
            Text("生成带版本号和哈希校验的更新包；朋友端导入时不会覆盖人工稿件。")
            HStack {
                Button("生成基线更新包") { saveUpdate(basePackage: nil) }
                Button("生成增量更新包…") { selectBaseAndSaveUpdate() }
            }
            .disabled(model.isBusy)
        case .inbox:
            Text("先接收朋友回传的编辑稿包；确认后才写入编辑区，冲突版本会并列保留。")
            Button("导入编辑稿包") { openDraftPackage() }
                .disabled(model.isBusy)
            ForEach(model.receivedDrafts) { receipt in
                HStack {
                    Text("待确认：\(receipt.draftCount) 份稿件")
                    if receipt.alreadyReceived {
                        Text("已接收过").foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("确认收录") {
                        start { await model.acceptDraft(receipt: receipt) }
                    }
                    .disabled(model.isBusy)
                }
                .padding(.vertical, 4)
            }
        case .about:
            LegalNoticeView()
        }
    }

    private func metric(_ label: String, _ value: Int) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(value.formatted()).font(.title.bold())
            Text(label).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(.background, in: RoundedRectangle(cornerRadius: 12))
    }

    private func icon(for section: Section) -> String {
        switch section {
        case .overview: "chart.bar"
        case .collect: "arrow.triangle.2.circlepath"
        case .library: "books.vertical"
        case .delivery: "shippingbox"
        case .inbox: "tray.and.arrow.down"
        case .about: "info.circle"
        }
    }

    private func saveUpdate(basePackage: URL?) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = basePackage == nil
            ? "英诺资讯基线.inno-update" : "英诺资讯增量.inno-update"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        start { await model.buildUpdate(destination: url, basePackage: basePackage) }
    }

    private func selectBaseAndSaveUpdate() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.message = "选择朋友当前使用版本对应的上一份更新包"
        guard panel.runModal() == .OK, let base = panel.url else { return }
        saveUpdate(basePackage: base)
    }

    private func openDraftPackage() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        start { await model.receiveDrafts(package: url) }
    }

    private func start(_ operation: @escaping @MainActor () async -> Void) {
        guard activeTask == nil else { return }
        activeTask = Task {
            await operation()
            activeTask = nil
        }
    }
}
