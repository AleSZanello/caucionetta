// CaucionettaBar — caucionetta's caución bot in the macOS menu bar (unofficial; not affiliated with InvertirOnline).
//
// It only READS <state>/status.json (default ~/.local/state/caucionetta/status.json), which the bot rewrites every few
// minutes, so opening the panel never calls IOL. «Actualizar» runs `caucionetta status --refresh` (2 calls). Pause /
// resume / stop run the same `caucionetta bot …` commands as the terminal. Amounts can be hidden (eye button).
//
//   CaucionettaBar                                             the menu bar app
//   CaucionettaBar --snapshot out.png --fixture f.json [--dark]  renders the panel from a status file and exits
//
// Settings (defaults write org.caucionetta.bar <key> <value>): statusFile, caucionettaPath.

import AppKit
import SwiftUI

// MARK: - data (mirror of caucionetta/status.py)

struct Status: Codable {
    struct Day: Codable { var kind: String?; var high: Bool?; var final_at: String? }
    struct Money: Codable { var total: Double?; var free: Double?; var in_caucion: Double?; var in_fund: Double?; var in_fund_pending: Double?; var money_at: String? }
    struct Yield: Codable { var working_tna: Double?; var per_day: Double?; var month_projection: Double?; var fund_tna: Double? }
    struct Earned: Codable { var cauciones_month: Double?; var cauciones_total: Double?; var fund_unrealized: Double? }
    struct Caucion: Codable, Identifiable { var amount: Double; var rate: Double; var due: String; var days: Int; var interest: Double; var filled: Bool
        var id: String { "\(due)-\(amount)-\(rate)" } }
    struct Fund: Codable { var symbol: String?; var name: String?; var value: Double?; var tna: Double?; var pending: Double? }
    struct Action: Codable, Identifiable { var time: String; var kind: String; var term: String; var rate: Double?; var amount: Double
        var status: String; var ok: Bool; var why: String; var error: String?
        var id: String { "\(time)-\(kind)-\(amount)-\(status)" } }
    struct Rate: Codable, Identifiable { var bdays: Int; var days: Int?; var take: Double?; var offer: Double?; var ref: Double?; var ref_source: String?
        var id: Int { bdays } }
    var at: String?
    var market: String?
    var mode: String?
    var day: Day?
    var money: Money?
    var yield: Yield?
    var earned: Earned?
    var cauciones: [Caucion]?
    var fund: Fund?
    var today: [Action]?
    var rates: [Rate]?
    var summary: String?
    var demo: Bool?
}

// MARK: - store

@MainActor
final class Store: ObservableObject {
    @Published var data: Status?
    @Published var busy: String?
    @Published var note: String?
    @AppStorage("hideAmounts") var hide = false
    let file: URL
    private var timer: Timer?

    static func defaultStatusFile() -> URL {
        if let custom = UserDefaults.standard.string(forKey: "statusFile"), !custom.isEmpty {
            return URL(fileURLWithPath: (custom as NSString).expandingTildeInPath)
        }
        let env = ProcessInfo.processInfo.environment
        if let dir = env["CAUCIONETTA_STATE_DIR"], !dir.isEmpty {
            return URL(fileURLWithPath: (dir as NSString).expandingTildeInPath).appendingPathComponent("status.json")
        }
        let base = env["XDG_STATE_HOME"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/state")
        return base.appendingPathComponent("caucionetta/status.json")
    }

    /// The `caucionetta` executable: the `caucionettaPath` setting, or the usual install locations.
    static func caucionettaPath() -> String? {
        if let p = UserDefaults.standard.string(forKey: "caucionettaPath"), !p.isEmpty { return (p as NSString).expandingTildeInPath }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return ["\(home)/.local/bin/caucionetta", "/opt/homebrew/bin/caucionetta", "/usr/local/bin/caucionetta"]
            .first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    init(file: URL? = nil, live: Bool = true) {
        self.file = file ?? Store.defaultStatusFile()
        reload()
        if live {
            timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
                Task { @MainActor in self?.reload() }
            }
        }
    }

    func reload() {
        guard let d = try? Data(contentsOf: file), let s = try? JSONDecoder().decode(Status.self, from: d) else { return }
        data = s
    }

    /// Runs `caucionetta <args>` off the main thread; the last output line (or its JSON `message`) becomes the note.
    func run(_ args: [String], label: String) {
        guard let exe = Store.caucionettaPath() else {
            note = "No encuentro `caucionetta`: instalalo o configurá caucionettaPath."
            return
        }
        busy = label
        Task.detached {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: exe)
            p.arguments = args
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
            env["PYTHONUTF8"] = "1"
            p.environment = env
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            var out = ""
            do {
                try p.run()
                out = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                p.waitUntilExit()
            } catch { out = error.localizedDescription }
            let last = out.split(separator: "\n").last.map(String.init) ?? ""
            var msg = last
            if let d = last.data(using: .utf8), let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                msg = (o["message"] as? String) ?? last
            }
            let shown = msg
            await MainActor.run {
                self.busy = nil
                self.note = shown.isEmpty ? nil : String(shown.prefix(140))
                self.reload()
            }
        }
    }
}

