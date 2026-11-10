"""跨应用运行时（C15）：队列消费、单点调度、健康探针。

打包约束：``pyproject.toml`` 的 ``packages.find.include`` 必须包含 ``core*``，
否则镜像内 ``python -m core.runtime.worker`` 会 ``ModuleNotFoundError``。
"""
