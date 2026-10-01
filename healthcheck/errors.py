"""领域错误：每条业务规则失败都有确定的错误类型与可读信息。"""
from __future__ import annotations


class DomainError(Exception):
    code = "domain_error"
    http_status = 400

    def __init__(self, message: str = "", **extra):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.extra = extra


class SlotNotAvailable(DomainError):
    code = "slot_not_available"
    http_status = 409

    def __init__(self, reason: str = "该时段已被约满或不可预约"):
        super().__init__(reason)
        self.reason = reason


class SlotLockedByOther(SlotNotAvailable):
    """并发竞争失败：号源在本事务内已被他人锁定。"""
    code = "slot_locked_by_other"

    def __init__(self):
        super().__init__("该时段刚刚被其他人锁定，请选择其他时段")


class SlotClosed(DomainError):
    code = "slot_closed"
    http_status = 409


class AppointmentNotFound(DomainError):
    code = "appointment_not_found"
    http_status = 404


class PermissionDenied(DomainError):
    code = "permission_denied"
    http_status = 403


class ConsentError(DomainError):
    code = "consent_error"
    http_status = 409


class LinkInvalid(DomainError):
    """下载链接失效：撤销/到期/旧版本/重复作废——统一 410 Gone。"""
    code = "link_invalid"
    http_status = 410

    def __init__(self, message: str = "链接已失效", reason: str = "", detail: str = ""):
        super().__init__(message)
        self.message = message
        self.reason = reason or message
        self.detail = detail


class ReportVersionError(DomainError):
    code = "report_version_error"
    http_status = 409


class InterpretationError(DomainError):
    code = "interpretation_error"
    http_status = 409
