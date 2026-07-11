import Foundation
import Testing
@testable import InnoReaderFeature

private final class FakeWorkspace: WorkspaceOpening, @unchecked Sendable {
    let installed: Bool
    private(set) var opened: [URL] = []

    init(installed: Bool) {
        self.installed = installed
    }

    func urlForApplication(toOpen url: URL) -> URL? {
        installed ? URL(fileURLWithPath: "/Applications/Obsidian.app") : nil
    }

    func open(_ url: URL) -> Bool {
        opened.append(url)
        return true
    }
}

@Suite("Obsidian launcher")
struct ObsidianLauncherTests {
    @Test("opens an installed Obsidian with the local Vault path")
    func installed() throws {
        let workspace = FakeWorkspace(installed: true)
        let launcher = ObsidianLauncher(workspace: workspace)
        let vault = URL(fileURLWithPath: "/tmp/英诺 资讯库", isDirectory: true)

        #expect(launcher.open(vault: vault))
        let opened = try #require(workspace.opened.first)
        let components = try #require(URLComponents(url: opened, resolvingAgainstBaseURL: false))
        #expect(components.scheme == "obsidian")
        #expect(components.host == "open")
        #expect(components.queryItems == [URLQueryItem(name: "path", value: vault.path)])
    }

    @Test("returns false without opening when Obsidian is not installed")
    func notInstalled() {
        let workspace = FakeWorkspace(installed: false)
        let launcher = ObsidianLauncher(workspace: workspace)

        #expect(!launcher.open(vault: URL(fileURLWithPath: "/tmp/vault")))
        #expect(workspace.opened.isEmpty)
    }
}
