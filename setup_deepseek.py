"""Interactive setup for a per-user, DPAPI-protected DeepSeek API key."""

import getpass
import sys
import warnings

from src.deepseek_auth import save_key


def main() -> int:
    if len(sys.argv) != 1:
        print("请直接运行 python setup_deepseek.py，再按提示输入密钥。", file=sys.stderr)
        return 2
    try:
        with warnings.catch_warnings():
            # Stop before getpass can fall back to visible input.
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("DeepSeek API Key（输入时隐藏）: ")
        save_key(key)
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        print("已取消。请在交互式终端中运行配置脚本。", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"保存失败。{exc}", file=sys.stderr)
        return 1
    print("DeepSeek API Key 已加密保存至当前 Windows 用户账户。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
