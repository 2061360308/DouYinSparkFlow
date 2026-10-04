"""用户可见的业务错误。

桥（bridge）会把 `AppError` 的消息原样回传给页面；其它异常则带上类型名，
方便区分「用户能理解的原因」和「程序 bug」。
"""

from __future__ import annotations


class AppError(Exception):
    """可预期、面向用户的错误。"""
