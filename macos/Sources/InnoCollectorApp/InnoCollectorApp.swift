import SwiftUI
import InnoAppCore
import InnoCollectorFeature

@main
struct InnoCollectorApp: App {
    private let locations: AppLocations?

    init() {
        locations = try? AppLocations.collector()
    }

    var body: some Scene {
        WindowGroup {
            if let locations {
                CollectorRootView(locations: locations)
            } else {
                CollectorUnavailableView(message: "无法初始化本地应用目录。")
            }
        }
    }
}

private struct CollectorRootView: View {
    @StateObject private var model: CollectorViewModel

    init(locations: AppLocations) {
        _model = StateObject(wrappedValue: CollectorViewModel(
            helper: HelperClient(executable: locations.helper),
            locations: locations
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
