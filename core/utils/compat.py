import os
import platform
import subprocess
import sys

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


def ensure_avx2_supported():
    """
    确保宿主 CPU 支持 AVX2 指令集（仅在 x86 架构下生效）。
    若检测到不支持则直接抛出 RuntimeError，阻止加载底层 LanceDB 避免触发 SIGILL 崩溃。
    """
    if not check_avx2_support():
        err_msg = (
            "\n==================================================================\n"
            "[Giftia] 启动失败：检测到当前宿主 CPU 不支持 AVX2 指令集！\n"
            "插件依赖的向量数据库 (LanceDB) 在 x86 架构下需要 AVX2 指令集加速。\n"
            "在不支持 AVX2 的 x86 CPU 上加载 LanceDB 会直接导致 AstrBot 进程崩溃 (SIGILL / 非法指令 132)。\n"
            "为保护 AstrBot 稳定运行，插件已主动终止加载。\n"
            "如需使用 Giftia 插件，请在支持 AVX2 的 x86 CPU 设备（如 Intel 4代+ / AMD 锐龙+）或 ARM64 设备上运行。\n"
            "=================================================================="
        )
        logger.error(err_msg)
        raise RuntimeError(err_msg)
