class AsyncClient:  # network is never used in these tests
    def __init__(self, *a, **k):
        raise RuntimeError("network disabled in tests")


class Client(AsyncClient):
    pass


class HTTPError(Exception):
    pass


class TimeoutException(HTTPError):
    pass
