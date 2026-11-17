class DomainError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.status = status

