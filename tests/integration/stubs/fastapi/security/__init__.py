class HTTPAuthorizationCredentials:
    def __init__(self, scheme="Bearer", credentials=""):
        self.scheme, self.credentials = scheme, credentials


class HTTPBearer:
    def __init__(self, auto_error=True):
        pass
