import SwiftUI
import InnoAppCore
import InnoReaderFeature

@main
struct InnoReaderApp: App {
    private let locations: AppLocations?

    init() {
        locations = try? AppLocations.reader()
    }

    var body: some Scene {
        WindowGroup {
            if let locations {
                ReaderRootView(locations: locations)
            } else {
                VStack(spacing: 12) {
                    Image(systemName: "exclamationmark.triangle").font(.largeTitle)
                    Text("阅读端暂不可用").font(.title2.bold())
                    Text("无法初始化本地应用目录。").foregroundStyle(.secondary)
                }
                .padding()
            }
        }
    }
}

private struct ReaderRootView: View {
    @StateObject private var model: ReaderViewModel

    init(locations: AppLocations) {
        _model = StateObject(wrappedValue: ReaderViewModel(
            helper: HelperClient(executable: locations.helper),
            locations: locations
        ))
    }

    var body: some View {
        ReaderContentView(model: model)
    }
}
