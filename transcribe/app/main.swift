// 批量转逐字稿：原生 macOS app。把视频/文件夹拖进窗口（或拖到 Dock 图标、⌘O 添加）→「开始转写」→
// 左边每个文件的状态和进度，右边预览选中文件的简体逐字稿。
// 转写本身不在这里做：调用同项目的 transcribe.py --events，读它输出的 @@JSON 行更新界面。
// 这样命令行和 App 共用一套已验证的转写逻辑（VAD 余量、OpenCC、判重、同名处理都在 Python 里）。
//
// 命令行自测（不显示窗口）：
//   VideoTranscribe --snapshot <out.png> [--run] [--stop-after <秒>] <视频或文件夹...>
//   加入文件 →（--run 时先跑完整批；--stop-after 到点模拟按「停止」）→ 把窗口渲染成 PNG，并把每个文件的最终状态打印到 stdout
import Cocoa
import UniformTypeIdentifiers

// 源码目录由 build.sh 写进 Info.plist（VTProjectDir），App 装到别处也能找到 transcribe.py
let projectDir = Bundle.main.object(forInfoDictionaryKey: "VTProjectDir") as? String ?? ""
let scriptPath = projectDir + "/transcribe.py"
// VT_OUT_DIR 只给自测用：测试产物不进真实的 out/
let outDir = ProcessInfo.processInfo.environment["VT_OUT_DIR"] ?? projectDir + "/out"
// 与 transcribe.py 的 MEDIA_EXT 保持一致：App 先展开文件夹，列表里看到的就是实际要转的文件
let mediaExt: Set<String> = ["mp4", "mov", "m4v", "mkv", "avi", "webm", "flv", "wmv", "ts",
                             "mp3", "m4a", "wav", "aac", "flac", "ogg", "opus", "aiff"]

// MARK: 配色（审美库 tokens.css 的 --dark-*：小工具始终暗色，朱是唯一强调色）

func rgb(_ v: UInt32) -> NSColor {
    NSColor(red: CGFloat(v >> 16 & 0xFF) / 255, green: CGFloat(v >> 8 & 0xFF) / 255,
            blue: CGFloat(v & 0xFF) / 255, alpha: 1)
}

enum Palette {
    static let bg = rgb(0x1B1A17)      // 窗口底
    static let raised = rgb(0x22201D)  // 选中行
    static let field = rgb(0x24221E)   // 预览框，比底亮一档
    static let soft = rgb(0x2A2824)    // 进度槽
    static let line = rgb(0x3A3731)    // 分隔线
    static let ink = rgb(0xE9E4D8)     // 正文
    static let mute = rgb(0xA59F92)    // 次要文字
    static let shu = rgb(0xD0644C)     // 朱：主按钮、失败
    static let bamboo = rgb(0x9DAE7A)  // 完成
    static let fuji = rgb(0xA394BE)    // 进行中
    static let kohaku = rgb(0xD8AA78)  // 待处理
}

// MARK: 数据

enum ItemState {
    case pending, running, ok, skipped, failed
    var color: NSColor {
        switch self {
        case .pending: return Palette.kohaku
        case .running: return Palette.fuji
        case .ok: return Palette.bamboo
        case .skipped: return Palette.mute
        case .failed: return Palette.shu
        }
    }
}

final class Item {
    let url: URL
    var state = ItemState.pending
    var pct = 0
    var note = "待转写"
    var name: String?  // 输出文件夹名，由 transcribe.py 决定；文件夹里是 封面.jpg / 视频 / 逐字稿.md / 逐字稿.txt / 字幕.srt
    init(url: URL) { self.url = url }
    var folderURL: URL? { name.map { URL(fileURLWithPath: "\(outDir)/\($0)") } }
    var txtURL: URL? { folderURL?.appendingPathComponent("逐字稿.txt") }
    var mdURL: URL? { folderURL?.appendingPathComponent("逐字稿.md") }
}

/// 拖入的文件夹递归展开成音视频文件；隐藏文件跳过；按路径排序，和 Python 端顺序一致。
func expand(_ urls: [URL]) -> [URL] {
    var out: [URL] = []
    for url in urls {
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir) else { continue }
        if isDir.boolValue {
            let e = FileManager.default.enumerator(at: url, includingPropertiesForKeys: nil,
                                                   options: [.skipsHiddenFiles, .skipsPackageDescendants])
            var found: [URL] = []
            // 注意不能写成 while let … where：遇到第一个非音视频文件就会提前结束遍历
            while let f = e?.nextObject() as? URL {
                if mediaExt.contains(f.pathExtension.lowercased()) { found.append(f) }
            }
            out += found.sorted { $0.path < $1.path }
        } else if mediaExt.contains(url.pathExtension.lowercased()) {
            out.append(url)
        }
    }
    return out
}

// MARK: 视图零件

