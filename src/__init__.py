"""rag-knowledge-base 源码包。

在这里加载 `.env`：脚本和 API 都从 `src.*` 导入，
所以只要进一次这个包，本地密钥就已经生效，不必每个入口重复写一遍。
`override=False` 保证真正的环境变量（CI、容器）优先级更高。
未安装 python-dotenv 时静默跳过，不影响生产环境。
"""

try:  # pragma: no cover - 纯粹的环境准备
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass
