import AppKit
import Foundation

public protocol WorkspaceOpening: AnyObject {
    func urlForApplication(toOpen url: URL) -> URL?
    func open(_ url: URL) -> Bool
}

extension NSWorkspace: WorkspaceOpening {}

public struct ObsidianLauncher {
    private let workspace: any WorkspaceOpening

    public init(workspace: any WorkspaceOpening = NSWorkspace.shared) {
        self.workspace = workspace
    }

    public func open(vault: URL) -> Bool {
        guard
            let probe = URL(string: "obsidian://open"),
            workspace.urlForApplication(toOpen: probe) != nil
        else { return false }
        var components = URLComponents(string: "obsidian://open")
        components?.queryItems = [URLQueryItem(name: "path", value: vault.path)]
        guard let url = components?.url else { return false }
        return workspace.open(url)
    }
}