/// 暗底基础视图。macOS 14+ 的 NSView 默认不裁剪到 bounds，必须自己截，否则会刷掉相邻视图（图片转文字踩过）。
class DarkView: NSView {
    var fill = Palette.bg
    override func draw(_ dirtyRect: NSRect) {
        fill.setFill()
        dirtyRect.intersection(bounds).fill()
    }
}

final class Dot: NSView {
    var color = Palette.kohaku { didSet { needsDisplay = true } }
    override func draw(_ dirtyRect: NSRect) {
        color.setFill()
        NSBezierPath(ovalIn: bounds.insetBy(dx: 1, dy: 1)).fill()
    }
}

/// 细进度条：槽是 soft，填充是 fuji；圆头。
final class ProgressLine: NSView {
    var value: CGFloat = 0 { didSet { needsDisplay = true } }
    override func draw(_ dirtyRect: NSRect) {
        let r = bounds.height / 2
        Palette.soft.setFill()
        NSBezierPath(roundedRect: bounds, xRadius: r, yRadius: r).fill()
        guard value > 0 else { return }
        var f = bounds
        f.size.width = max(bounds.height, bounds.width * min(value, 1))
        Palette.fuji.setFill()
        NSBezierPath(roundedRect: f, xRadius: r, yRadius: r).fill()
    }
}

func makeLabel(_ text: String, size: CGFloat = 12, color: NSColor = Palette.mute) -> NSTextField {
    let l = NSTextField(labelWithString: text)
    l.font = NSFont.systemFont(ofSize: size)
    l.textColor = color
    l.lineBreakMode = .byTruncatingMiddle
    l.translatesAutoresizingMaskIntoConstraints = false
    return l
}

func makeButton(_ title: String, _ target: AnyObject, _ action: Selector, primary: Bool = false) -> NSButton {
    let b = NSButton(title: title, target: target, action: action)
    b.bezelStyle = .rounded
    if primary { setPrimaryTitle(b, title) }
    return b
}

/// 朱色主按钮：bezelColor 上的字要手动设成白色，否则暗色外观下发灰
func setPrimaryTitle(_ b: NSButton, _ title: String) {
    b.bezelColor = Palette.shu
    b.attributedTitle = NSAttributedString(string: title, attributes: [
        .foregroundColor: NSColor.white, .font: NSFont.systemFont(ofSize: 13, weight: .medium)])
}

/// 列表里的一行：状态点 + 文件名 + 说明，转写中时下面多一条进度。
final class ItemCell: NSTableCellView {
    let dot = Dot()
    let title = makeLabel("", size: 13, color: Palette.ink)
    let note = makeLabel("", size: 11)
    let bar = ProgressLine()

    init() {
        super.init(frame: .zero)
        for v in [dot, title, note, bar] as [NSView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            addSubview(v)
        }
        note.lineBreakMode = .byTruncatingTail
        NSLayoutConstraint.activate([
            dot.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            dot.centerYAnchor.constraint(equalTo: title.centerYAnchor),
            dot.widthAnchor.constraint(equalToConstant: 9), dot.heightAnchor.constraint(equalToConstant: 9),
            title.leadingAnchor.constraint(equalTo: dot.trailingAnchor, constant: 10),
            title.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -14),
            title.topAnchor.constraint(equalTo: topAnchor, constant: 10),
            note.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            note.trailingAnchor.constraint(equalTo: title.trailingAnchor),
            note.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 3),
            bar.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            bar.trailingAnchor.constraint(equalTo: title.trailingAnchor),
            bar.topAnchor.constraint(equalTo: note.bottomAnchor, constant: 6),
            bar.heightAnchor.constraint(equalToConstant: 4),
        ])
    }
    required init?(coder: NSCoder) { fatalError() }

    func show(_ it: Item) {
        dot.color = it.state.color
        title.stringValue = it.url.lastPathComponent
        note.stringValue = it.state == .running ? "转写中 \(it.pct)%" : it.note
        note.textColor = it.state == .failed ? Palette.shu : Palette.mute
        bar.isHidden = it.state != .running
        bar.value = CGFloat(it.pct) / 100
        toolTip = it.url.path
    }
}

/// 选中行：比底亮一档的圆角块 + 左侧朱色细条，不用系统蓝。
final class ItemRowView: NSTableRowView {
    override func drawSelection(in dirtyRect: NSRect) {
        let r = bounds.insetBy(dx: 6, dy: 2)
        Palette.raised.setFill()
        NSBezierPath(roundedRect: r, xRadius: 8, yRadius: 8).fill()
        Palette.shu.setFill()
        NSBezierPath(roundedRect: NSRect(x: r.minX, y: r.minY + 10, width: 3, height: r.height - 20),
                     xRadius: 1.5, yRadius: 1.5).fill()
    }
    override var isEmphasized: Bool { get { true } set {} }
}

/// 列表：Delete 键移出选中项（转写中的不动）。
final class ItemTable: NSTableView {
    var onDelete: (() -> Void)?
    override func keyDown(with event: NSEvent) {
        if event.keyCode == 51 || event.keyCode == 117 { onDelete?() } else { super.keyDown(with: event) }
    }
}

