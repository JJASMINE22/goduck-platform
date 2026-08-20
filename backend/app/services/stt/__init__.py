"""STT 供应商适配层（语音输入）。

当前实现：AssemblyAI Streaming v3（``assemblyai_stt.AssemblyAISTT``）。
一个实例 = 一个语音回合的供应商转写会话（懒连接、随回合关闭）。
"""
