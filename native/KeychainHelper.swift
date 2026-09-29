import Foundation
import Security

// Private pipe protocol. Never accept a secret through argv or write it to stderr.
let service = "dev.pair-companion.providers"
func finish(_ object: [String: Any], code: Int32 = 0) -> Never {
    let data = (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])) ?? Data("{\"ok\":false,\"error\":-50}".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([10]))
    exit(code)
}
var input = Data()
while true {
    let chunk = FileHandle.standardInput.readData(ofLength: 16_384)
    if chunk.isEmpty { break }
    guard input.count + chunk.count <= 1_048_576 else { finish(["ok": false, "error": Int(errSecParam)], code: 1) }
    input.append(chunk)
}
guard input.count <= 1_048_576,
      let request = try? JSONSerialization.jsonObject(with: input) as? [String: Any],
      let operation = request["operation"] as? String,
      let account = request["account"] as? String,
      account.range(of: "^[A-Za-z0-9_-]{1,128}$", options: .regularExpression) != nil
else { finish(["ok": false, "error": Int(errSecParam)], code: 1) }

let query: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: service,
    kSecAttrAccount as String: account,
    kSecAttrSynchronizable as String: false,
]
switch operation {
case "has", "get":
    var lookup = query
    lookup[kSecMatchLimit as String] = kSecMatchLimitOne
    lookup[kSecReturnData as String] = operation == "get"
    var item: CFTypeRef?
    let status = SecItemCopyMatching(lookup as CFDictionary, &item)
    if status == errSecItemNotFound {
        finish(operation == "has" ? ["ok": true, "present": false] : ["ok": true, "secret": NSNull()])
    }
    guard status == errSecSuccess else { finish(["ok": false, "error": Int(status)], code: 1) }
    if operation == "has" { finish(["ok": true, "present": true]) }
    guard let data = item as? Data, let secret = String(data: data, encoding: .utf8) else {
        finish(["ok": false, "error": Int(errSecDecode)], code: 1)
    }
    finish(["ok": true, "secret": secret])
case "set":
    guard let secret = request["secret"] as? String, !secret.isEmpty, secret.utf8.count <= 131_072 else {
        finish(["ok": false, "error": Int(errSecParam)], code: 1)
    }
    let values = [kSecValueData as String: Data(secret.utf8)]
    var status = SecItemUpdate(query as CFDictionary, values as CFDictionary)
    if status == errSecItemNotFound {
        var insertion = query.merging(values) { _, value in value }
        insertion[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        status = SecItemAdd(insertion as CFDictionary, nil)
    }
    guard status == errSecSuccess else { finish(["ok": false, "error": Int(status)], code: 1) }
    finish(["ok": true])
case "delete":
    let status = SecItemDelete(query as CFDictionary)
    guard status == errSecSuccess || status == errSecItemNotFound else {
        finish(["ok": false, "error": Int(status)], code: 1)
    }
    finish(["ok": true])
default:
    finish(["ok": false, "error": Int(errSecParam)], code: 1)
}