/// 整个左栏都接收拖入（文件或文件夹）。
final class DropView: DarkView {
    var onDrop: (([URL]) -> Void)?
    var highlight = false { didSet { needsDisplay = true } }
    override init(frame: NSRect) {
        super.init(frame: frame)
        registerForDraggedTypes([.fileURL])
    }
    required init?(coder: NSCoder) { fatalError() }
    private func urls(_ info: NSDraggingInfo) -> [URL] {
        info.draggingPasteboard.readObjects(forClasses: [NSURL.self],
                                            options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
    }
    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        let ok = !expand(urls(sender)).isEmpty
        highlight = ok
        return ok ? .copy : []
    }
    override func draggingExited(_ sender: NSDraggingInfo?) { highlight = false }
    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        highlight = false
        onDrop?(urls(sender))
        return true
    }
    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard highlight else { return }
        // 拖入时画朱色虚线圆角框，提示「放这里」
        let p = NSBezierPath(roundedRect: bounds.insetBy(dx: 10, dy: 10), xRadius: 18, yRadius: 18)
        p.lineWidth = 2
        p.setLineDash([8, 6], count: 2, phase: 0)
        Palette.shu.setStroke()
        p.stroke()
    }
}

/// 顶栏 / 底栏：左边一个视图，右边一排按钮。
func makeBar(left: NSView, right: [NSView], height: CGFloat = 48) -> NSView {
    let bar = DarkView()
    bar.translatesAutoresizingMaskIntoConstraints = false
    let stack = NSStackView(views: right)
    stack.spacing = 8
    stack.translatesAutoresizingMaskIntoConstraints = false
    left.translatesAutoresizingMaskIntoConstraints = false
    bar.addSubview(left)
    bar.addSubview(stack)
    left.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    NSLayoutConstraint.activate([
        bar.heightAnchor.constraint(equalToConstant: height),
        left.leadingAnchor.constraint(equalTo: bar.leadingAnchor, constant: 16),
        left.centerYAnchor.constraint(equalTo: bar.centerYAnchor),
        left.trailingAnchor.constraint(lessThanOrEqualTo: stack.leadingAnchor, constant: -10),
        stack.trailingAnchor.constraint(equalTo: bar.trailingAnchor, constant: -12),
        stack.centerYAnchor.constraint(equalTo: bar.centerYAnchor),
    ])
    return bar
}

func separator() -> NSView {
    let v = DarkView()
    v.fill = Palette.line
    v.translatesAutoresizingMaskIntoConstraints = false
    v.heightAnchor.constraint(equalToConstant: 1).isActive = true
    return v
}

/// 竖向叠放：顶栏、分隔线、内容、（可选）分隔线 + 底栏。
func stackVertically(_ views: [NSView]) -> NSView {
    let box = DarkView()
    var prev: NSView?
    for v in views {
        v.translatesAutoresizingMaskIntoConstraints = false
        box.addSubview(v)
        NSLayoutConstraint.activate([
            v.leadingAnchor.constraint(equalTo: box.leadingAnchor),
            v.trailingAnchor.constraint(equalTo: box.trailingAnchor),
            v.topAnchor.constraint(equalTo: prev?.bottomAnchor ?? box.topAnchor),
        ])
        prev = v
    }
    prev?.bottomAnchor.constraint(equalTo: box.bottomAnchor).isActive = true
    return box
}

// MARK: App

final class AppDelegate: NSObject, NSApplicationDelegate, NSTableViewDataSource, NSTableViewDelegate {
    var window: NSWindow!
    var items: [Item] = []
    let table = ItemTable()
    let dropView = DropView()
    let emptyHint = NSTextField(wrappingLabelWithString: "把视频或文件夹拖进来")
    let emptySub = NSTextField(wrappingLabelWithString: "也可以拖到 Dock 图标上，或按 ⌘O 添加\n文件夹会自动找出里面所有音视频")
    let status = makeLabel("拖入视频后点「开始转写」", size: 12)
    let langPopup = NSPopUpButton()
    var startButton: NSButton!
    var addButton: NSButton!
    var clearButton: NSButton!
    let previewTitle = makeLabel("逐字稿预览", size: 12)
    let preview = NSTextView()
    var copyButton: NSButton!
    var revealButton: NSButton!
    let langCodes = ["zh", "auto", "en"]

    var proc: Process?
    var lineBuffer = Data()
    var stderrText = ""
    var gotSummary = false
    var stopping = false
    var pendingOpen: [URL] = []
    var onBatchFinished: (() -> Void)?  // 自测用

    // MARK: 启动

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let left = buildList()
        let right = buildPreview()

        let split = NSSplitView()
        split.isVertical = true
        split.dividerStyle = .thin
        split.addArrangedSubview(left)
        split.addArrangedSubview(right)
        NSLayoutConstraint.activate([
            left.widthAnchor.constraint(greaterThanOrEqualToConstant: 300),
            right.widthAnchor.constraint(greaterThanOrEqualToConstant: 320),
        ])

