import SwiftUI
import WebKit
import UIKit

@main struct SinaOCOApp: App {
    var body: some Scene { WindowGroup { ConnectionView() } }
}

struct ConnectionView: View {
    @AppStorage("sina_server_url") private var address = ""
    @State private var active: URL? = nil
    @State private var error = ""
    var body: some View {
        NavigationStack {
            Group {
                if let url = active {
                    SecureDashboard(url: url)
                        .toolbar { Button("تنظیم اتصال") { active = nil } }
                } else {
                    Form {
                        Section("SINA OCO") {
                            Text("نمایش وضعیت سرور خودت؛ انتشار زنده فعلاً غیرفعال است.")
                            TextField("https://…", text: $address)
                                .textInputAutocapitalization(.never).autocorrectionDisabled()
                                .keyboardType(.URL).environment(\.layoutDirection, .leftToRight)
                            Button("بازکردن سرور امن") { connect() }
                            if !error.isEmpty { Text(error).foregroundStyle(.red) }
                        }
                    }
                }
            }
            .navigationTitle("OCO سینا")
        }
    }
    private func connect() {
        guard let parts = URLComponents(string: address.trimmingCharacters(in: .whitespacesAndNewlines)),
              parts.scheme?.lowercased() == "https", let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              parts.path.isEmpty || parts.path == "/", let url = parts.url else {
            error = "نشانی HTTPS ریشهٔ سرور را بدون رمز یا کلید وارد کن."; return
        }
        error = ""; active = url
    }
}

struct SecureDashboard: UIViewRepresentable {
    let url: URL
    func makeCoordinator() -> Coordinator { Coordinator(origin: url) }
    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = context.coordinator
        view.load(URLRequest(url: url))
        return view
    }
    func updateUIView(_ view: WKWebView, context: Context) {}
    final class Coordinator: NSObject, WKNavigationDelegate {
        let origin: URL
        init(origin: URL) { self.origin = origin }
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let target = action.request.url, target.scheme == "https" else { decisionHandler(.cancel); return }
            if target.host == origin.host && target.port == origin.port { decisionHandler(.allow); return }
            if target.host == "t.me" { UIApplication.shared.open(target) }
            decisionHandler(.cancel)
        }
    }
}