// MARK: - formatting

enum Fmt {
    static let money: NumberFormatter = {
        let f = NumberFormatter()
        f.locale = Locale(identifier: "es_AR")
        f.numberStyle = .decimal
        f.maximumFractionDigits = 0
        return f
    }()
    static func ars(_ v: Double?, hide: Bool) -> String {
        guard let v else { return "—" }
        if hide { return "$ •••" }
        return "$ " + (money.string(from: NSNumber(value: v.rounded())) ?? "\(Int(v))")
    }
    static func pct(_ v: Double?) -> String {
        guard let v else { return "—" }
        return String(format: "%.1f%%", v).replacingOccurrences(of: ".", with: ",")
    }
    static func signed(_ v: Double?) -> String {
        guard let v else { return "—" }
        return (v >= 0 ? "+" : "") + pct(v)
    }
    static func status(_ s: String) -> String {
        ["sent": "enviada", "dry": "simulada", "error": "rechazada", "skipped": "salteada", "unknown": "sin respuesta",
         "planned": "pensada"][s] ?? s
    }
    static func due(_ iso: String) -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        guard let d = f.date(from: iso) else { return iso }
        let o = DateFormatter()
        o.locale = Locale(identifier: "es_AR")
        o.dateFormat = "EEE d/M"
        return o.string(from: d)
    }
    static func hhmm(_ iso: String?) -> String {
        guard let iso, iso.count >= 16 else { return "—" }
        let s = iso.index(iso.startIndex, offsetBy: 11)
        return String(iso[s..<iso.index(s, offsetBy: 5)])
    }
}

// MARK: - views

struct Card<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
            content
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.05)))
    }
}

struct Split: View {
    let parts: [(Double, Color)]
    var body: some View {
        GeometryReader { g in
            let total = max(parts.map(\.0).reduce(0, +), 1)
            HStack(spacing: 2) {
                ForEach(Array(parts.enumerated()), id: \.offset) { _, p in
                    if p.0 > 0 {
                        RoundedRectangle(cornerRadius: 3).fill(p.1)
                            .frame(width: max(4, (g.size.width - 4) * p.0 / total))
                    }
                }
            }
        }
        .frame(height: 8)
    }
}

struct HeightKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

struct Panel: View {
    @ObservedObject var store: Store
    var scroll = true
    @State private var confirmStop = false

    /// The content's real height: a ScrollView has none of its own, so a menu bar window would open at its minimum.
    /// The window takes the content's height and only scrolls past the screen.
    @State private var contentHeight: CGFloat = 680