        langPopup.addItems(withTitles: ["普通话", "自动识别", "英语"])
        langPopup.toolTip = "视频里说的语言。普通话会转成简体；外语输出原文，不翻译"
        startButton = makeButton("开始转写", self, #selector(toggleRun(_:)), primary: true)
        addButton = makeButton("添加…", self, #selector(addFiles(_:)))
        clearButton = makeButton("清空列表", self, #selector(clearList(_:)))
        let top = makeBar(left: status, right: [langPopup, clearButton, addButton, startButton])

        let outLabel = makeLabel("每个视频一个文件夹，在 \(outDir)（封面 / 视频 / 逐字稿.md 带时间戳 / .txt / .srt）", size: 11)
        let footer = makeBar(left: outLabel, right: [makeButton("打开输出文件夹", self, #selector(openOutDir(_:)))],
                             height: 40)

        let root = stackVertically([top, separator(), split, separator(), footer])

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 640),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "批量转逐字稿"
        window.minSize = NSSize(width: 720, height: 420)
        window.appearance = NSAppearance(named: .darkAqua)  // 界面固定暗色，标题栏和系统控件跟着暗
        window.backgroundColor = Palette.bg
        window.contentView = root
        window.center()
        window.setFrameAutosaveName("main")

        split.layoutSubtreeIfNeeded()
        split.setPosition(window.frame.width * 0.42, ofDividerAt: 0)
        refresh()

        if !runSelfTestIfRequested() {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        if !pendingOpen.isEmpty { add(pendingOpen); pendingOpen = [] }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    /// 退出时把还在跑的转写一起停掉，否则 whisper 会在后台继续占着 GPU
    func applicationWillTerminate(_ note: Notification) { killProcessTree() }

    /// 拖到 Dock 图标上 / 「打开方式」选本 App
    func application(_ application: NSApplication, open urls: [URL]) {
        if window == nil { pendingOpen += urls } else { add(urls) }
    }

    // MARK: 左栏：文件列表

    func buildList() -> NSView {
        let col = NSTableColumn(identifier: .init("item"))
        table.addTableColumn(col)
        table.headerView = nil
        table.backgroundColor = Palette.bg
        table.rowHeight = 62
        table.intercellSpacing = .zero
        table.selectionHighlightStyle = .regular
        table.allowsMultipleSelection = true
        table.dataSource = self
        table.delegate = self
        table.target = self
        table.doubleAction = #selector(openSelectedMarkdown(_:))
        table.onDelete = { [weak self] in self?.removeSelected(nil) }
        table.menu = buildRowMenu()
        // 列表本身不当拖拽目标，交给外层 DropView，整块左栏都能放
        table.unregisterDraggedTypes()

        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.unregisterDraggedTypes()
        scroll.contentView.unregisterDraggedTypes()

        emptyHint.font = NSFont(name: "Songti SC", size: 20) ?? NSFont.systemFont(ofSize: 20)
        emptyHint.textColor = Palette.ink
        emptySub.font = NSFont.systemFont(ofSize: 12)
        emptySub.textColor = Palette.mute
        for l in [emptyHint, emptySub] {
            l.alignment = .center
            l.translatesAutoresizingMaskIntoConstraints = false
        }

        dropView.onDrop = { [weak self] urls in self?.add(urls) }
        for v in [scroll, emptyHint, emptySub] as [NSView] { dropView.addSubview(v) }
        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: dropView.topAnchor, constant: 6),
            scroll.bottomAnchor.constraint(equalTo: dropView.bottomAnchor, constant: -6),
            scroll.leadingAnchor.constraint(equalTo: dropView.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: dropView.trailingAnchor),
            emptyHint.centerXAnchor.constraint(equalTo: dropView.centerXAnchor),
            emptyHint.centerYAnchor.constraint(equalTo: dropView.centerYAnchor, constant: -16),
            emptySub.centerXAnchor.constraint(equalTo: dropView.centerXAnchor),
            emptySub.topAnchor.constraint(equalTo: emptyHint.bottomAnchor, constant: 10),
            emptySub.leadingAnchor.constraint(greaterThanOrEqualTo: dropView.leadingAnchor, constant: 20),
        ])
        return dropView
    }

    func buildRowMenu() -> NSMenu {
        let m = NSMenu()
        m.addItem(withTitle: "打开逐字稿（.md）", action: #selector(openSelectedMarkdown(_:)), keyEquivalent: "").target = self
        m.addItem(withTitle: "在 Finder 中显示输出文件夹", action: #selector(revealSelected(_:)), keyEquivalent: "").target = self
        m.addItem(withTitle: "在 Finder 中显示原视频", action: #selector(revealSource(_:)), keyEquivalent: "").target = self
        m.addItem(.separator())
        m.addItem(withTitle: "移出列表", action: #selector(removeSelected(_:)), keyEquivalent: "").target = self
        return m
    }

    func numberOfRows(in tableView: NSTableView) -> Int { items.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let cell = (tableView.makeView(withIdentifier: .init("cell"), owner: nil) as? ItemCell) ?? {
            let c = ItemCell()
            c.identifier = .init("cell")
            return c
        }()
        cell.show(items[row])
        return cell
    }

    func tableView(_ tableView: NSTableView, rowViewForRow row: Int) -> NSTableRowView? { ItemRowView() }

    func tableViewSelectionDidChange(_ note: Notification) { showPreview() }

    // MARK: 右栏：逐字稿预览

    func buildPreview() -> NSView {
        copyButton = makeButton("复制全文", self, #selector(copyPreview(_:)))
        revealButton = makeButton("在 Finder 中显示", self, #selector(revealSelected(_:)))
        let header = makeBar(left: previewTitle, right: [revealButton, copyButton], height: 44)

        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = true
        scroll.backgroundColor = Palette.field
        scroll.borderType = .noBorder
        preview.minSize = .zero
        preview.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        preview.isVerticallyResizable = true
        preview.isHorizontallyResizable = false
        preview.autoresizingMask = [.width]
        preview.textContainer?.widthTracksTextView = true
        preview.isEditable = false  // 只读预览：改稿去 .md/.txt 文件里改，免得以为在这里改了会保存
        preview.isSelectable = true
        preview.isRichText = false
        preview.drawsBackground = true
        preview.backgroundColor = Palette.field
        preview.textContainerInset = NSSize(width: 18, height: 16)
        preview.unregisterDraggedTypes()
        scroll.documentView = preview
        return stackVertically([header, separator(), scroll])
    }

    static let previewAttrs: [NSAttributedString.Key: Any] = {
        let p = NSMutableParagraphStyle()
        p.lineSpacing = 6
        p.paragraphSpacing = 12
        return [.font: NSFont.systemFont(ofSize: 15), .foregroundColor: Palette.ink, .paragraphStyle: p]
    }()

    func setPreview(_ text: String, dim: Bool = false) {
        var a = Self.previewAttrs
        if dim { a[.foregroundColor] = Palette.mute }
        preview.textStorage?.setAttributedString(NSAttributedString(string: text, attributes: a))
    }

    func showPreview() {
        let sel = table.selectedRowIndexes
        guard sel.count == 1, let row = sel.first, row < items.count else {
            previewTitle.stringValue = "逐字稿预览"
            setPreview(items.isEmpty ? "" : "在左边选一个已完成的文件，这里显示它的逐字稿。", dim: true)
            copyButton.isEnabled = false
            revealButton.isEnabled = false
            return
        }
        let it = items[row]
        previewTitle.stringValue = it.url.deletingPathExtension().lastPathComponent
        if let url = it.txtURL, let text = try? String(contentsOf: url, encoding: .utf8),
           it.state == .ok || it.state == .skipped {
            setPreview(text)
            let n = text.unicodeScalars.filter { (0x4E00...0x9FFF).contains($0.value) }.count
            previewTitle.stringValue += n > 0 ? "  ·  \(n) 字" : ""
            copyButton.isEnabled = true
            revealButton.isEnabled = true
        } else {
            switch it.state {
            case .failed: setPreview("转写失败：\(it.note)", dim: true)
            case .running: setPreview("正在转写，完成后这里显示逐字稿。", dim: true)
            default: setPreview("还没转写。点右上角「开始转写」。", dim: true)
            }
            copyButton.isEnabled = false
            revealButton.isEnabled = false
        }
    }

    // MARK: 列表增删

    func add(_ urls: [URL]) {
        guard !urls.isEmpty else { return }
        let known = Set(items.map { $0.url.path })
        let fresh = expand(urls).filter { !known.contains($0.path) }
        let skippedCount = urls.isEmpty ? 0 : max(0, expand(urls).count - fresh.count)
        guard !fresh.isEmpty else {
            status.stringValue = skippedCount > 0 ? "这些文件已经在列表里了" : "没找到音视频文件"
            return
        }
        items += fresh.map(Item.init)
        refresh()
        if proc == nil { status.stringValue = "已加入 \(fresh.count) 个文件，点「开始转写」" }
    }

    @objc func addFiles(_ sender: Any?) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        panel.allowedContentTypes = [.movie, .audio, .folder]
        panel.message = "选视频、音频或文件夹（文件夹会递归查找）"
        panel.beginSheetModal(for: window) { [weak self] resp in
            if resp == .OK { self?.add(panel.urls) }
        }
    }

    @objc func removeSelected(_ sender: Any?) {
        let rows = targetRows()
        let drop = Set(rows.filter { items[$0].state != .running })
        guard !drop.isEmpty else { return }
        items = items.enumerated().filter { !drop.contains($0.offset) }.map { $0.element }
        table.deselectAll(nil)
        refresh()
    }

    @objc func clearList(_ sender: Any?) {
        guard proc == nil else { return }
        items.removeAll()
        refresh()
        status.stringValue = "拖入视频后点「开始转写」"
    }

    /// 右键菜单作用于点中的那行；没有右键就用当前选中行
    func targetRows() -> [Int] {
        let clicked = table.clickedRow
        if clicked >= 0 && !table.selectedRowIndexes.contains(clicked) { return [clicked] }
        return Array(table.selectedRowIndexes)
    }

    func refresh() {
        let sel = table.selectedRowIndexes
        table.reloadData()
        table.selectRowIndexes(sel.filteredIndexSet { $0 < items.count }, byExtendingSelection: false)
        let empty = items.isEmpty
        emptyHint.isHidden = !empty
        emptySub.isHidden = !empty
        let running = proc != nil
        clearButton.isEnabled = !running && !empty
        langPopup.isEnabled = !running
        let todo = items.filter { $0.state == .pending || $0.state == .failed }.count
        setPrimaryTitle(startButton, running ? "停止" : (todo > 0 && items.contains { $0.state == .failed }
                                                         && !items.contains { $0.state == .pending } ? "重试失败的" : "开始转写"))
        startButton.isEnabled = running || todo > 0
        showPreview()
    }

    /// 只刷新一行，进度高频更新时不整表重载（否则选中和滚动会抖）
    func refreshRow(_ it: Item) {
        guard let row = items.firstIndex(where: { $0 === it }) else { return }
        (table.view(atColumn: 0, row: row, makeIfNecessary: false) as? ItemCell)?.show(it)
    }

    // MARK: 跑 transcribe.py

    @objc func toggleRun(_ sender: Any?) {
        if proc != nil { stop() } else { start() }
    }

    func start() {
        let todo = items.filter { $0.state == .pending || $0.state == .failed }
        guard proc == nil, !todo.isEmpty else { return }
        for it in todo { it.state = .pending; it.pct = 0; it.note = "排队中" }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["python3", "-u", scriptPath, "--events", "--lang", langCodes[langPopup.indexOfSelectedItem],
                       "-o", outDir] + todo.map { $0.url.path }
        // 从 Finder 启动的 App 拿不到终端的 PATH，brew 装的 ffmpeg/whisper-cli/opencc 都在 /opt/homebrew/bin
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
        env["PYTHONIOENCODING"] = "utf-8"
        p.environment = env
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        lineBuffer = Data()
        stderrText = ""
        gotSummary = false
        stopping = false
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            if !d.isEmpty { DispatchQueue.main.async { self?.consume(d) } }
        }
        err.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            if let s = String(data: d, encoding: .utf8), !s.isEmpty {
                DispatchQueue.main.async { self?.stderrText = String(((self?.stderrText ?? "") + s).suffix(2000)) }
            }
        }
        p.terminationHandler = { [weak self] p in
            // 先收掉管道里剩下的数据，再在主线程按顺序收尾
            out.fileHandleForReading.readabilityHandler = nil
            err.fileHandleForReading.readabilityHandler = nil
            let rest = out.fileHandleForReading.readDataToEndOfFile()
            let errRest = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            let code = p.terminationStatus
            DispatchQueue.main.async {
                guard let self else { return }
                if !rest.isEmpty { self.consume(rest) }
                self.stderrText += errRest
                self.finished(code)
            }
        }
        do {
            try p.run()
        } catch {
            status.stringValue = "启动失败：\(error.localizedDescription)"
            return
        }
        proc = p
        status.stringValue = "准备中…（第一个文件要先加载模型，几秒钟）"
        refresh()
    }

    func stop() {
        stopping = true
        status.stringValue = "正在停止…"
        killProcessTree()
    }

    /// 停止顺序有讲究：
    /// 1. 先记下 python 的子进程（ffmpeg/whisper-cli）——python 一退，它们就过继给 launchd，pgrep -P 找不到了
    /// 2. 先停 python：它收到 SIGTERM 会正常退出并清掉临时 wav；若先杀 whisper，python 会抢先报一条「失败」
    /// 3. 再杀第 1 步记下的子进程，否则 whisper 会在后台继续占着 GPU
    func killProcessTree() {
        guard let p = proc, p.isRunning else { return }
        let q = Process()
        let pipe = Pipe()
        q.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        q.arguments = ["-P", String(p.processIdentifier)]
        q.standardOutput = pipe
        try? q.run()
        q.waitUntilExit()
        let kids = (String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? "")
            .split(separator: "\n").compactMap { pid_t($0.trimmingCharacters(in: .whitespaces)) }
        p.terminate()
        for k in kids { kill(k, SIGTERM) }
    }

    func consume(_ data: Data) {
        lineBuffer.append(data)
        while let nl = lineBuffer.firstIndex(of: 0x0A) {
            let line = String(data: lineBuffer[lineBuffer.startIndex..<nl], encoding: .utf8) ?? ""
            lineBuffer.removeSubrange(lineBuffer.startIndex...nl)
            if line.hasPrefix("@@") { handle(event: String(line.dropFirst(2))) }
        }
    }

    func item(for src: String) -> Item? {
        items.first { $0.url.path == src } ?? items.first { $0.url.standardizedFileURL.path == src }
    }

    func handle(event json: String) {
        guard let d = json.data(using: .utf8),
              let ev = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any],
              let kind = ev["ev"] as? String else { return }
        let src = ev["src"] as? String ?? ""
        switch kind {
        case "start":
            guard !stopping, let it = item(for: src) else { return }
            it.state = .running
            it.pct = 0
            it.name = ev["name"] as? String
            refreshRow(it)
            let i = ev["i"] as? Int ?? 0, n = ev["n"] as? Int ?? 0
            status.stringValue = "转写中 \(i)/\(n)：\(it.url.lastPathComponent)"
            if table.selectedRowIndexes.count == 1, items[table.selectedRow] === it { showPreview() }
        case "progress":
            guard let it = item(for: src) else { return }
            it.pct = ev["pct"] as? Int ?? it.pct
            refreshRow(it)
        case "done":
            guard let it = item(for: src) else { return }
            if stopping {  // 停止过程中到达的结果不可信（可能是被打断导致的失败），统一记成已停止
                it.state = .pending
                it.pct = 0
                it.note = "已停止，可重新开始"
                refresh()
                return
            }
            it.name = ev["name"] as? String ?? it.name
            it.note = ev["note"] as? String ?? ""
            switch ev["state"] as? String {
            case "成功": it.state = .ok
            case "跳过": it.state = .skipped; it.note = "之前已转过，直接用上次的逐字稿"
            default: it.state = .failed
            }
            refresh()
        case "summary":
            gotSummary = true
            let ok = ev["成功"] as? Int ?? 0, sk = ev["跳过"] as? Int ?? 0, fail = ev["失败"] as? Int ?? 0
            status.stringValue = "完成：成功 \(ok)" + (sk > 0 ? "，跳过 \(sk)" : "") + (fail > 0 ? "，失败 \(fail)" : "")
        default: break
        }
    }

    /// 从启动台打开时 App 自己是 macOS 隐私权限的责任方：读写「文稿」「下载」可能被拦，报 Operation not permitted
    static let permissionHint = "macOS 没允许本 App 读写这些文件夹。\n去 系统设置 → 隐私与安全性 → 文件和文件夹，找到「批量转逐字稿」，打开「文稿文件夹」和「下载文件夹」（视频放在桌面等其他位置的也一并打开），然后点「重试失败的」。\n\n如果列表里没有它，就在 隐私与安全性 → 完全磁盘访问权限 里点 + 把 ~/Applications/批量转逐字稿.app 加进去。"

    func isPermissionError(_ s: String) -> Bool {
        s.contains("Operation not permitted") || s.contains("PermissionError")
    }

    func finished(_ code: Int32) {
        proc = nil
        let permIssue = isPermissionError(stderrText) || items.contains { isPermissionError($0.note) }
        for it in items where it.state == .failed && isPermissionError(it.note) {
            it.note = "没有读写权限（见弹窗说明）"
        }
        // 没收到 done 的（被停止，或 Python 启动就报错）一律退回可重试状态
        for it in items where it.state == .running || (it.state == .pending && it.note == "排队中")
            || (stopping && it.state == .pending) {
            it.state = stopping ? .pending : .failed
            it.pct = 0
            it.note = stopping ? "已停止，可重新开始" : "没跑到这个文件"
        }
        if stopping {
            status.stringValue = "已停止"
        } else if permIssue {
            status.stringValue = "没有文件夹读写权限，按弹窗说明打开后重试"
            if onBatchFinished == nil {
                let a = NSAlert()
                a.messageText = "需要允许访问文件夹"
                a.informativeText = Self.permissionHint
                a.addButton(withTitle: "打开隐私设置")
                a.addButton(withTitle: "稍后")
                a.beginSheetModal(for: window) { resp in
                    if resp == .alertFirstButtonReturn,
                       let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders") {
                        NSWorkspace.shared.open(url)
                    }
                }
            }
        } else if !gotSummary {
            // transcribe.py 自己退出了却没给汇总：多半是依赖缺失（preflight），把原因亮出来
            let reason = stderrText.trimmingCharacters(in: .whitespacesAndNewlines)
            status.stringValue = "出错了：" + (reason.split(separator: "\n").last.map(String.init) ?? "退出码 \(code)")
            if !reason.isEmpty && onBatchFinished == nil {
                let a = NSAlert()
                a.messageText = "转写没能开始"
                a.informativeText = reason
                a.beginSheetModal(for: window)
            }
        }
        refresh()
        if !stopping && gotSummary && onBatchFinished == nil && !window.isKeyWindow {
            NSApp.requestUserAttention(.informationalRequest)  // 跑完时在 Dock 上跳一下
        }
        onBatchFinished?()
    }

    // MARK: 其他动作

    func selectedItem() -> Item? {
        let rows = targetRows()
        return rows.count == 1 ? items[rows[0]] : nil
    }

    @objc func openSelectedMarkdown(_ sender: Any?) {
        guard let url = selectedItem()?.mdURL, FileManager.default.fileExists(atPath: url.path) else { return }
        NSWorkspace.shared.open(url)
    }

    @objc func revealSelected(_ sender: Any?) {
        // 选中整个文件夹而不是 .md：封面、视频、逐字稿在一起，用户要的是这一包
        guard let url = selectedItem()?.folderURL, FileManager.default.fileExists(atPath: url.path) else { return }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    @objc func revealSource(_ sender: Any?) {
        guard let it = selectedItem() else { return }
        NSWorkspace.shared.activateFileViewerSelecting([it.url])
    }

    @objc func copyPreview(_ sender: Any?) {
        let text = preview.string
        guard copyButton.isEnabled, !text.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        let prev = status.stringValue
        status.stringValue = "已复制逐字稿全文"
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            if self?.status.stringValue == "已复制逐字稿全文" { self?.status.stringValue = prev }
        }
    }

    @objc func openOutDir(_ sender: Any?) {
        try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(URL(fileURLWithPath: outDir))
    }

    // MARK: 菜单（编辑菜单必须有，否则预览框不响应 ⌘C/⌘A）

    func buildMenu() {
        let main = NSMenu()
        @discardableResult
        func add(_ menu: NSMenu, _ title: String, _ action: Selector?, _ key: String,
                 _ mods: NSEvent.ModifierFlags = .command, target: AnyObject? = nil) -> NSMenuItem {
            let item = menu.addItem(withTitle: title, action: action, keyEquivalent: key)
            item.keyEquivalentModifierMask = mods
            item.target = target
            return item
        }
        func submenu(_ title: String) -> NSMenu {
            let item = main.addItem(withTitle: title, action: nil, keyEquivalent: "")
            let m = NSMenu(title: title)
            item.submenu = m
            return m
        }
        let app = submenu("批量转逐字稿")
        add(app, "隐藏 批量转逐字稿", #selector(NSApplication.hide(_:)), "h")
        app.addItem(.separator())
        add(app, "退出 批量转逐字稿", #selector(NSApplication.terminate(_:)), "q")

        let file = submenu("文件")
        add(file, "添加视频或文件夹…", #selector(addFiles(_:)), "o", target: self)
        add(file, "开始 / 停止转写", #selector(toggleRun(_:)), "r", target: self)
        add(file, "打开输出文件夹", #selector(openOutDir(_:)), "o", [.command, .shift], target: self)
        add(file, "复制逐字稿全文", #selector(copyPreview(_:)), "c", [.command, .shift], target: self)
        add(file, "清空列表", #selector(clearList(_:)), "k", target: self)
        file.addItem(.separator())
        add(file, "关闭窗口", #selector(NSWindow.performClose(_:)), "w")

        let edit = submenu("编辑")
        add(edit, "拷贝", #selector(NSText.copy(_:)), "c")
        add(edit, "全选", #selector(NSText.selectAll(_:)), "a")

        let win = submenu("窗口")
        add(win, "最小化", #selector(NSWindow.performMiniaturize(_:)), "m")
        NSApp.windowsMenu = win
        NSApp.mainMenu = main
    }

    // MARK: 自测

    /// --snapshot out.png [--run] 文件...：不显示窗口，渲染成 PNG 并打印每个文件的状态
    func runSelfTestIfRequested() -> Bool {
        let args = CommandLine.arguments
        guard let i = args.firstIndex(of: "--snapshot"), args.count > i + 1 else { return false }
        let outPath = args[i + 1]
        let run = args.contains("--run")
        var rest = Array(args[(i + 2)...]).filter { $0 != "--run" }
        var stopAfter: Double?
        if let k = rest.firstIndex(of: "--stop-after"), k + 1 < rest.count {
            stopAfter = Double(rest[k + 1])
            rest.removeSubrange(k...(k + 1))
        }
        let files = rest.map { URL(fileURLWithPath: $0) }
        add(files)
        let shoot = { [weak self] in
            guard let self, let view = self.window.contentView else { return }
            if !self.items.isEmpty { self.table.selectRowIndexes([self.items.count - 1], byExtendingSelection: false) }
            view.layoutSubtreeIfNeeded()
            if let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                view.cacheDisplay(in: view.bounds, to: rep)
                try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: outPath))
            }
            print("status: \(self.status.stringValue)")
            for it in self.items { print("\(it.state)\t\(it.url.lastPathComponent)\t\(it.note)") }
            NSApp.terminate(nil)
        }
        if run {
            onBatchFinished = { DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: shoot) }
            start()
            if let t = stopAfter {
                DispatchQueue.main.asyncAfter(deadline: .now() + t) { [weak self] in self?.stop() }
            }
        } else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: shoot)
        }
        return true
    }
}

// MARK: 入口

let app = NSApplication.shared
app.setActivationPolicy(CommandLine.arguments.contains("--snapshot") ? .accessory : .regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
