"""stdio MCP server stdout 守卫：过滤子进程 stdout 里的非 JSONRPC 输出。

背景：部分第三方 stdio MCP server（如 office-word-mcp-server）会向 stdout print
非 JSON 内容（"Loading configuration from .env file..." 等），违反 stdio 传输
协议（stdout 只允许换行分隔的 JSONRPC 消息）。轻则客户端刷 ERROR 日志，重则在
Windows 管道块缓冲下，垃圾行延迟到进程退出才 flush，撞上已关闭的读取流导致
ExceptionGroup[BrokenResourceError]，整个 server 被判 down。

用法（注册 stdio MCP server 时）：
  command: <python 绝对路径>
  args:    [<本脚本绝对路径>, "--", "<真实 command>", "<arg1>", ...]
  env:     原样透传给子进程
  cwd:     平台配置里的 cwd 生效于守卫进程，子进程继承

守卫行为：stdin 逐行转发给子进程；子进程 stdout 中能解析为 JSON 对象的行原样
转发（JSONRPC 消息），其余行改道 stderr 并加 [mcp-stdio-guard] 前缀；子进程
stderr 原样透传；退出码透传。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

PREFIX = "[mcp-stdio-guard]"
# 管道对端关闭 / 流已关闭时 IO 抛出的异常族，泵线程静默退出即可
PIPE_ERRORS = (BrokenPipeError, ConnectionError, OSError, ValueError)


def _pump_stdin(child: subprocess.Popen) -> None:
    """把父进程（MCP 客户端）的 stdin 逐行转发给子进程。"""
    try:
        for line in sys.stdin:
            child.stdin.write(line)
            child.stdin.flush()
    except PIPE_ERRORS:
        pass
    finally:
        try:
            child.stdin.close()
        except PIPE_ERRORS:
            pass


def _is_jsonrpc_object(line: str) -> bool:
    """JSONRPC 消息必须是单行 JSON 对象（jsonrpc/method/result/error 等字段）。"""
    try:
        return isinstance(json.loads(line), dict)
    except ValueError:
        return False


def _pump_stdout(child: subprocess.Popen) -> None:
    """子进程 stdout -> 过滤：JSON 对象行转发 stdout，垃圾行改道 stderr。"""
    out = sys.stdout
    err = sys.stderr
    try:
        for line in child.stdout:
            if _is_jsonrpc_object(line):
                out.write(line if line.endswith("\n") else line + "\n")
                out.flush()
            else:
                stripped = line.rstrip("\r\n")
                if stripped:
                    err.write(f"{PREFIX} dropped non-JSON stdout: {stripped}\n")
                    err.flush()
    except PIPE_ERRORS:
        pass


def main() -> None:
    # stdio 传输按协议固定 UTF-8；Windows 默认 GBK 管道会损坏非 ASCII JSONRPC 消息
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")

    argv = sys.argv[1:]
    cwd = None
    if argv and argv[0] == "--cwd":
        cwd = argv[1]
        argv = argv[2:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        sys.stderr.write(f"{PREFIX} usage: stdio_guard.py [--cwd DIR] -- <command> [args...]\n")
        sys.exit(2)

    child = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    threading.Thread(target=_pump_stdin, args=(child,), daemon=True).start()
    _pump_stdout(child)

    deadline = time.monotonic() + 10.0
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
    sys.exit(child.returncode if child.returncode is not None else 1)


if __name__ == "__main__":
    main()
