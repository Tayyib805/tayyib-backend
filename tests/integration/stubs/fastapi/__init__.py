class HTTPException(Exception):
    def __init__(self, status_code: int, detail=None, headers=None):
        super().__init__(detail)
        self.status_code, self.detail, self.headers = status_code, detail, headers


class _Status:
    def __getattr__(self, name):  # HTTP_404_NOT_FOUND -> 404
        return int(name.split("_")[1])


status = _Status()


def Depends(dependency=None):
    return dependency


def Query(default=None, **_):
    return default


def Header(default=None, **_):
    return default


def Body(default=None, **_):
    return default


class Request:  # pragma: no cover - shape only
    headers: dict = {}


class BackgroundTasks:
    def add_task(self, fn, *a, **k):
        fn(*a, **k)


class APIRouter:
    def __init__(self, *a, **k):
        pass

    def _route(self, *a, **k):
        return lambda fn: fn

    get = post = put = patch = delete = _route

    def include_router(self, *a, **k):
        pass


class FastAPI(APIRouter):
    def add_middleware(self, *a, **k):
        pass
