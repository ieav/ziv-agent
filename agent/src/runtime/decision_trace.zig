/// Decision trace — records the full reasoning chain for each user query.
/// Enables self-diagnostic: when user questions a result, we can replay the decision chain
/// and identify which step had a logic gap.
const std = @import("std");
const wincompat = @import("../util/wincompat.zig");
const time_mod = @import("../util/time.zig");
const message = @import("../llm/message.zig");

pub const DecisionPhase = enum {
    memory_retrieval,
    context_assembly,
    tool_selection,
    tool_execution,
    llm_generation,
    response_postprocess,
    reflection_trigger,
};

pub const Alternative = struct {
    description: []const u8,
    why_excluded: []const u8,
};

pub const DecisionStep = struct {
    step_id: u32,
    phase: DecisionPhase,
    timestamp: i64,
    input: []const u8,
    output: []const u8,
    reasoning: []const u8,
    confidence: f64, // 0.0 - 1.0, below φ (~0.618) triggers diagnostic
    duration_ms: i64,
    alternatives: []const Alternative,
    dependencies: []const u32,
};

pub const DecisionTrace = struct {
    allocator: std.mem.Allocator,
    query: []const u8,
    steps: std.ArrayList(DecisionStep),
    started_at: i64,
    completed_at: ?i64,

    const Self = @This();

    pub fn init(allocator: std.mem.Allocator, query: []const u8) Self {
        return .{
            .allocator = allocator,
            .query = query,
            .steps = std.ArrayList(DecisionStep).empty,
            .started_at = time_mod.nowMillis(),
            .completed_at = null,
        };
    }

    pub fn deinit(self: *Self) void {
        for (self.steps.items) |step| {
            self.allocator.free(step.input);
            self.allocator.free(step.output);
            self.allocator.free(step.reasoning);
            for (step.alternatives) |alt| {
                self.allocator.free(alt.description);
                self.allocator.free(alt.why_excluded);
            }
            self.allocator.free(step.alternatives);
            self.allocator.free(step.dependencies);
        }
        self.steps.deinit(self.allocator);
        self.allocator.free(self.query);
    }

    pub fn addStep(self: *Self, step: DecisionStep) !void {
        try self.steps.append(self.allocator, step);
    }

    pub fn complete(self: *Self) void {
        self.completed_at = time_mod.nowMillis();
    }

    pub fn getSteps(self: *const Self) []const DecisionStep {
        return self.steps.items;
    }

    pub fn getStepByPhase(self: *const Self, phase: DecisionPhase) ?*const DecisionStep {
        for (self.steps.items) |*step| {
            if (step.phase == phase) return step;
        }
        return null;
    }

    /// Find all steps with confidence below the golden ratio threshold.
    /// Returns indices into `getSteps()` — the old version returned copies
    /// of steps (each owning duped strings) that no caller ever freed.
    pub fn findWeakSteps(self: *const Self, threshold: f64) []const usize {
        var result = std.ArrayList(usize).empty;
        defer result.deinit(self.allocator);
        for (self.steps.items, 0..) |step, i| {
            if (step.confidence < threshold) {
                result.append(self.allocator, i) catch break;
            }
        }
        if (result.items.len == 0) return &.{};
        return self.allocator.dupe(usize, result.items) catch &.{};
    }

    /// Total duration of the decision chain.
    pub fn totalDurationMs(self: *const Self) i64 {
        if (self.completed_at) |end| {
            return end - self.started_at;
        }
        return time_mod.nowMillis() - self.started_at;
    }

    /// JSONL payload — a header line (query + totals) then one line per
    /// step with its duration. Split out from writeJsonlFile so the
    /// serialization is unit-testable without touching the filesystem.
    fn toJsonl(self: *const Self, alloc: std.mem.Allocator) ![]u8 {
        var buf = std.ArrayList(u8).empty;
        defer buf.deinit(alloc);
        var aw = std.Io.Writer.Allocating.fromArrayList(alloc, &buf);
        const w = &aw.writer;

        try std.Io.Writer.print(w, "{{\"query\":", .{});
        try message.writeJsonString(w, self.query);
        try std.Io.Writer.print(w, ",\"steps\":{d},\"total_ms\":{d},\"started_at\":{d}}}\n", .{ self.steps.items.len, self.totalDurationMs(), self.started_at });
        for (self.steps.items) |s| {
            try std.Io.Writer.print(w, "{{\"step\":{d},\"phase\":\"{s}\",\"ts\":{d},\"ms\":{d},\"conf\":{d:.2},\"in\":", .{ s.step_id, @tagName(s.phase), s.timestamp, s.duration_ms, s.confidence });
            try message.writeJsonString(w, s.input);
            try std.Io.Writer.print(w, ",\"out\":", .{});
            try message.writeJsonString(w, s.output);
            try std.Io.Writer.print(w, ",\"why\":", .{});
            try message.writeJsonString(w, s.reasoning);
            try std.Io.Writer.print(w, "}}\n", .{});
        }
        buf = aw.toArrayList();
        return buf.toOwnedSlice(alloc);
    }

    /// Persist as JSONL to `<dir_path>/<started_at>.jsonl`. Turn telemetry
    /// (2026-11-05): the trace used to die with deinit(), so "哪一步花了
    /// 多久" never survived the session. Callers treat failure as a
    /// warning — telemetry must never break a turn.
    pub fn writeJsonlFile(self: *const Self, io: std.Io, dir_path: []const u8, alloc: std.mem.Allocator) !void {
        const data = try self.toJsonl(alloc);
        defer alloc.free(data);

        const d: std.Io.Dir = .cwd();
        try d.createDirPath(io, dir_path);
        var path_buf: [160]u8 = undefined;
        const path = try std.fmt.bufPrint(&path_buf, "{s}/{d}.jsonl", .{ dir_path, self.started_at });
        try d.writeFile(io, .{ .sub_path = path, .data = data });
    }
};

