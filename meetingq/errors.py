"""业务异常。CLI 捕获后以中文信息退出，测试可直接断言类型。"""


class MeetingQError(Exception):
    """所有业务校验错误的基类。"""


class AuthzError(MeetingQError):
    """角色或项目权限不足。"""


class StateError(MeetingQError):
    """事项/通知/复核状态不允许该操作（状态机守卫拒绝）。"""


class ConflictError(MeetingQError):
    """并发写入冲突（版本号不匹配、唯一约束等）。"""


class NotFoundError(MeetingQError):
    """引用的事实不存在。"""


class InputContractError(MeetingQError):
    """输入文件违反资料契约。"""


class SamplingError(MeetingQError):
    """抽样容量或抽样框不足以支持计划。"""
