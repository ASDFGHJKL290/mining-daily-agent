"""
矿权日报 Agent — 统一命令行入口。

在项目根目录运行（等价于 python -m agent.cli）：

    python main.py                                # 默认生成 Pilbara 锂矿今日简报
    python main.py "Newmont 金矿最近 30 天有什么变化？"
    python main.py --interactive                  # 交互式问答
    python main.py --out outputs/briefing.md      # 保存 Markdown 到文件

无 DEEPSEEK_API_KEY 时 agent 自动走确定性规划，日报仍可生成
（行情/新闻走真实源，网络不可达时降级为已标注的样例数据）。
"""

from agent.cli import main


if __name__ == "__main__":
    main()