/// T1(2026-11-22 资源审计):traces 每回合一文件、默认开、曾永不清理——
/// 长期使用的唯一无界磁盘增长点。逐出式收口:按文件名(=毫秒时间戳,
/// 同位数下字典序即时间序)降序保留最近 keep 份,其余删除。启动时清一
/// 次存量、每回合写入后再清增量。返回删除数(供日志;0 = 无事)。
/// 单文件删除失败吞掉——遥测清理不许破坏回合。
pub const TRACES_KEEP: usize = 500;

pub fn pruneTraces(io: std.Io, dir_path: []const u8, keep: usize, alloc: std.mem.Allocator) usize {
    const d: std.Io.Dir = .cwd();
    var dir = d.openDir(io, dir_path, .{ .iterate = true }) catch return 0;
    defer dir.close(io);
    var names: std.ArrayList([]const u8) = .empty;
    defer {
        for (names.items) |n| alloc.free(n);
        names.deinit(alloc);
    }
    var it = dir.iterate();
    while (it.next(io) catch null) |e| {
        if (e.kind != .file) continue;
        if (!std.mem.endsWith(u8, e.name, ".jsonl")) continue;
        names.append(alloc, alloc.dupe(u8, e.name) catch continue) catch continue;
    }
    if (names.items.len <= keep) return 0;
    // 降序:最新在前,keep 之后全删。
    std.mem.sort([]const u8, names.items, {}, struct {
        fn before(_: void, a: []const u8, b: []const u8) bool {
            return std.mem.order(u8, a, b) == .gt;
        }
    }.before);
    var deleted: usize = 0;
    var pb: [std.fs.max_path_bytes]u8 = undefined;
    for (names.items[keep..]) |n| {
        const p = std.fmt.bufPrint(&pb, "{s}/{s}", .{ dir_path, n }) catch continue;
        d.deleteFile(io, p) catch continue;
        deleted += 1;
    }
    return deleted;
}

