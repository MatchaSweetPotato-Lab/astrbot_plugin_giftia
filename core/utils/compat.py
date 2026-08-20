import os
import platform
import subprocess
import sys
from pathlib import Path

from astrbot.api import logger

# Windows SDK 定义：PF_AVX2_INSTRUCTIONS_AVAILABLE (Windows 10 1903+ / Server 2019+)
PF_AVX2_INSTRUCTIONS_AVAILABLE = 40


def check_avx2_support() -> bool:
    """
    在 x86 架构 CPU 上检查是否支持 AVX2 指令集。
    对于非 x86 架构（如 ARM/Apple Silicon），AVX2 不适用，跳过检查并返回 True。
    若在已知系统上明确检测到缺少 AVX2 则返回 False；若发生未知异常则记录警告并默认返回 True（避免误杀正常机器）。
    """
    machine = platform.machine().lower()
    # 非 x86 架构（如 arm64, aarch64）AVX2 不适用，直接跳过检查
    if not any(x in machine for x in ("x86", "amd64", "i386", "i686")):
        return True

    system = platform.system().lower()

    if system == "linux":
        try:
            if os.path.exists("/proc/cpuinfo"):
                with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.strip().startswith("flags") or line.strip().startswith("Features"):
                            flags = set(line.strip().split(":")[1].lower().split())
                            return "avx2" in flags
        except Exception as e:
            logger.warning(f"[Giftia Compat] 读取 /proc/cpuinfo 检测 AVX2 失败，已放行: {e}")

    elif system == "darwin":
        try:
            res = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.leaf7_features", "machdep.cpu.features"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode == 0:
                features = res.stdout.lower()
                return "avx2" in features
        except Exception as e:
            logger.warning(f"[Giftia Compat] 执行 sysctl 检查 AVX2 失败，已放行: {e}")

    elif system == "windows":
        try:
            import ctypes

            if hasattr(ctypes.windll.kernel32, "IsProcessorFeaturePresent"):
                return bool(
                    ctypes.windll.kernel32.IsProcessorFeaturePresent(
                        PF_AVX2_INSTRUCTIONS_AVAILABLE
                    )
                )
        except Exception as e:
            logger.warning(f"[Giftia Compat] Windows 检查 AVX2 失败，已放行: {e}")

    return True


def ensure_avx2_supported(config: dict | None = None, data_dir: Path | str | None = None):
    """
    确保宿主 CPU 支持 AVX2 指令集（仅在 x86 架构下生效）。
    支持通过 memory_config.ignore_avx2_check 强制跳过，或通过数据目录标记持久化跳过检测。
    若不支持且未跳过，向日志输出详细指引并抛出 RuntimeError 终止插件启动，保护 AstrBot 避免触发 SIGILL 崩溃。
    """
    # 1. 检查用户是否在 memory_config 中配置了强制跳过检测
    if config and isinstance(config.get("memory_config"), dict):
        if config["memory_config"].get("ignore_avx2_check", False):
            logger.warning(
                "[Giftia] 用户已开启 ignore_avx2_check，已跳过 CPU AVX2 指令集前置检测。"
            )
            return

    # 2. 检查持久化验证标记
    marker_file = None
    if data_dir:
        try:
            data_dir = Path(data_dir)
            marker_file = data_dir / ".avx2_verified"
            if marker_file.exists():
                return
        except Exception:
            pass

    # 3. 执行硬件检测
    if check_avx2_support():
        # 检测通过，写入持久化标记
        if marker_file:
            try:
                marker_file.parent.mkdir(parents=True, exist_ok=True)
                marker_file.write_text("ok", encoding="utf-8")
            except Exception:
                pass
        return

    # 4. 检测未通过：向日志输出详细的排版指引，异常文本保持单行精简
    guide_banner = (
        "\n================================================================================\n"
        "[Giftia] ⚠️ 启动硬件检测警告：未检测到 AVX2 指令集支持！\n"
        "--------------------------------------------------------------------------------\n"
        "本插件底层向量数据库 (LanceDB) 在 x86 架构下依赖 CPU AVX2 指令集加速。\n"
        "为防止 AstrBot 触发底层非法指令异常 (SIGILL 132) 导致进程崩溃，已主动拦截启动。\n\n"
        "【处置指引】\n"
        "1. 若您的 CPU 确实属于老旧型号（不支持 AVX2）：\n"
        "   👉 请前往 AstrBot 面板【插件管理】直接卸载 Giftia 插件以保护 AstrBot 稳定。\n\n"
        "2. 若您的 CPU 为较新型号（确信支持 AVX2，可能为容器/虚拟机环境误报）：\n"
        "   👉 请在插件配置【记忆检索配置 -> 强制跳过 AVX2 指令集检测 (ignore_avx2_check)】开启后重启插件。\n"
        "================================================================================"
    )
    logger.error(guide_banner)
    raise RuntimeError(
        "[Giftia] 未检测到 CPU AVX2 指令集支持，已阻止插件加载以防 AstrBot 崩溃。如需强制跳过请在记忆检索配置中开启 ignore_avx2_check，详见日志指引。"
    )
