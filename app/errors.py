"""领域错误：Web 层据此返回 4xx/409，测试据此断言。"""


class AppError(Exception):
    http_status = 400

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.message = message
        self.code = code


class NotFound(AppError):
    http_status = 404

    def __init__(self, message: str = "资源不存在"):
        super().__init__(message, "not_found")


class PermissionDenied(AppError):
    http_status = 403

    def __init__(self, message: str = "无权访问"):
        super().__init__(message, "forbidden")


class Conflict(AppError):
    http_status = 409

    def __init__(self, message: str, code: str = "conflict"):
        super().__init__(message, code)


class ValidationError(AppError):
    http_status = 422

    def __init__(self, message: str):
        super().__init__(message, "validation_error")


class BusinessRule(AppError):
    """业务规则拦截（如未签字记录不能作为正式结论）。"""
    http_status = 409

    def __init__(self, message: str, code: str = "business_rule"):
        super().__init__(message, code)