test "T1 pruneTraces: keeps newest N by timestamp name, no-op under cap" {
    const a = std.testing.allocator;
    var io_state = std.Io.Threaded.init(a, .{});
    defer io_state.deinit();
    const io = io_state.io();
    const dir = ".zig-cache/t1-traces";
    const d: std.Io.Dir = .cwd();
    defer {
        var cb: [std.fs.max_path_bytes]u8 = undefined;
        var dir_h: ?std.Io.Dir = d.openDir(io, dir, .{ .iterate = true }) catch null;
        if (dir_h) |*h| {
            var it = h.iterate();
            while (it.next(io) catch null) |e| {
                if (e.kind != .file) continue;
                const p = std.fmt.bufPrint(&cb, "{s}/{s}", .{ dir, e.name }) catch continue;
                d.deleteFile(io, p) catch {};
            }
            h.close(io);
        }
        d.deleteDir(io, dir) catch {};
    }
    d.createDirPath(io, dir) catch return error.TestSetup;
    // 5 份时间戳名(同位数,字典序=时间序)+ 1 个非 jsonl 干扰文件。
    var nb: [64]u8 = undefined;
    for (1..6) |i| {
        const n = std.fmt.bufPrint(&nb, "{s}/179000000000{d}.jsonl", .{ dir, i }) catch unreachable;
        d.writeFile(io, .{ .sub_path = n, .data = "{}" }) catch return error.TestSetup;
    }
    d.writeFile(io, .{ .sub_path = std.fmt.bufPrint(&nb, "{s}/noise.txt", .{dir}) catch unreachable, .data = "x" }) catch return error.TestSetup;

    // ≤keep:no-op。
    try std.testing.expectEqual(@as(usize, 0), pruneTraces(io, dir, 10, a));
    // keep=2:删最老 3 份,留最新 2;noise.txt 不受影响。
    try std.testing.expectEqual(@as(usize, 3), pruneTraces(io, dir, 2, a));
    var dir_h = try d.openDir(io, dir, .{ .iterate = true });
    var count: usize = 0;
    var has_04 = false;
    var has_noise = false;
    var it = dir_h.iterate();
    while (it.next(io) catch null) |e| {
        if (e.kind != .file) continue;
        count += 1;
        if (std.mem.eql(u8, e.name, "1790000000004.jsonl")) has_04 = true;
        if (std.mem.eql(u8, e.name, "1790000000005.jsonl")) has_04 = true;
        if (std.mem.eql(u8, e.name, "noise.txt")) has_noise = true;
    }
    dir_h.close(io);
    try std.testing.expectEqual(@as(usize, 3), count); // 2 jsonl + noise
    try std.testing.expect(has_04); // 留的是最新两份
    try std.testing.expect(has_noise);
    // 空目录/不存在目录:no-op 不炸。
    try std.testing.expectEqual(@as(usize, 0), pruneTraces(io, ".zig-cache/t1-nonexistent", 2, a));
}

test "trace jsonl carries per-step durations" {
    const a = std.testing.allocator;
    // init takes ownership of `query` (deinit frees it) — dupe, matching
    // the production call site in repl.zig.
    var t = DecisionTrace.init(a, try a.dupe(u8, "测 query \"引号\""));
    defer t.deinit();
    try t.addStep(.{
        .step_id = 1,
        .phase = .llm_generation,
        .timestamp = 1793874863369,
        // deinit frees these — dupe like the production traceAddStep does,
        // literals would abort in the allocator's poison memset.
        .input = try a.dupe(u8, "history + system prompt"),
        .output = try a.dupe(u8, "content blocks"),
        .reasoning = try a.dupe(u8, "tool_use"),
        .confidence = 0.9,
        .duration_ms = 1234,
        .alternatives = &.{},
        .dependencies = &.{},
    });
    const jsonl = try t.toJsonl(a);
    defer a.free(jsonl);
    // Header carries the query (escaped) and a total; the step line
    // carries phase + its own duration — the fields the timing
    // post-mortem reads.
    try std.testing.expect(std.mem.indexOf(u8, jsonl, "\\\"引号\\\"") != null);
    try std.testing.expect(std.mem.indexOf(u8, jsonl, "\"total_ms\":") != null);
    try std.testing.expect(std.mem.indexOf(u8, jsonl, "\"phase\":\"llm_generation\"") != null);
    try std.testing.expect(std.mem.indexOf(u8, jsonl, "\"ms\":1234") != null);

    // File round-trip: the turn-end persistence path (writeJsonlFile) is
    // the piece post-mortems depend on — verify the bytes land on disk.
    var io_state = std.Io.Threaded.init(a, .{});
    defer io_state.deinit();
    var dir_buf: [64]u8 = undefined;
    const dir = std.fmt.bufPrint(&dir_buf, "{s}/ziv_trace_test_{d}", .{ @import("../util/time.zig").tmpRoot(), @import("../util/wincompat.zig").pid() }) catch return;
    t.writeJsonlFile(io_state.io(), dir, a) catch return; // fs-less envs: skip
    var fpath_buf: [128]u8 = undefined;
    const fpath = std.fmt.bufPrint(&fpath_buf, "{s}/{d}.jsonl", .{ dir, t.started_at }) catch return;
    var back_buf: [4096]u8 = undefined;
    const back = std.Io.Dir.cwd().readFile(io_state.io(), fpath, &back_buf) catch return;
    try std.testing.expect(std.mem.indexOf(u8, back, "\"phase\":\"llm_generation\"") != null);
    std.Io.Dir.cwd().deleteFile(io_state.io(), fpath) catch {};
    std.Io.Dir.cwd().deleteDir(io_state.io(), dir) catch {};
}
