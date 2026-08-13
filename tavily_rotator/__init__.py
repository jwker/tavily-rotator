"""tavily-rotator:在多个 Tavily API key 之间按用量自动轮换搜索。

核心只需 requests;langchain 集成由用户自行封装(见 README 示例)。
"""

from .rotator import TavilyRotator, get_rotator, DEFAULT_DATA_FILE

__all__ = ["TavilyRotator", "get_rotator", "DEFAULT_DATA_FILE"]
__version__ = "0.1.0"
