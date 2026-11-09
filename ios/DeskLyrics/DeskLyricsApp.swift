import SwiftUI
import WebKit

// A full-screen window onto the Desk Music Companion page that keeps the phone awake.
// The Mac is reached by its Bonjour name, so it still works if its IP address changes.
private let defaultAddress = "http://Prashanths-Mac-Studio.local:8787"

@main
struct DeskLyricsApp: App {
    var body: some Scene {
        WindowGroup { RootView() }
    }
}

struct RootView: View {
    @AppStorage("address") private var address = defaultAddress
    @State private var failed = false
    @State private var attempt = 0
    @Environment(\.scenePhase) private var phase

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            LyricsWebView(address: address, attempt: attempt, failed: $failed)
                .ignoresSafeArea()
                .opacity(failed ? 0 : 1)
            if failed {
                ConnectView(address: $address) { attempt += 1 }
            }
        }
        .statusBarHidden()
        .persistentSystemOverlays(.hidden)
        .preferredColorScheme(.dark)
        .onAppear { UIApplication.shared.isIdleTimerDisabled = true }
        .onChange(of: phase) { _, newPhase in
            if newPhase == .active {
                UIApplication.shared.isIdleTimerDisabled = true
                if failed { attempt += 1 }
            }
        }
        .task(id: failed) {
            // Keeps trying while the Mac is asleep or the server is off.
            while failed && !Task.isCancelled {
                try? await Task.sleep(for: .seconds(5))
                if failed && !Task.isCancelled { attempt += 1 }
            }
        }
    }
}

struct ConnectView: View {
    @Binding var address: String
    let retry: () -> Void
    @State private var draft = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Can't reach your Mac")
                .font(.title2.weight(.semibold))
            Text("Check that Desk Music Companion is running on the Mac and that this phone is on the same Wi-Fi. Retrying automatically.")
                .foregroundStyle(.secondary)
            TextField("Mac address", text: $draft)
                .textFieldStyle(.roundedBorder)
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .onSubmit(save)
            Button("Connect", action: save)
                .buttonStyle(.borderedProminent)
        }
        .padding(24)
        .frame(maxWidth: 460)
        .onAppear { draft = address }
    }

    private func save() {
        let trimmed = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        address = trimmed.contains("://") ? trimmed : "http://\(trimmed)"
        retry()
    }
}

struct LyricsWebView: UIViewRepresentable {
    let address: String
    let attempt: Int
    @Binding var failed: Bool

    func makeCoordinator() -> Coordinator { Coordinator(failed: $failed) }

    func makeUIView(context: Context) -> WKWebView {
        let webView = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        webView.navigationDelegate = context.coordinator
        webView.isOpaque = false
        webView.backgroundColor = .black
        webView.scrollView.backgroundColor = .black
        webView.scrollView.isScrollEnabled = false
        webView.scrollView.bounces = false
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        let coordinator = context.coordinator
        guard coordinator.loadedAddress != address || coordinator.loadedAttempt != attempt,
              let url = URL(string: address) else { return }
        coordinator.loadedAddress = address
        coordinator.loadedAttempt = attempt
        webView.load(URLRequest(url: url, timeoutInterval: 8))
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        @Binding var failed: Bool
        var loadedAddress: String?
        var loadedAttempt: Int?

        init(failed: Binding<Bool>) { _failed = failed }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { failed = false }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            noteFailure(error)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            noteFailure(error)
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { webView.reload() }

        private func noteFailure(_ error: Error) {
            // A new load replacing an unfinished one is not a real failure.
            if (error as NSError).code != NSURLErrorCancelled { failed = true }
        }
    }
}
