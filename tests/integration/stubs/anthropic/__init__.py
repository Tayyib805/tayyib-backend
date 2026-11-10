class AsyncAnthropic:  # never called in these tests
    def __init__(self, *a, **k):
        pass


class APIError(Exception):
    pass


APIStatusError = APIConnectionError = RateLimitError = AuthenticationError = APIError
