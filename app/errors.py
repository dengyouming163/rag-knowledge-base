"""应用层错误类型：把领域异常翻译成 HTTP 状态码的唯一出口。"""

from __future__ import annotations


class ScopeNotFoundError(LookupError):
    """请求的知识库不存在，或不属于当前租户。

    统一返回 404 而不是 403：告诉调用方"你没有权限"等于泄露"这个库存在"。
    """
