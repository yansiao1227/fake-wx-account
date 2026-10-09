"""数据库读取的稳定错误码；诊断不携带密钥或消息正文。"""


class DatabaseReadError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
