from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MissingDependency:
    name: str
    reason: str
    install_command: str


@dataclass
class EnvReport:
    platform: str
    python: str
    ok: bool
    issues: list[MissingDependency] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def format_message(self) -> str:
        lines = [
            f"系统: {self.platform}",
            f"Python: {self.python}",
            f"状态: {'正常' if self.ok else '存在缺失依赖'}",
        ]
        if self.issues:
            lines.append("")
            lines.append("检测到以下问题：")
            for i, item in enumerate(self.issues, 1):
                lines.append(f"{i}. 当前环境缺少{item.name}，请执行命令“{item.install_command}”")
                if item.reason:
                    lines.append(f"   说明: {item.reason}")
        if self.notes:
            lines.append("")
            lines.append("提示：")
            for note in self.notes:
                lines.append(f"- {note}")
        return "\n".join(lines)


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _linux_lib_exists(names: list[str]) -> bool:
    for name in names:
        # try common locations + ldconfig
        candidates = [
            f"/usr/lib/{name}",
            f"/usr/lib64/{name}",
            f"/lib/{name}",
            f"/lib64/{name}",
            f"/usr/lib/x86_64-linux-gnu/{name}",
            f"/usr/lib/aarch64-linux-gnu/{name}",
        ]
        if any(Path(p).exists() for p in candidates):
            return True
        try:
            ctypes.CDLL(name)
            return True
        except OSError:
            pass
    try:
        result = subprocess.run(
            ["ldconfig", "-p"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        out = result.stdout or ""
        return any(name.split(".")[0] in out or name in out for name in names)
    except Exception:
        return False


def _command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def check_meme_generator_import() -> tuple[bool, str | None]:
    try:
        import meme_generator  # noqa: F401

        return True, None
    except Exception as exc:
        return False, str(exc)


def detect_environment() -> EnvReport:
    system = platform.system()
    release = platform.release()
    machine = platform.machine()
    py = f"{platform.python_version()} ({sys.executable})"
    report = EnvReport(
        platform=f"{system} {release} ({machine})",
        python=py,
        ok=True,
    )

    ok_import, import_err = check_meme_generator_import()
    if not ok_import:
        report.ok = False
        report.issues.append(
            MissingDependency(
                name="Python 包 meme_generator",
                reason=import_err or "导入失败",
                install_command='pip install "meme_generator>=0.1.14,<0.2.0"',
            )
        )
        # skia often fails due to missing system libs; continue checking below
        if import_err and any(
            key in import_err.lower()
            for key in ("egl", "gl", "skia", "libgl", "fontconfig")
        ):
            report.notes.append(
                "meme_generator 导入失败可能由系统图形库缺失引起，请优先安装下方系统依赖。"
            )

    if _is_linux():
        egl_ok = _linux_lib_exists(
            ["libEGL.so.1", "libEGL.so", "libegl.so.1", "libegl.so"]
        )
        gl_ok = _linux_lib_exists(
            ["libGL.so.1", "libGL.so", "libOpenGL.so.0", "libOpenGL.so"]
        )
        fontconfig_ok = _linux_lib_exists(
            ["libfontconfig.so.1", "libfontconfig.so"]
        ) or _command_exists("fc-cache")
        glib_ok = _linux_lib_exists(["libglib-2.0.so.0", "libglib-2.0.so"])

        # Prefer modern package names; also show docker-friendly variants in notes.
        if not egl_ok:
            report.ok = False
            report.issues.append(
                MissingDependency(
                    name="系统库 libEGL (OpenGL EGL)",
                    reason="skia-python / meme-generator 在 Linux 上需要 EGL",
                    install_command="apt-get update && apt-get install -y libegl1 libgl1 libgles2",
                )
            )
        if not gl_ok:
            report.ok = False
            report.issues.append(
                MissingDependency(
                    name="系统库 libGL / OpenGL",
                    reason="图像渲染依赖 OpenGL",
                    install_command="apt-get update && apt-get install -y libgl1 libglib2.0-0",
                )
            )
        if not fontconfig_ok:
            report.ok = False
            report.issues.append(
                MissingDependency(
                    name="fontconfig",
                    reason="字体发现与缓存依赖 fontconfig",
                    install_command="apt-get update && apt-get install -y fontconfig fonts-noto-cjk fonts-noto-color-emoji",
                )
            )
        if not glib_ok:
            report.ok = False
            report.issues.append(
                MissingDependency(
                    name="系统库 libglib",
                    reason="部分图形/图像依赖需要 glib",
                    install_command="apt-get update && apt-get install -y libglib2.0-0",
                )
            )

        if not report.ok:
            report.notes.append(
                "Debian/Ubuntu 桌面环境也可尝试: "
                "sudo apt install -y libegl1-mesa libgles2-mesa libgl1-mesa-dev fontconfig"
            )
            report.notes.append(
                "若仍报错可补装: "
                "apt-get install -y libxrender1 libxcursor1 libxkbcommon0 libdbus-1-3"
            )
            report.notes.append("无 root 权限时请让管理员执行上述命令，或改用已预装依赖的镜像。")

    elif _is_windows():
        report.notes.append(
            "Windows 一般无需额外 OpenGL 系统包；若 meme_generator 导入失败，请先执行: "
            'pip install "meme_generator>=0.1.14,<0.2.0"'
        )
    elif _is_macos():
        report.notes.append(
            "macOS 若字体异常，可安装: brew install fontconfig"
        )

    return report
