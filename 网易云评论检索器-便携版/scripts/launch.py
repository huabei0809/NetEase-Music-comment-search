#!/usr/bin/env python3
"""统一启动器 —— 三个平台的「双击脚本」最终都调用这里。

为什么要有这么一层
------------------
Windows 的 cmd 在处理中文编码上很不可靠（文件编码、代码页、BOM 都能把中文变成乱码），
而 Python 3.6+ 在 Windows 上直接调用宽字符控制台 API，中文输出天然正常。
所以把**所有对用户可见的提示**都放在这个 Python 文件里，
各平台的启动脚本只做一件事：找到可用的 Python，然后调用本文件。

用法
----
    python scripts/launch.py             # 启动网页界面
    python scripts/launch.py --check     # 只做环境自检
    python scripts/launch.py --port 9000 --no-browser
"""

from __future__ import annotations

import runpy
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "web" / "server.py"

MIN_PY = (3, 8)

REQUIRED = [
    "web/server.py",
    "web/static/index.html",
    "web/static/style.css",
    "web/static/app.js",
    "netease_crawler/__init__.py",
    "netease_crawler/client.py",
    "netease_crawler/crypto.py",
]


def _line(text: str = "") -> None:
    print(text, flush=True)


def _banner() -> None:
    _line("=" * 62)
    _line("  网易云评论检索器 · 本地网页界面")
    _line("  纯本地运行，不调用任何 AI 服务")
    _line("=" * 62)


def _fail(title: str, lines: list[str]) -> int:
    _line()
    _line("!" * 62)
    _line(f"  启动失败：{title}")
    _line("!" * 62)
    for ln in lines:
        _line(ln)
    _line("!" * 62)
    return 1


def _find_running_instance() -> str | None:
    """探测本机是否已有本工具的服务在跑（端口 8765~8775）。

    为什么需要：用户重复双击启动脚本时，server.py 发现端口被占会**自动往后顺延**，
    于是桌面上出现好几个「启动界面」窗口、浏览器打开好几个端口，用户根本分不清
    哪个才是活的。这里先探一遍，能复用就直接复用。
    只认 ``/api/health`` 返回 JSON ``{"ok": true}`` 的地址，避免误把别的服务当自己人。
    """
    import json
    import urllib.request

    for port in range(8765, 8776):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=0.7
            ) as resp:
                if resp.status != 200:
                    continue
                payload = json.loads(resp.read().decode("utf-8", "replace"))
                if isinstance(payload, dict) and payload.get("ok") is True:
                    return f"http://127.0.0.1:{port}/"
        except Exception:  # noqa: BLE001 —— 端口空着、不是我们的服务，都继续试下一个
            continue
    return None


def _heal_macos_quarantine() -> None:
    """macOS 专用：清掉本目录的「隔离标记」，这是 Gatekeeper 拦截的根源。

    经微信 / 网盘 / 邮件 / 浏览器传出来的压缩包，解压后每个文件都会被写上
    ``com.apple.quarantine`` 扩展属性，macOS 会因此拒绝运行其中的 .command 脚本
    （提示「已损坏，无法打开」或「来自身份不明的开发者」）。

    本函数在脚本**能跑起来**的前提下（说明用户已放行过一次）把整个目录一次治好，
    免得用户为目录里的每个脚本重复点「打开」。
    """
    if sys.platform != "darwin":
        return
    import subprocess

    try:
        probe = subprocess.run(["xattr", "-r", str(ROOT)],
                               capture_output=True, text=True, timeout=15)
        if "com.apple.quarantine" not in (probe.stdout or ""):
            return
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(ROOT)],
                       capture_output=True, text=True, timeout=90)
        _line("  ✓ 已自动解除 macOS 对“下载文件”的运行限制（隔离标记）")
    except (OSError, subprocess.SubprocessError):
        pass  # 清不掉也不影响主流程，用户仍可手动处理


def main() -> int:
    argv = sys.argv[1:]

    # 版本
    if sys.version_info < MIN_PY:
        return _fail(
            "Python 版本过低",
            [f"  当前版本：{sys.version.split()[0]}",
             f"  需要版本：{'.'.join(map(str, MIN_PY))} 或更高",
             "",
             "  请到 https://www.python.org/downloads/ 安装新版后重试。"],
        )

    # macOS：先清掉隔离标记（微信/网盘传文件后 Gatekeeper 会拦 .command）
    _heal_macos_quarantine()

    # 文件完整性（防止用户只拷了单个文件、或解压不完整）
    missing = [p for p in REQUIRED if not (ROOT / p).exists()]
    if missing:
        return _fail(
            "文件不完整",
            ["  整个文件夹必需完整解压后才能运行，缺少以下文件："]
            + [f"    - {m}" for m in missing]
            + ["",
               "  请不要把单个文件挪出来单独运行；整个文件夹一起复制 / 解压即可。"],
        )

    if "--check" not in argv:
        _banner()
        _line(f"  Python ：{sys.version.split()[0]}")
        _line(f"  目录   ：{ROOT}")
        _line("  正在检查环境…")
        _line()

        # 已经有一个实例在跑就直接复用：否则 server.py 会把端口往后顺延，
        # 桌面上堆出好几个窗口，用户分不清哪个是活的。
        # 但用户显式指定了 --port，说明他就是要另起一个，这时不要拦。
        explicit_port = any(a == "--port" or a.startswith("--port=") for a in argv)
        if "--no-reuse" not in argv and not explicit_port:
            existing = _find_running_instance()
            if existing:
                _line("=" * 62)
                _line(f"  服务已经在运行：{existing}")
                _line("  无需重复启动，已直接为你打开界面。")
                _line()
                _line("  想重启：先回到原来那个启动窗口按 Ctrl+C（或直接关掉它）停止服务，")
                _line("          再双击本脚本即可。")
                _line("=" * 62)
                if "--no-browser" not in argv:
                    try:
                        webbrowser.open(existing)
                    except Exception:  # noqa: BLE001
                        pass
                return 0

    # --no-reuse 只是本启动器的开关，透传给 server.py 会被 argparse 拒绝
    sys.argv = ["server.py"] + [a for a in argv if a != "--no-reuse"]
    try:
        runpy.run_path(str(SERVER), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback

        _line()
        _line("!" * 62)
        _line("  运行中出现未预期的错误")
        _line("!" * 62)
        traceback.print_exc()
        _line()
        _line("  可以把上面这段信息发给提供该工具的人，便于定位问题。")
        return 1

    if "--check" not in argv:
        _line()
        _line("服务已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
