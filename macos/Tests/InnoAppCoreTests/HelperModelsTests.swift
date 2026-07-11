import Foundation
import Testing
@testable import InnoAppCore

@Suite("Helper models")
struct HelperModelsTests {
    @Test("request uses stable keys")
    func requestUsesStableKeys() throws {
        let request = HelperRequest(id: "r1", command: "status", arguments: [:])
        let data = try JSONEncoder().encode(request)
        let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]

        #expect(Set(object?.keys.map { $0 } ?? []) == Set(["id", "command", "arguments"]))
    }

    @Test("failure response decodes without result")
    func failureResponseDecodesWithoutResult() throws {
        let data = #"{"error":"bad package","id":"r1","ok":false}"#.data(using: .utf8)!

        let response = try JSONDecoder().decode(HelperResponse.self, from: data)

        #expect(response.ok == false)
        #expect(response.result == nil)
        #expect(response.error == "bad package")
    }

    @Test("JSON values round trip nested objects and reject floats")
    func jsonValueRoundTripsNestedObjectsWithoutFloats() throws {
        let value: JSONValue = .object([
            "name": .string("资讯"),
            "count": .integer(3),
            "ready": .boolean(true),
            "items": .array([.null, .string("稿件")]),
        ])

        let encoded = try JSONEncoder().encode(value)

        #expect(try JSONDecoder().decode(JSONValue.self, from: encoded) == value)
        #expect(throws: (any Error).self) {
            try JSONDecoder().decode(JSONValue.self, from: Data("1.5".utf8))
        }
    }
}
