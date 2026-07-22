"""Music generation MCP service (migrated unchanged)."""
from __future__ import annotations

import json
import os

import requests
from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

music_generation_mcp = FastMCP(
    name="MusicGenerationService", instructions="提供歌词、音乐生成服务"
)


@music_generation_mcp.tool
def lyrics_generation(prompt: str) -> dict:
    """根据创意主题、情绪或故事提示生成结构化歌曲歌词。必须在生成任何音乐之前首先调用。

    参数:
        prompt (str): 歌曲的核心主题、故事、情感或主体。
                     示例: "一首关于冰冷灰色工业机械的忧郁而压抑的歌曲"。

    返回值:
        dict: 包含生成歌词字符串（位于 'lyrics' 键下）以及元数据的字典。
    """
    api_host = os.environ.get("MINIMAX_API_HOST", "")
    api_key = os.environ.get("MINIMAX_API_KEY", "")

    if not api_host or not api_key:
        return {"error": "MINIMAX_API_HOST 或 MINIMAX_API_KEY 环境变量未配置"}

    payload = {"mode": "write_full_song", "prompt": prompt}
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    response = requests.post(f"{api_host}/v1/lyrics_generation", json=payload, headers=headers)

    if response.status_code != 200:
        return {"error": f"API 请求失败, 状态码: {response.status_code}", "detail": response.text[:500]}

    try:
        return response.json()
    except requests.exceptions.JSONDecodeError:
        return {"error": "API 返回了非 JSON 响应", "detail": response.text[:500]}


@music_generation_mcp.tool
def music_generation(prompt: str, lyrics: str) -> str:
    """根据指定的风格提示和结构化歌词合成高保真带人声的MP3音乐。

    参数:
        prompt (str): 详细的音乐流派、配器、Tempo（节奏）和人声特征。
                     示例: "雷鬼风格，爆炸性快节奏，情绪化的女声"。
        lyrics (str): 完整的格式化歌词文本。必须是从 'lyrics_generation' 工具获取的精确歌词，
                      或由用户明确修改并批准的内容。请勿自行编造。

    返回值:
        str: 序列化的JSON字符串，包含 'status'、生成文件的 'url'（有效期24小时）及其他指标。
    """
    api_host = os.environ.get("MINIMAX_API_HOST", "")
    api_key = os.environ.get("MINIMAX_API_KEY", "")

    if not api_host or not api_key:
        return json.dumps(
            {"error": "MINIMAX_API_HOST 或 MINIMAX_API_KEY 环境变量未配置"}, ensure_ascii=False
        )

    payload = {
        "model": "music-2.6",
        "prompt": prompt,
        "lyrics": lyrics,
        "audio_setting": {"sample_rate": 44100, "bitrate": 256000, "format": "mp3"},
        "output_format": "url",
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    response = requests.post(f"{api_host}/v1/music_generation", json=payload, headers=headers)

    if response.status_code != 200:
        return json.dumps(
            {"error": f"API 请求失败, 状态码: {response.status_code}", "detail": response.text[:500]},
            ensure_ascii=False,
        )

    try:
        return json.dumps(response.json(), ensure_ascii=False, indent=2)
    except requests.exceptions.JSONDecodeError:
        return json.dumps(
            {"error": "API 返回了非 JSON 响应", "detail": response.text[:500]}, ensure_ascii=False
        )
