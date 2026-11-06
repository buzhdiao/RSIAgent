# 【中文注释·模块定位】RSIAgent 的 Actor–Verifier 适配层：把共享学习运行时接到
# 官方 ALE（Agents' Last Exam）环境与官方评分器（grader）上。该子包内部分工：
# host.py 外层宿主进程（供给+评分）、worker.py 密封智能体进程、protocol.py 固定
# 协议、capacity.py 容量池、transport.py/windows.py VM 传输适配、learning.py BRS/DRS 接线。
"""RSIAgent Actor–Verifier adapter for the official ALE environment and graders."""
