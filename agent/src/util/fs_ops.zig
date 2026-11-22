/// Filesystem operations — cross-platform file/dir operations.
/// Each function spins up a private Io instance for the Zig 0.17 std.Io API;
/// these are one-shot utilities, not hot-path helpers.
const std = @import("std");

/// Check if a file exists at the given path.
pub fn fileExists(path: []const u8) bool {
    var io_state = std.Io.Threaded.init(std.heap.smp_allocator, .{});
    defer io_state.deinit();
    std.Io.Dir.cwd().access(io_state.io(), path, .{}) catch return false;
    return true;
}

/// Check if a directory exists at the given path.
pub fn dirExists(path: []const u8) bool {
    var io_state = std.Io.Threaded.init(std.heap.smp_allocator, .{});
    defer io_state.deinit();
    var dir = std.Io.Dir.cwd().openDir(io_state.io(), path, .{}) catch return false;
    dir.close(io_state.io());
    return true;
}

/// Ensure a directory exists, creating it and parents if necessary.
pub fn ensureDir(path: []const u8) !void {
    var io_state = std.Io.Threaded.init(std.heap.smp_allocator, .{});
    defer io_state.deinit();
    std.Io.Dir.cwd().createDirPath(io_state.io(), path) catch |err| switch (err) {
        error.PathAlreadyExists => {},
        else => return err,
    };
}

/// Read a file's contents. Returns owned slice.
pub fn readFileAlloc(allocator: std.mem.Allocator, path: []const u8, max_size: usize) ![]u8 {
    var io_state = std.Io.Threaded.init(std.heap.smp_allocator, .{});
    defer io_state.deinit();
    return std.Io.Dir.readFileAlloc(
        std.Io.Dir.cwd(),
        io_state.io(),
        path,
        allocator,
        std.Io.Limit.limited(max_size),
    );
}

/// Write data to a file, creating it if necessary.
pub fn writeFile(path: []const u8, data: []const u8) !void {
    var io_state = std.Io.Threaded.init(std.heap.smp_allocator, .{});
    defer io_state.deinit();
    try std.Io.Dir.writeFile(std.Io.Dir.cwd(), io_state.io(), .{
        .sub_path = path,
        .data = data,
    });
}

/// Copy a file from src to dst.
pub fn copyFile(src: []const u8, dst: []const u8) !void {
    var io_state = std.Io.Threaded.init(std.heap.smp_allocator, .{});
    defer io_state.deinit();
    const cwd = std.Io.Dir.cwd();
    try cwd.copyFile(cwd, src, cwd, dst, io_state.io(), .{});
}

/// Delete a file. Returns true if file was deleted, false if it didn't exist.
pub fn deleteFile(path: []const u8) bool {
    var io_state = std.Io.Threaded.init(std.heap.smp_allocator, .{});
    defer io_state.deinit();
    if (std.fs.path.isAbsolute(path)) {
        std.Io.Dir.deleteFileAbsolute(io_state.io(), path) catch return false;
    } else {
        std.Io.Dir.cwd().deleteFile(io_state.io(), path) catch return false;
    }
    return true;
}

/// Get file size in bytes. Returns 0 if file doesn't exist.
pub fn fileSize(path: []const u8) !u64 {
    var io_state = std.Io.Threaded.init(std.heap.smp_allocator, .{});
    defer io_state.deinit();
    const st = try std.Io.Dir.cwd().statFile(io_state.io(), path, .{});
    return st.size;
}

/// Rename/move a file.
pub fn rename(old_path: []const u8, new_path: []const u8) !void {
    var io_state = std.Io.Threaded.init(std.heap.smp_allocator, .{});
    defer io_state.deinit();
    const cwd = std.Io.Dir.cwd();
    try cwd.rename(cwd, old_path, cwd, new_path, io_state.io());
}

/// List files in a directory. Caller owns returned slice and each element.
pub fn listDir(allocator: std.mem.Allocator, path: []const u8) ![][]u8 {
    var io_state = std.Io.Threaded.init(std.heap.smp_allocator, .{});
    defer io_state.deinit();
    var dir = try std.Io.Dir.cwd().openDir(io_state.io(), path, .{ .iterate = true });
    defer dir.close(io_state.io());

    var result = std.ArrayList([]u8).empty;
    defer result.deinit(allocator);

    var iter = dir.iterate();
    while (try iter.next(io_state.io())) |entry| {
        if (entry.kind == .file) {
            const name = try allocator.dupe(u8, entry.name);
            try result.append(allocator, name);
        }
    }
    return result.toOwnedSlice(allocator);
}

test "fileExists" {
    try std.testing.expect(!fileExists("/nonexistent/file.txt"));
}

test "ensureDir" {
    const tmp = "/tmp/ziv_fs_test";
    try ensureDir(tmp);
    try std.testing.expect(dirExists(tmp));
    var io_state = std.Io.Threaded.init(std.heap.smp_allocator, .{});
    defer io_state.deinit();
    std.Io.Dir.cwd().deleteTree(io_state.io(), tmp) catch {};
}

/// 原子落盘(F2,2026-11-20;2026-11-22 提公用):同目录 tmp + rename。
/// truncate+整写期间并发读者会读到半截(加载侧 `parse catch return`
/// 静默丢整层);rename 后读者要么全旧要么全新。tmp 显式 0600(rename
/// 后即最终权限,带密钥的文件不落默认 0644)。与同文件其它一次性工具
/// 不同,本函数接受调用方 io——调用点均在持 io 的路径上,省线程起停。
/// 写败/rename 败原样上抛,残留 .tmp 尽力清理。
pub fn writeFileAtomic(io: std.Io, path: []const u8, data: []const u8, alloc: std.mem.Allocator) !void {
    const tmp = try std.fmt.allocPrint(alloc, "{s}.tmp", .{path});
    defer alloc.free(tmp);
    std.Io.Dir.writeFile(std.Io.Dir.cwd(), io, .{
        .sub_path = tmp,
        .data = data,
        .flags = .{
            .permissions = if (@import("builtin").os.tag == .windows)
                .default_file
            else
                @enumFromInt(0o600),
        },
    }) catch |err| {
        std.Io.Dir.cwd().deleteFile(io, tmp) catch {};
        return err;
    };
    const cwd = std.Io.Dir.cwd();
    cwd.rename(tmp, cwd, path, io) catch |err| {
        std.Io.Dir.cwd().deleteFile(io, tmp) catch {};
        return err;
    };
}
