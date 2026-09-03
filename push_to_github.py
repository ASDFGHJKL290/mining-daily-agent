"""
一键推送脚本：仓库在 GitHub 建好后，运行本脚本完成首次推送。

用法：
  1. 先在 GitHub 网页建好仓库（ASDFGHJKL290/mining-daily-agent，选 Private）
  2. 双击运行本脚本（或 python push_to_github.py）
"""

import subprocess
import sys


def run(command: list[str]) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(command)}")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip())
    return result


def main() -> None:
    # 1. 确保在仓库目录
    if run(["git", "rev-parse", "--git-dir"]).returncode != 0:
        print("[错误] 请在 mining-daily-agent 目录下运行")
        sys.exit(1)

    # 2. 检查 remote
    remote_check = run(["git", "remote", "get-url", "origin"])
    if remote_check.returncode != 0:
        run(["git", "remote", "add", "origin",
             "git@github.com:ASDFGHJKL290/mining-daily-agent.git"])

    # 3. 推送
    print("\n>>> 开始推送（若提示仓库不存在，请先在 GitHub 网页创建同名私有仓库）")
    result = run(["git", "push", "-u", "origin", "master"])
    if result.returncode == 0:
        print("\n[成功] 已推送到 GitHub！")
        print("仓库地址: https://github.com/ASDFGHJKL290/mining-daily-agent")
        print("下一步: 复制该链接 + 你的简历，一起发给 HR")
    else:
        print("\n[失败] 常见原因：")
        print("  1. GitHub 上还没建仓库 -> 去 https://github.com/new 创建 mining-daily-agent（Private）")
        print("  2. SSH key 未添加到 GitHub -> 见 https://github.com/settings/keys")
        sys.exit(1)


if __name__ == "__main__":
    main()