    var body: some View {
        Group {
            if scroll {
                ScrollView {
                    content.background(GeometryReader { g in
                        Color.clear.preference(key: HeightKey.self, value: g.size.height)
                    })
                }
                .frame(height: min(contentHeight, (NSScreen.main?.visibleFrame.height ?? 900) - 40))
                .onPreferenceChange(HeightKey.self) { h in if h > 0 { contentHeight = h } }
            } else {
                content
            }
        }
        .frame(width: 370)
        .alert("¿Frenar el bot y cancelar lo pendiente de hoy?", isPresented: $confirmStop) {
            Button("Frenar", role: .destructive) { store.run(["--json", "bot", "stop"], label: "Frenando…") }
            Button("No", role: .cancel) {}
        } message: {
            Text("Cancela las cauciones sin llenar y las suscripciones al fondo de hoy. Después se retoma con «Reanudar».")
        }
    }

    var content: some View {
        let d = store.data
        let hide = store.hide
        return VStack(alignment: .leading, spacing: 10) {
            header(d)
            if d == nil {
                Text("Todavía no hay datos. El bot los escribe en su próxima pasada; o tocá «Actualizar».")
                    .font(.callout).foregroundStyle(.secondary)
            } else {
                totals(d!, hide)
                working(d!, hide)
                todayCard(d!, hide)
                ratesCard(d!)
            }
            footer(d)
        }
        .padding(12)
    }

