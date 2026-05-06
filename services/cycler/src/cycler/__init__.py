"""cycler — multi-channel battery cycler service.

One container = one chassis. N channels per container, each running an ECM
cell as an asyncio task. Hardware-level safety lives here, not in the
orchestrator.
"""
