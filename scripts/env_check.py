#!/usr/bin/env python3
"""环境检测模块 —— 启动时自动检查运行前置条件。

检测项：
  ① Chrome 浏览器是否已安装
  ② Kimi WebBridge 插件是否可用（端口 10086）
  ③ Boss 直聘页面是否已登录（复用用户登录态）

用法：
    from scripts.env_check import check_env, EnvResult
    result = check_env()
    if not result.ok:
        print(result.message)   # 含修复指引
        sys.exit(1)
    print("✅ 环境检测全部通过")
"""

import sys
import os
import subprocess
import socket
import json

# 路径引导：确保 scripts 包可导入（无论以脚本直接运行还是被其它模块导入）
_HERE = os.path.dirname(os.path.abspath(__file__))
# 兼容 Git Bash 的 /c/... UNIX 风格路径：Windows Python 会把 /c 误算成 C:\c
if len(_HERE) >= 3 and _HERE[0] == "/" and _HERE[1].isalpha() and _HERE[2] == "/":
    _HERE = _HERE[1].upper() + ":" + _HERE[2:]
SKILL_DIR = os.path.dirname(_HERE)
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)
from scripts.execution_context import context
from scripts.webbridge_client import DEFAULT_HOST, DEFAULT_PORT

# ── 检测结果 ──────────────────────────────────────
class EnvResult:
    """单次环境检测结果。"""
    def __init__(self):
        self.ok = True
        self.items = []   # [(name, passed, detail), ...]
        self.message = ""

    def add(self, name, passed, detail=""):
        self.items.append((name, passed, detail))
        if not passed:
            self.ok = False

    def build_message(self):
        lines = []
        for name, passed, detail in self.items:
            icon = "✅" if passed else "❌"
            lines.append(f"  {icon} {name}: {detail or ('通过' if passed else '未通过')}")
        if not self.ok:
            lines.append("")
            lines.append("⚠️  环境未就绪，请按上方指引完成准备后重试。")
        self.message = "\n".join(lines)
        return self.message


# ── Chrome 检测 ────────────────────────────────────
def _check_chrome():
    """检测 Chrome 是否安装在常见路径。"""
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]
    browser = (context() or {}).get('browser', 'chrome')
    if browser == 'edge':
        chrome_paths = [r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
                        os.path.expanduser(r'~\AppData\Local\Microsoft\Edge\Application\msedge.exe')]
    for p in chrome_paths:
        if os.path.exists(p):
            return True, f"已安装 ({p})"
    # Windows 还可尝试 where/which
    try:
        r = subprocess.run(["where", "msedge" if browser == 'edge' else "chrome"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return True, f"已安装 ({r.stdout.strip().splitlines()[0]})"
    except Exception:
        pass
    if browser == 'edge':
        return False, '未检测到 Edge 浏览器，请安装并启用 Kimi 浏览器扩展'
    return False, (
        "未检测到 Chrome 浏览器。\n"
        "   请前往 https://www.google.com/chrome/ 下载安装。"
    )


# ── WebBridge 检测 ─────────────────────────────────
_WEBBRIDGE_HOST = DEFAULT_HOST
_WEBBRIDGE_PORT = DEFAULT_PORT


def _check_webbridge():
    """检测 Kimi WebBridge 是否在本地端口 10086 可达。"""
    try:
        if context():
            from scripts.browser_session import bridge_status
            status = bridge_status(context())
            return bool(status.get('extension_connected')), f'账户桥接端口 {_WEBBRIDGE_PORT}，扩展连接={status.get("extension_connected")}'
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        result = s.connect_ex((_WEBBRIDGE_HOST, _WEBBRIDGE_PORT))
        s.close()
        if result == 0:
            return True, f"WebBridge 已启动 (端口 {_WEBBRIDGE_PORT})"
        return False, (
            f"WebBridge 未在端口 {_WEBBRIDGE_PORT} 响应。\n"
            "   请确认：\n"
            "   ① 已安装 Kimi WebBridge 插件 → https://www.kimi.com/zh-cn/features/webbridge\n"
            "   ② Chrome 中已启用该插件\n"
            "   ③ 终端执行 kimi-webbridge start 启动服务"
        )
    except Exception as e:
        return False, f"WebBridge 连接异常: {e}"


# ── Boss 登录检测 ──────────────────────────────────
def _check_boss_login():
    """
    通过 WebBridge evaluate 检测 Boss 直聘登录态。
    不绑定任何特定用户名——通用检测：
      - 页面非登录墙（不含"请登录"/"立即登录"等）
      - 页面含已登录标志（导航栏有用户信息 / body 含正常推荐内容）
    """
    try:
        if context():
            from scripts.browser_session import verify_identity
            verify_identity()
            return True, '浏览器及账户身份已核验'
        # 延迟导入，避免无 WebBridge 时 import 失败
        from scripts.webbridge_client import api, evaluate as _ev
        import time as _t

        # 先导航到 Boss 推荐页，确保读到的是目标页面（而非其它空白标签页）
        RECOMMEND_URL = "https://www.zhipin.com/web/geek/jobs?ka=header-jobs"
        try:
            _ev  # noqa
            api("navigate", {"url": RECOMMEND_URL})
            _t.sleep(6)
        except Exception:
            pass

        body_text = _ev("document.body.innerText || ''") or ""
        login_wall_kws = ["请登录", "立即登录", "扫码登录", "账号密码登录", "手机号登录"]
        for kw in login_wall_kws:
            # 短文本命中且是主导航区特征（避免误判岗位描述中的"登录"）
            if kw in body_text and body_text.index(kw) < 200:
                return False, (
                    "Boss 直聘未登录或登录态已过期。\n"
                    "   请在 Chrome 中打开 https://www.zhipin.com 并完成登录，\n"
                    "   本流程需复用您的登录数据才能投递。"
                )

        # 已登录标志：body 足够长（说明已加载推荐内容）且无登录墙
        if len(body_text.strip()) > 100:
            return True, "Boss 直聘已登录"

        return False, (
            "无法确认 Boss 直聘登录状态（页面内容过短）。\n"
            "   请在 Chrome 中打开 https://www.zhipin.com 确认已登录。"
        )
    except Exception as e:
        return False, f"无法连接 WebBridge 检测登录态: {e}（请先确保 WebBridge 已启动）"


# ── 主入口 ─────────────────────────────────────────
def check_env(verbose=True):
    """执行全部环境检测，返回 EnvResult。

    Args:
        verbose: 是否打印每项结果到 stdout
    """
    result = EnvResult()

    # ① Chrome
    passed, detail = _check_chrome()
    result.add((context() or {}).get('browser', 'chrome') + ' 浏览器', passed, detail)

    # ② WebBridge
    passed, detail = _check_webbridge()
    result.add("Kimi WebBridge", passed, detail)

    # 仅当前两项都通过时才检测登录（需要 WebBridge 才能读页面）
    if result.ok or any(n == "Kimi WebBridge" and p for n, p, _ in result.items):
        passed, detail = _check_boss_login()
        result.add("Boss 直聘登录", passed, detail)

    result.build_message()
    if verbose:
        print("\n🔍 BOSS 直聘投递技能 — 环境检测")
        print(result.message)
        print()
    return result


# ── CLI 入口 ───────────────────────────────────────
if __name__ == "__main__":
    r = check_env()
    sys.exit(0 if r.ok else 1)