    func header(_ d: Status?) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Image(systemName: "chart.line.uptrend.xyaxis").foregroundStyle(.green)
            Text("Cauciones").font(.headline)
            if d?.demo == true { Text("DEMO").font(.caption2.weight(.bold)).foregroundStyle(.orange) }
            Spacer()
            let mode = d?.mode ?? "—"
            Text(mode)
                .font(.caption.weight(.medium))
                .padding(.horizontal, 7).padding(.vertical, 2)
                .background(Capsule().fill(mode == "en vivo" ? Color.green.opacity(0.18) : Color.orange.opacity(0.22)))
            Button { store.hide.toggle() } label: { Image(systemName: store.hide ? "eye.slash" : "eye") }
                .buttonStyle(.plain).help(store.hide ? "Mostrar montos" : "Ocultar montos")
        }
    }

    func totals(_ d: Status, _ hide: Bool) -> some View {
        let m = d.money
        let y = d.yield
        return Card(title: "Tu plata") {
            HStack(alignment: .firstTextBaseline) {
                Text(Fmt.ars(m?.total, hide: hide)).font(.system(size: 26, weight: .semibold, design: .rounded))
                Spacer()
                VStack(alignment: .trailing, spacing: 0) {
                    if let tna = y?.working_tna {
                        Text(Fmt.pct(tna)).font(.title3.weight(.semibold)).foregroundStyle(.green)
                        Text("anual, lo que trabaja").font(.caption2).foregroundStyle(.secondary)
                    } else {
                        Text("nada trabajando todavía").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            Split(parts: [(m?.in_caucion ?? 0, .blue), (m?.in_fund ?? 0, .teal), (m?.free ?? 0, .gray.opacity(0.5))])
            HStack(spacing: 12) {
                legend(.blue, "Caución", Fmt.ars(m?.in_caucion, hide: hide))
                legend(.teal, "Fondo", Fmt.ars(m?.in_fund, hide: hide))
                legend(.gray.opacity(0.5), "Libre", Fmt.ars(m?.free, hide: hide))
            }
            Divider()
            HStack {
                stat("Por día", Fmt.ars(y?.per_day, hide: hide))
                stat("En 30 días", Fmt.ars(y?.month_projection, hide: hide))
                stat("Ganado este mes", Fmt.ars((d.earned?.cauciones_month ?? 0) , hide: hide))
            }
            if let total = m?.total, let pd = y?.per_day, pd > 0 {
                Text("Mañana esperás \(Fmt.ars(total + pd, hide: hide)) (+\(Fmt.ars(pd, hide: hide)))"
                     + ((d.fund?.value ?? 0) > 0 ? " · el fondo, según sus últimos 14 días" : ""))
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let e = d.earned, (e.cauciones_total ?? 0) != 0 || (e.fund_unrealized ?? 0) != 0 {
                Text("Desde el inicio: \(Fmt.ars(e.cauciones_total, hide: hide)) en cauciones · \(Fmt.ars(e.fund_unrealized, hide: hide)) en el fondo")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    func legend(_ c: Color, _ name: String, _ v: String) -> some View {
        HStack(spacing: 4) {
            Circle().fill(c).frame(width: 7, height: 7)
            VStack(alignment: .leading, spacing: 0) {
                Text(name).font(.caption2).foregroundStyle(.secondary)
                Text(v).font(.caption.monospacedDigit())
            }
        }
    }

    func stat(_ name: String, _ v: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(name).font(.caption2).foregroundStyle(.secondary)
            Text(v).font(.callout.weight(.medium).monospacedDigit())
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    func working(_ d: Status, _ hide: Bool) -> some View {
        Card(title: "Trabajando ahora") {
            let cs = d.cauciones ?? []
            if cs.isEmpty && (d.fund?.value ?? 0) <= 0 {
                Text("Nada colocado en este momento.").font(.callout).foregroundStyle(.secondary)
            }
            ForEach(cs) { c in
                HStack {
                    Image(systemName: c.filled ? "lock.fill" : "hourglass").foregroundStyle(c.filled ? .blue : .orange)
                        .help(c.filled ? "Colocada" : "Esperando tomador")
                    VStack(alignment: .leading, spacing: 0) {
                        Text("\(Fmt.ars(c.amount, hide: hide)) al \(Fmt.pct(c.rate))").font(.callout.monospacedDigit())
                        Text("vence \(Fmt.due(c.due)) · \(c.days) día\(c.days == 1 ? "" : "s")").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text("+" + Fmt.ars(c.interest, hide: hide)).font(.callout.monospacedDigit()).foregroundStyle(.green)
                }
            }
            if let f = d.fund, (f.value ?? 0) > 0 {
                HStack {
                    Image(systemName: "parkingsign.circle.fill").foregroundStyle(.teal)
                    VStack(alignment: .leading, spacing: 0) {
                        Text("\(Fmt.ars(f.value, hide: hide)) en el fondo").font(.callout.monospacedDigit())
                        Text((f.pending ?? 0) > 0 ? "en camino: IOL procesa la suscripción a la noche" : (f.name ?? f.symbol ?? "money market"))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(Fmt.pct(f.tna)).font(.callout.monospacedDigit()).foregroundStyle(.teal)
                }
            }
        }
    }

    func todayCard(_ d: Status, _ hide: Bool) -> some View {
        Card(title: "Hoy") {
            let acts = d.today ?? []
            if acts.isEmpty {
                Text("Sin movimientos todavía.").font(.callout).foregroundStyle(.secondary)
            }
            ForEach(acts) { a in
                HStack(alignment: .top) {
                    Text(a.time).font(.caption.monospacedDigit()).foregroundStyle(.secondary).frame(width: 38, alignment: .leading)
                    VStack(alignment: .leading, spacing: 1) {
                        Text([a.kind, a.term, a.rate.map { "al " + Fmt.pct($0) } ?? ""].filter { !$0.isEmpty }.joined(separator: " · "))
                            .font(.callout)
                        Text(a.error ?? a.why).font(.caption).foregroundStyle(a.error == nil ? Color.secondary : Color.red).lineLimit(2)
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 1) {
                        Text(Fmt.ars(a.amount, hide: hide)).font(.caption.monospacedDigit())
                        Text(a.status).font(.caption2).foregroundStyle(a.ok ? Color.green : Color.orange)
                    }
                }
            }
            if let s = d.summary { Text(s).font(.caption).foregroundStyle(.secondary) }
        }
    }

    func ratesCard(_ d: Status) -> some View {
        Card(title: "Tasas ahora") {
            let day = d.day
            Text("\(d.market == "abierto" ? "Mercado abierto" : (d.market ?? "—").capitalized) · día \(day?.kind ?? "—")"
                 + (day?.final_at.map { " · coloca a las \($0)" } ?? ""))
                .font(.caption).foregroundStyle(day?.high == true ? Color.orange : Color.secondary)
            let rs = d.rates ?? []
            if rs.isEmpty {
                Text("Sin lecturas hoy.").font(.callout).foregroundStyle(.secondary)
            }
            ForEach(rs) { r in
                HStack {
                    Text("\(r.bdays) día\(r.bdays == 1 ? "" : "s") hábil\(r.bdays == 1 ? "" : "es")").font(.callout)
                    if let days = r.days, days != r.bdays {
                        Text("(\(days) corridos)").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(Fmt.pct(r.take)).font(.callout.monospacedDigit().weight(.medium))
                        .foregroundStyle((r.take ?? 0) >= (r.ref ?? .infinity) * 1.5 ? Color.orange : Color.primary)
                    Text("ref. \(Fmt.pct(r.ref))").font(.caption.monospacedDigit()).foregroundStyle(.secondary).frame(width: 74, alignment: .trailing)
                }
            }
            if let f = d.yield?.fund_tna {
                HStack {
                    Text("Fondo (14 días)").font(.callout)
                    Spacer()
                    Text(Fmt.pct(f)).font(.callout.monospacedDigit()).foregroundStyle(.teal)
                    Text("").frame(width: 74)
                }
            }
        }
    }

    func footer(_ d: Status?) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Button { store.run(["--json", "status", "--refresh"], label: "Actualizando…") } label: { Label("Actualizar", systemImage: "arrow.clockwise") }
                if d?.mode == "pausado" {
                    Button { store.run(["--json", "bot", "resume"], label: "Reanudando…") } label: { Label("Reanudar", systemImage: "play.fill") }
                } else {
                    Button { store.run(["--json", "bot", "pause"], label: "Pausando…") } label: { Label("Pausar", systemImage: "pause.fill") }
                }
                Button(role: .destructive) { confirmStop = true } label: { Label("Frenar", systemImage: "stop.fill") }
                Spacer()
                Button { NSApp.terminate(nil) } label: { Image(systemName: "power") }.buttonStyle(.plain).help("Salir")
            }
            .controlSize(.small)
            Text(store.busy ?? store.note ?? "Actualizado a las \(Fmt.hhmm(d?.at))"
                 + (d?.money?.money_at.map { " · saldo de las \(Fmt.hhmm($0))" } ?? ""))
                .font(.caption2).foregroundStyle(.secondary)
        }
    }
}

// MARK: - app

@main
struct CaucionettaBarApp: App {
    @StateObject private var store: Store

    init() {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--snapshot"), i + 1 < args.count {
            let out = URL(fileURLWithPath: args[i + 1])
            let fixture = args.firstIndex(of: "--fixture").flatMap { $0 + 1 < args.count ? URL(fileURLWithPath: args[$0 + 1]) : nil }
            MainActor.assumeIsolated {
                CaucionettaBarApp.snapshot(out: out, fixture: fixture, dark: args.contains("--dark"))
            }
            exit(0)
        }
        _store = StateObject(wrappedValue: Store())
    }

    @MainActor static func snapshot(out: URL, fixture: URL?, dark: Bool) {
        let store = Store(file: fixture, live: false)
        let view = Panel(store: store, scroll: false)
            .background(dark ? Color(white: 0.12) : Color(white: 0.97))
            .environment(\.colorScheme, dark ? .dark : .light)
        let r = ImageRenderer(content: view)
        r.scale = 2
        guard let img = r.nsImage, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else {
            FileHandle.standardError.write("no pude renderizar\n".data(using: .utf8)!)
            return
        }
        try? png.write(to: out)
    }

    var body: some Scene {
        MenuBarExtra {
            Panel(store: store).onAppear { store.reload() }
        } label: {
            let tna = store.data?.yield?.working_tna
            HStack(spacing: 3) {
                Image(systemName: "chart.line.uptrend.xyaxis")
                if let tna { Text(Fmt.pct(tna)) }
            }
        }
        .menuBarExtraStyle(.window)
    }
}
