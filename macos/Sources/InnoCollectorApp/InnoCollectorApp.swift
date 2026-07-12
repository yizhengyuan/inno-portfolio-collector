import SwiftUI
import Foundation
import InnoAppCore
import InnoCollectorFeature

@main
struct InnoCollectorApp: App {
    private let locations: AppLocations?
    private let webPreviewEnabled: Bool
    private let webLauncher: LocalWebLauncher?

    init() {
        let locations = try? AppLocations.collector()
        let webPreviewEnabled = LocalWebPreview.isEnabled(
            environment: ProcessInfo.processInfo.environment
        )

        self.locations = locations
        self.webPreviewEnabled = webPreviewEnabled
        if webPreviewEnabled,
           let locations,
           let executable = locations.collectorWebServer,
           let projectsConfig = locations.projectsConfig {
            webLauncher = LocalWebLauncher(
                executable: executable,
                pluginsDirectory: executable.deletingLastPathComponent(),
                supportRoot: locations.supportRoot,
                projectsConfig: projectsConfig
            )
        } else {
            webLauncher = nil
        }
    }

    var body: some Scene {
        WindowGroup {
            if webPreviewEnabled, let webLauncher {
                CollectorWebPreviewView(launcher: webLauncher)
            } else if webPreviewEnabled {
                CollectorUnavailableView(message: "无法初始化本地 Web 预览。")
            } else if let locations {
                CollectorRootView(locations: locations)
            } else {
                CollectorUnavailableView(message: "无法初始化本地应用目录。")
            }
        }
    }
}

private struct CollectorWebPreviewView: View {
    private enum Status: Equatable {
        case starting
        case opened
        case failed(String)
    }

    let launcher: LocalWebLauncher
    @State private var status = Status.starting

    var body: some View {
        VStack(spacing: 16) {
            statusIcon
            Text(title)
                .font(.title2.bold())
            Text(detail)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
        }
        .padding(36)
        .frame(minWidth: 520, minHeight: 260)
        .task {
            await openWebCollector()
        }
        .onDisappear {
            launcher.stop()
        }
    }

    @ViewBuilder
    private var statusIcon: some View {
        switch status {
        case .starting:
            ProgressView()
                .controlSize(.large)
        case .opened:
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 40))
                .foregroundStyle(.green)
        case .failed:
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 40))
                .foregroundStyle(.orange)
        }
    }

    private var title: String {
        switch status {
        case .starting:
            "正在启动英诺资讯采集"
        case .opened:
            "已在默认浏览器中打开"
        case .failed:
            "本地 Web 预览暂不可用"
        }
    }

    private var detail: String {
        switch status {
        case .starting:
            "正在安全启动仅限本机访问的服务，请稍候。"
        case .opened:
            "请保持此窗口开启；关闭窗口会同时停止本地服务。"
        case .failed(let message):
            message
        }
    }

    private func openWebCollector() async {
        do {
            try await launcher.open()
            try Task.checkCancellation()
            status = .opened
        } catch is CancellationError {
            return
        } catch let error as LocalWebLauncherError {
            status = .failed(message(for: error))
        } catch {
            status = .failed("本地 Web 服务启动失败，请退出后重新打开应用。")
        }
    }

    private func message(for error: LocalWebLauncherError) -> String {
        switch error {
        case .unavailable:
            "找不到完整且可信的本地 Web 服务组件，请重新安装应用。"
        case .launchFailed:
            "本地 Web 服务未能启动，请退出后重新打开应用。"
        case .notReady:
            "本地 Web 服务启动超时，请退出后重试。"
        case .invalidReady:
            "本地 Web 服务未通过安全启动校验，请重新安装应用。"
        case .browserUnavailable:
            "无法打开默认浏览器，请检查 macOS 的默认浏览器设置。"
        }
    }
}

private struct CollectorRootView: View {
    @StateObject private var model: CollectorViewModel

    init(locations: AppLocations) {
        let localLogin = locations.mooreHelper.flatMap { helper in
            locations.exporterRuntime.map { runtime in
                MooreLocalLoginServer(
                    executable: helper,
                    pluginsDirectory: locations.helper.deletingLastPathComponent(),
                    runtimeDirectory: runtime,
                    supportRoot: locations.supportRoot
                )
            }
        }
        _model = StateObject(wrappedValue: CollectorViewModel(
            helper: HelperClient(executable: locations.helper),
            locations: locations,
            localLogin: localLogin
        ))
    }

    var body: some View {
        CollectorContentView(model: model)
    }
}

private struct CollectorUnavailableView: View {
    let message: String

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle")
                .font(.largeTitle)
            Text("采集端暂不可用")
                .font(.title2.bold())
            Text(message)
                .foregroundStyle(.secondary)
        }
        .padding()
    }
}
